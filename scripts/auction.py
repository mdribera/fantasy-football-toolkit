#!/usr/bin/env python3
"""Live auction console. Run this in a terminal beside the ESPN draft room.

A full-screen Textual view (see ws_console.py) that reads the draft room's
own websocket directly: `b` bids current-high + 1, `n` nominates the
board's selected row, `space` stars a row onto the prepared queue, `/`
searches, `c` opens the command line pre-filled to send chat, `tab` moves
focus between the board and the team list. `:` opens a command line for
everything else -- `me`, `best`, `need`, `teams`, `market`, `pos`, `sort`,
`star`, `sold`, `clear`, `team`, `chat`, `quit`.

The join URL comes from `data/join-url.txt` (see --join-url-file): open the
draft room, DevTools -> Network tab -> WS filter -> copy the JOIN request's
full URL. --ws-replay drives the console from a recorded ws-log-*.jsonl
capture instead of a live socket, for rehearsal.

State persists to data/draft-state.json, so a crashed terminal loses nothing.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table

from ff import config, draft_state, draft_sync, draft_ws, values

console = Console()
VALUES_PATH = Path(__file__).resolve().parents[1] / "data" / "values.json"


# --- live websocket auction logic (pure, no I/O) --------------------------
# Kept separate from WsController (below) and unit-tested directly: this is
# the logic that decides what a real-money bid does, so it gets a test
# before it ever talks to a live socket.

CLOCK_MILESTONES_S = (10, 5)


@dataclass
class WsAuctionPointer:
    """What the live nomination is doing right now, derived from drained
    websocket events -- the state 'b' and 'n' need to know what they're
    acting on."""
    player_id: int | None = None
    high_bid: int = 0
    nominating_team: int | None = None


def apply_ws_event(pointer: WsAuctionPointer, event: draft_ws.Event) -> WsAuctionPointer:
    """Fold one parsed websocket event into the current auction pointer."""
    if isinstance(event, draft_ws.Bid):
        return WsAuctionPointer(event.player_id, event.amount, pointer.nominating_team)
    if isinstance(event, draft_ws.Nomination):
        return WsAuctionPointer(None, 0, event.team_id)
    if isinstance(event, draft_ws.Clock):
        if event.state == 2:
            return WsAuctionPointer(event.player_id, event.high_bid_amount, pointer.nominating_team)
        if event.state == 1:
            return WsAuctionPointer(None, 0, event.nominating_team)
        return pointer
    if isinstance(event, draft_ws.Sold):
        return WsAuctionPointer(None, 0, None)
    if isinstance(event, draft_ws.Init):
        # INIT's header carries an in-flight nomination, but which words hold
        # it wasn't decodable unambiguously across samples -- see
        # docs/notes/ws-protocol.md. Reset to idle instead of guessing; the
        # CLOCK frame that follows every INIT within a frame or two restores
        # it through the branches above.
        return WsAuctionPointer()
    return pointer


@dataclass(frozen=True)
class ReconcileReport:
    """What reconcile_init changed, for the console's banner."""
    added: tuple[draft_state.Purchase, ...]      # sales the console never witnessed
    corrected: tuple[draft_state.Purchase, ...]  # local team/price overwritten by the server
    removed: tuple[draft_state.Purchase, ...]    # local purchases the server does not have


def reconcile_init(
    state: draft_state.DraftState,
    init: draft_ws.InitState,
    resolver: draft_sync.PlayerResolver,
) -> ReconcileReport:
    """Overwrite state's purchases to match INIT's pick table -- the server
    is authoritative. Matches local purchases by espn_pick_id first, falling
    back to case-insensitive player name for a hand-typed record() that
    never got one. Rebuilds the purchase list and saves once rather than
    going through record_pick per pick, which would mean one atomic file
    write per sale replayed on a reconnect.
    """
    by_pick_id = {p.espn_pick_id: p for p in state.purchases if p.espn_pick_id is not None}
    by_name = {p.player.lower(): p for p in state.purchases}

    added: list[draft_state.Purchase] = []
    corrected: list[draft_state.Purchase] = []
    new_purchases: list[draft_state.Purchase] = []
    matched: set[int] = set()

    for pick in init.picks:
        team = draft_state.normalize_team(config.TEAMS.get(pick.team_id, f"TEAM{pick.team_id}"))
        existing = by_pick_id.get(pick.player_id)
        name = position = None
        if existing is None:
            name, position = resolver.resolve(pick.player_id)
            existing = by_name.get(name.lower())

        if existing is not None:
            matched.add(id(existing))
            if existing.team != team or existing.price != pick.price:
                fixed = draft_state.Purchase(
                    existing.player, existing.position, pick.price, team, pick.player_id)
                corrected.append(fixed)
                new_purchases.append(fixed)
            elif existing.espn_pick_id != pick.player_id:
                # Same team/price, just backfilling the id so next time this
                # matches on pick_id instead of falling back to name.
                new_purchases.append(draft_state.Purchase(
                    existing.player, existing.position, existing.price, existing.team,
                    pick.player_id))
            else:
                new_purchases.append(existing)
        else:
            if name is None:
                name, position = resolver.resolve(pick.player_id)
            fresh = draft_state.Purchase(name, position, pick.price, team, pick.player_id)
            added.append(fresh)
            new_purchases.append(fresh)

    removed = [p for p in state.purchases if id(p) not in matched]

    state.purchases = new_purchases
    state.save()
    return ReconcileReport(tuple(added), tuple(corrected), tuple(removed))


def clock_milestone(remaining_ms: int, announced: set[int]) -> int | None:
    """First time remaining_ms drops at or below a threshold in
    CLOCK_MILESTONES_S, returns that threshold once; the caller clears
    `announced` whenever the nomination changes."""
    for threshold in CLOCK_MILESTONES_S:
        if remaining_ms <= threshold * 1000 and threshold not in announced:
            announced.add(threshold)
            return threshold
    return None


@dataclass(frozen=True)
class BidRefused:
    reason: str


@dataclass(frozen=True)
class BidNeedsConfirmation:
    amount: int
    reason: str


@dataclass(frozen=True)
class BidReady:
    amount: int


BidPlan = BidRefused | BidNeedsConfirmation | BidReady

TYPO_GUARD_JUMP = 10             # confirm if the bid clears the current high by more than this
TYPO_GUARD_SHEET_MULTIPLE = 1.5  # confirm if the bid exceeds this multiple of sheet value


def evaluate_bid(args: list[str], current_high: int, my_max_bid: int,
                  sheet_value: int | None, already_high: bool = False) -> BidPlan:
    """Decide what 'b' or 'b <amount>' should do, before anything is sent.

    args is the command split on whitespace with the leading 'b' removed.
    No amount means "current high + 1", the common case. already_high means
    the caller already holds the current high bid on this player -- refused
    unconditionally, since raising your own price buys nothing.
    """
    if args:
        if not args[0].isdigit():
            return BidRefused(f"'{args[0]}' is not a dollar amount")
        amount = int(args[0])
    else:
        amount = current_high + 1

    if already_high:
        return BidRefused(f"you already hold the ${current_high} high bid -- "
                          "no need to bid against yourself")
    if amount <= current_high:
        return BidRefused(f"${amount} does not beat the current high of ${current_high}")
    if amount > my_max_bid:
        return BidRefused(f"${amount} exceeds your max bid of ${my_max_bid}")

    jump = amount - current_high
    if jump > TYPO_GUARD_JUMP:
        return BidNeedsConfirmation(amount, f"${amount} is ${jump} over the current high of ${current_high}")
    if sheet_value is not None and amount > sheet_value * TYPO_GUARD_SHEET_MULTIPLE:
        return BidNeedsConfirmation(amount, f"${amount} is well over the ${sheet_value} sheet value")
    return BidReady(amount)


# 0.9 and 1.1 are the same cutoffs the 'market' read uses for under/over sheet;
# the top band is TYPO_GUARD_SHEET_MULTIPLE, where evaluate_bid already stops
# and asks. Keeping them aligned means the panel and the guard agree.
VERDICT_BARGAIN_RATIO = 0.9
VERDICT_FAIR_RATIO = 1.1


@dataclass(frozen=True)
class Verdict:
    label: str
    style: str


def bid_verdict(current_high: int, sheet_value: int | None) -> Verdict:
    """How the current high bid reads against the sheet value -- not the
    inflation-adjusted price, which this room's own front-loaded early
    pace (see docs/auction-strategy.md) drives broadly negative for reasons
    that have nothing to do with the player on the clock."""
    if not sheet_value:
        return Verdict("no read", "dim")
    ratio = current_high / sheet_value
    if ratio < VERDICT_BARGAIN_RATIO:
        return Verdict("good value", "green")
    if ratio <= VERDICT_FAIR_RATIO:
        return Verdict("fair", "dim")
    if ratio <= TYPO_GUARD_SHEET_MULTIPLE:
        return Verdict("pricey", "yellow")
    return Verdict("overpaying", "red")


def next_equivalent(vals: list["values.Valuation"], taken: set[str], position: str,
                     tier: int, exclude_name: str | None = None) -> "values.Valuation | None":
    """Best player still available at `position` in `tier`, or in the nearest
    tier below it if that tier is gone.

    The auction question is never whether a player is worth $34, it is whether
    an equivalent one is still on the board if you lose the bid. Lower tier
    number is the better tier, so filtering to `tier` or worse and then taking
    the minimum tier present gives a same-tier answer when one exists and the
    honest fallback when one does not.
    """
    pool = [v for v in vals
            if v.position == position
            and v.tier >= tier
            and v.name not in taken
            and v.name != exclude_name]
    if not pool:
        return None
    best_tier = min(v.tier for v in pool)
    return max((v for v in pool if v.tier == best_tier), key=lambda v: v.value)


def rostered_qb_bye_clash(state: draft_state.DraftState, team: str,
                          vals: list["values.Valuation"]) -> int | None:
    """Bye week shared by two or more of `team`'s already-rostered QBs, if
    any. The third QB exists specifically to cover a started QB's bye --
    two QBs who share a bye defeat the whole point of carrying one."""
    lookup = {v.name: v.bye for v in vals}
    byes: dict[int, int] = {}
    for p in state.purchases:
        if p.team == team and p.position == "QB":
            bye = lookup.get(p.player)
            if bye is not None:
                byes[bye] = byes.get(bye, 0) + 1
    return next((bye for bye, count in byes.items() if count >= 2), None)


def qb_bye_would_clash(state: draft_state.DraftState, team: str,
                       vals: list["values.Valuation"],
                       candidate: "values.Valuation") -> bool:
    """Whether buying `candidate` would put two of `team`'s QBs on the same
    bye -- checked against a QB being actively considered, before the
    purchase happens, rather than only ever caught after the fact."""
    if candidate.position != "QB" or candidate.bye is None:
        return False
    lookup = {v.name: v.bye for v in vals}
    return any(p.team == team and p.position == "QB"
              and lookup.get(p.player) == candidate.bye
              for p in state.purchases)


DEFAULT_JOIN_URL_FILE = Path(__file__).resolve().parents[1] / "data" / "join-url.txt"


def load_join_url(path: Path) -> str:
    if not path.exists():
        raise SystemExit(
            f"No join URL at {path}. Open the draft room, copy the JOIN request's full URL "
            "from DevTools (Network tab -> WS filter), and paste it into that file."
        )
    url = path.read_text().strip()
    if not url:
        raise SystemExit(f"{path} is empty.")
    return url


class WsController:
    """Wraps DraftRoomClient with a drain-before-prompt shape -- events pile
    up on a queue in a background thread and the main loop drains them all
    before every prompt, so there's no race with a typed command -- plus the
    live auction pointer ('b'/'n' need to know what nomination is active and
    at what price)."""

    def __init__(self, join_url: str, cred: config.EspnCredentials):
        self.client = draft_ws.DraftRoomClient(join_url, cred)
        self.pointer = WsAuctionPointer()
        self._announced: set[int] = set()

    def start(self) -> None:
        self.client.start()

    def drain(self) -> list[draft_ws.Event]:
        events = self.client.drain()
        for event in events:
            updated = apply_ws_event(self.pointer, event)
            if updated.player_id != self.pointer.player_id:
                self._announced.clear()
            self.pointer = updated
        return events

    def drain_alerts(self) -> list[str]:
        return self.client.drain_alerts()

    @property
    def connected(self) -> bool:
        return self.client.connected

    def milestone(self, event: draft_ws.Clock) -> int | None:
        return clock_milestone(event.remaining_ms, self._announced)


NOMINATION_LIST_PATH = Path(__file__).resolve().parents[1] / "data" / "nomination-list.txt"
WS_REPLAY_STATE_PATH = (Path(__file__).resolve().parents[1] / "data" / "cache" /
                        "ws-console-replay-state.json")


def load_nomination_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def save_nomination_list(path: Path, names: list[str]) -> None:
    """Write the starred set back to disk, tmp-then-replace so a crash mid
    write never corrupts the file. Matches draft_state.save's convention."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(names) + ("\n" if names else ""))
    tmp.replace(path)


@dataclass
class BoardRow:
    """One row of the nomination board: a priced player plus the numbers and
    state that only make sense live -- the market-adjusted price, the edge
    against sheet, and whether it's starred onto the prepared queue."""

    valuation: values.Valuation
    adjusted: int | None    # None: no market read at this player's position
    edge: int | None        # sheet value minus adjusted value; positive = bargain
    starred: bool
    owner: str | None = None   # the fantasy team that drafted this player,
                                # set only when nomination_board's sold filter
                                # includes drafted rows

    @property
    def name(self) -> str:
        return self.valuation.name


NOMINATION_BOARD_SORTS = ("rank", "rec", "pos", "tier", "sheet", "adj", "espn", "bye", "proj", "name")


def nomination_board(
    vals: list[values.Valuation],
    state: draft_state.DraftState,
    inflation: float | None | dict[str, float],
    *,
    starred: set[str] = frozenset(),
    query: str | None = None,
    position: str | None = None,
    starred_only: bool = False,
    sort: str = "rank",
    sold: str = "off",
) -> list[BoardRow]:
    """The full available board: every undrafted priced player, ready to
    search, filter and sort. `inflation` is taken as a parameter rather than
    recomputed here -- callers hold a single draft-wide rate (or per-position
    rates, from DraftState.forward_inflation_by_position) and pass it in
    once, since recomputing it per row is fine at a few dozen rows but not at
    the full ~600-player board. A plain float applies to every position; a
    dict falls back to the overall forward rate for a position with no entry
    (not enough sales at that position yet to tilt it). Either can be None
    -- no read available -- in which case the affected rows' adjusted/edge
    are None too.

    `sold` controls whether drafted players appear at all: "off" (default)
    hides them, matching every caller before this parameter existed; "on"
    includes them alongside the available pool; "only" shows nothing else.
    A drafted row's `owner` is set to the fantasy team that bought it.
    """
    taken = {name.lower() for name in state.taken()}
    owners = {p.player.lower(): p.team for p in state.purchases}
    starred_lower = {name.lower() for name in starred}
    overall = state.forward_inflation(vals) if isinstance(inflation, dict) else inflation

    rows = []
    for v in vals:
        is_taken = v.name.lower() in taken
        if sold == "off" and is_taken:
            continue
        if sold == "only" and not is_taken:
            continue
        if query and query.lower() not in v.name.lower():
            continue
        if position:
            pos_filter = position.upper()
            if pos_filter == "FLEX":
                if v.position not in config.FLEX_ELIGIBLE:
                    continue
            elif v.position != pos_filter:
                continue
        is_starred = v.name.lower() in starred_lower
        if starred_only and not is_starred:
            continue
        rate = inflation.get(v.position, overall) if isinstance(inflation, dict) else inflation
        adjusted = max(1, round(v.value * rate)) if rate is not None else None
        rows.append(BoardRow(
            valuation=v,
            adjusted=adjusted,
            edge=(v.value - adjusted) if adjusted is not None else None,
            starred=is_starred,
            owner=owners.get(v.name.lower()) if is_taken else None,
        ))

    key_funcs = {
        "rank": lambda r: r.valuation.value,
        "rec": lambda r: r.edge or 0,
        "pos": lambda r: r.valuation.position,
        "tier": lambda r: -r.valuation.tier,
        "sheet": lambda r: r.valuation.value,
        "adj": lambda r: r.adjusted or 0,
        "espn": lambda r: r.valuation.espn_avg or 0,
        "bye": lambda r: r.valuation.bye or 0,
        "proj": lambda r: r.valuation.projected_points,
        "name": lambda r: r.valuation.name.lower(),
    }
    key = key_funcs.get(sort, key_funcs["rank"])
    reverse = sort not in ("pos", "name")
    rows.sort(key=key, reverse=reverse)
    # Starred rows lead the board regardless of the active sort, so the
    # prepared queue always sits on top -- stable sort keeps each group's
    # internal order intact.
    rows.sort(key=lambda r: not r.starred)
    return rows


def load_state(fresh: bool, path: Path = draft_state.STATE_PATH) -> draft_state.DraftState:
    """Fresh state for a new practice-draft rehearsal, or the persisted one.

    data/draft-state.json is a single global file with no per-draft key, so
    loading it across separate practice-draft sessions carries every earlier
    session's purchases along -- including collisions on real players who get
    nominated in more than one session, since espn_pick_id is the real
    player's permanent id, not scoped to one draft. --fresh sidesteps that by
    skipping the load entirely; the first save() after this still writes to
    the same path, so a --fresh run replaces the file's contents once
    anything gets recorded. Whatever was already there gets backed up first,
    so --fresh never permanently destroys a real session's data.
    """
    if fresh:
        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_text(path.read_text())
        return draft_state.DraftState(state_path=path)
    return draft_state.DraftState.load(path)


def load_values() -> list[values.Valuation]:
    if not VALUES_PATH.exists():
        console.print("[red]No data/values.json.[/red] Run scripts/build_values.py first.")
        raise SystemExit(1)
    return [values.Valuation(**row) for row in json.loads(VALUES_PATH.read_text())]


def me_table(state: draft_state.DraftState,
             vals: list[values.Valuation]) -> tuple[Table, str]:
    me = state.my_team
    table = Table(title=f"{me} -- ${state.budget_left(me)} left, "
                        f"{state.spots_left(me)} spots, max bid ${state.max_bid(me)}")
    table.add_column("Player")
    table.add_column("Pos", justify="center")
    table.add_column("Paid", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("Edge", justify="right")

    lookup = {v.name: v.value for v in vals}
    for p in state.purchases:
        if p.team != me:
            continue
        value = lookup.get(p.player)
        edge = f"{value - p.price:+d}" if value is not None else "-"
        style = "green" if value and value > p.price else "red" if value else "dim"
        table.add_row(p.player, p.position, f"${p.price}",
                      f"${value}" if value else "-", f"[{style}]{edge}[/{style}]")

    unfilled = {k: v for k, v in state.needs(me).items() if v > 0}
    if unfilled:
        footer = "Still need: " + ", ".join(f"{k} x{v}" for k, v in unfilled.items())
    else:
        # Starting slots filled is not the same as a full bench -- QB
        # especially, where the third quarterback exists for byes and the
        # in-season waiver wire is empty.
        remaining_targets = {k: v for k, v in state.targets(me).items() if v > 0}
        if remaining_targets:
            footer = ("[green]All starting slots filled.[/green] Still want: "
                      + ", ".join(f"{k} x{v}" for k, v in remaining_targets.items()))
        else:
            footer = "[green]All starting slots filled.[/green]"
    return table, footer


def best_table(state: draft_state.DraftState, vals: list[values.Valuation],
               position=None, limit=15) -> Table:
    taken = state.taken()
    pool = [v for v in vals if v.name not in taken]
    if position:
        pool = [v for v in pool if v.position == position.upper()]
    pool = sorted(pool, key=lambda v: v.value, reverse=True)[:limit]

    overall = state.forward_inflation(vals)
    by_position = state.forward_inflation_by_position(vals)
    market_read = f"market x{overall:.2f} forward" if overall is not None else "market no read"
    table = Table(title=f"Best available{' -- ' + position.upper() if position else ''} "
                        f"({market_read})")
    table.add_column("Player")
    table.add_column("Pos", justify="center")
    table.add_column("Tier", justify="center")
    table.add_column("Proj", justify="right")
    table.add_column("Sheet", justify="right")
    table.add_column("Adjusted", justify="right", style="bold")

    for v in pool:
        rate = by_position.get(v.position, overall)
        adjusted = max(1, round(v.value * rate)) if rate is not None else None
        table.add_row(v.name, v.position, str(v.tier),
                      f"{v.projected_points:.0f}", f"${v.value}",
                      f"${adjusted}" if adjusted is not None else "-")
    return table


def teams_table(state: draft_state.DraftState) -> Table:
    table = Table(title="League budgets")
    table.add_column("Team")
    table.add_column("Spent", justify="right")
    table.add_column("Left", justify="right")
    table.add_column("Spots", justify="right")
    table.add_column("Max bid", justify="right")
    for team in state.all_teams():
        style = "bold" if team == state.my_team else ""
        table.add_row(f"[{style}]{team}[/{style}]" if style else team,
                      f"${state.spent_by(team)}", f"${state.budget_left(team)}",
                      str(state.spots_left(team)), f"${state.max_bid(team)}")
    return table


def market_table(state: draft_state.DraftState,
                 vals: list[values.Valuation]) -> Table:
    """What the room's remaining money projects to pay at each position,
    forward-looking (see DraftState.forward_inflation_by_position) -- the
    "buy here" / "let these go" call is inherently about what's still ahead,
    not a record of what already sold."""
    overall = state.forward_inflation(vals)
    overall_read = f"x{overall:.2f} forward" if overall is not None else "no read"
    table = Table(title=f"Market vs sheet -- overall {overall_read}")
    table.add_column("Pos")
    table.add_column("Forward", justify="right")
    table.add_column("Read")
    by_position = state.forward_inflation_by_position(vals)
    if not by_position:
        table.add_row("-", "-", "[dim]no per-position read yet[/dim]")
        return table
    for pos, rate in sorted(by_position.items(), key=lambda kv: kv[1], reverse=True):
        if rate > 1.1:
            read = "[red]over sheet -- let these go[/red]"
        elif rate < 0.9:
            read = "[green]under sheet -- buy here[/green]"
        else:
            read = "[dim]at sheet[/dim]"
        table.add_row(pos, f"x{rate:.2f}", read)
    return table


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--join-url-file", type=Path, default=DEFAULT_JOIN_URL_FILE,
                        help=f"file holding the pasted JOIN URL (default: {DEFAULT_JOIN_URL_FILE})")
    parser.add_argument("--ws-replay", metavar="PATH",
                        help="drive the console from a recorded ws-log-*.jsonl capture "
                             "instead of a live socket (for rehearsal) -- no join URL "
                             "needed, and never touches data/draft-state.json or "
                             "data/nomination-list.txt")
    parser.add_argument("--replay-speed", type=float, default=1.0,
                        help="--ws-replay playback speed multiplier (default 1.0x the "
                             "capture's own real-time pacing; 0 plays back with no pacing "
                             "at all)")
    parser.add_argument("--my-team", help="team label to use for 'me'/'need'")
    parser.add_argument("--league-id", help="override ESPN_LEAGUE_ID (e.g. a practice draft)")
    parser.add_argument("--team-id", help="override ESPN_TEAM_ID")
    parser.add_argument("--fresh", action="store_true",
                        help="start with an empty draft state instead of loading "
                             "data/draft-state.json (for practice-draft rehearsals)")
    args = parser.parse_args()

    vals = load_values()
    if args.ws_replay:
        # A dedicated scratch path, backed up by load_state's own --fresh
        # logic if a previous replay left one behind -- a rehearsal must
        # never be able to touch real draft-day state.
        state = load_state(True, path=WS_REPLAY_STATE_PATH)
        console.print(f"[yellow]--ws-replay: scratch draft state at "
                      f"{WS_REPLAY_STATE_PATH}, never touches "
                      "data/draft-state.json.[/yellow]")
    else:
        state = load_state(args.fresh)
        if args.fresh:
            console.print("[yellow]--fresh: starting with an empty draft state; "
                          "data/draft-state.json will be overwritten on first save.[/yellow]")
    if args.my_team:
        state.my_team = draft_state.normalize_team(args.my_team)

    cred = config.EspnCredentials()
    if args.league_id:
        cred.league_id = args.league_id
    if args.team_id:
        cred.team_id = args.team_id

    resolver = draft_sync.PlayerResolver(VALUES_PATH)
    nomination_list = load_nomination_list(NOMINATION_LIST_PATH)

    if args.ws_replay:
        # Imported here, not at module scope: ws_replay imports this module
        # (the same shape ws_console does), so pulling it in only when
        # actually replaying keeps that circularity confined to this branch.
        from ws_replay import ReplayController

        ws = ReplayController(Path(args.ws_replay), speed=args.replay_speed)
        sync_note = f"replay of {args.ws_replay} at {args.replay_speed}x"
    else:
        join_url = load_join_url(args.join_url_file)
        ws = WsController(join_url, cred)
        sync_note = "live websocket"
    ws.start()

    console.print(f"[bold]Auction console[/bold] -- {config.LEAGUE_NAME}, league {cred.league_id}, "
                  f"${config.SALARY_CAP} cap, {config.ROSTER_SIZE} spots, {sync_note}. "
                  f"{len(state.purchases)} purchases loaded. Type 'quit' to exit.\n")

    # Imported here, not at module scope: ws_console imports this module, and
    # a bare --help shouldn't need to pull in textual.
    from ws_console import run_ws_console

    # A replay never writes the starred queue back to the real file --
    # nomination_list_path=None makes space a harmless no-op instead.
    list_path = None if args.ws_replay else NOMINATION_LIST_PATH
    run_ws_console(ws, state, resolver, vals, nomination_list, list_path)
    ws.client.stop()
    state.save()
    console.print("Saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
