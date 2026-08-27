#!/usr/bin/env python3
"""Live auction console. Run this in a terminal beside the ESPN draft room.

Commands (type at the > prompt):
  <player> <price> <team>   record a purchase        e.g. "Josh Allen 62 ME"
  me                        my roster, budget, max bid, unfilled slots
  best [POS] [n]            best remaining by value
  need                      best remaining at positions I still must fill
  teams                     every team's budget and roster count
  market                    inflation: is the room paying over or under sheet
  sync                      force an immediate pull from the ESPN draft feed
  undo                      remove the last recorded purchase
  quit

By default this polls ESPN's own draft-detail feed in the background and
auto-records completed picks as they close -- see 'sync' to force a pull, and
--no-sync to disable it and enter everything by hand. If the feed goes quiet,
a banner says so; manual entry keeps working regardless.

State persists to data/draft-state.json, so a crashed terminal loses nothing.
"""

import argparse
import contextlib
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.table import Table

from ff import config, draft_state, draft_sync, draft_ws, values

console = Console()
VALUES_PATH = Path(__file__).resolve().parents[1] / "data" / "values.json"
FEED_DOWN_THRESHOLD = 3  # consecutive failed polls before we warn out loud


class _ReplayDone(Exception):
    """Internal: the replay fixture has no more snapshots."""


def _replay_source(path: Path):
    """A PickSource that steps through a recorded fixture instead of ESPN.

    Lets --replay drive the exact same background-poller code path as a live
    draft, which is the point: this is what a rehearsal actually rehearses.
    """
    snapshots = iter(list(draft_sync.replay_snapshots(path)))

    def _next() -> dict:
        try:
            return next(snapshots)
        except StopIteration:
            raise _ReplayDone() from None

    return _next


class SyncController:
    """Background poller feeding completed picks to the main REPL loop.

    The thread only ever reads from ESPN and pushes onto a queue -- it never
    touches DraftState. The main loop is the sole writer, so there's no race
    with manual entry or 'undo'.
    """

    def __init__(self, source, resolver: draft_sync.PlayerResolver, interval: int):
        self.feed = draft_sync.DraftFeed(source, resolver)
        self.interval = interval
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self._queue: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wake(self) -> None:
        """Ask the poller to run now instead of waiting out the interval."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                picks = self.feed.poll()
                self.consecutive_failures = 0
                self.last_error = None
                if picks:
                    with self._lock:
                        self._queue.extend(picks)
            except _ReplayDone:
                return
            except draft_sync.DraftFeedError as exc:
                self.consecutive_failures += 1
                self.last_error = str(exc)
            self._wake.wait(self.interval)
            self._wake.clear()

    def drain(self) -> list:
        with self._lock:
            picks, self._queue = self._queue, []
        return picks

    def is_down(self) -> bool:
        return self.consecutive_failures >= FEED_DOWN_THRESHOLD


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
    if isinstance(event, draft_ws.Clock):
        if event.state == 2:
            return WsAuctionPointer(event.player_id, event.high_bid_amount, pointer.nominating_team)
        if event.state == 1:
            return WsAuctionPointer(None, 0, event.nominating_team)
        return pointer
    if isinstance(event, draft_ws.Sold):
        return WsAuctionPointer(None, 0, None)
    return pointer


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
TYPO_GUARD_SHEET_MULTIPLE = 1.5  # confirm if the bid exceeds this multiple of adjusted sheet value


def evaluate_bid(args: list[str], current_high: int, my_max_bid: int,
                  adjusted_value: int | None) -> BidPlan:
    """Decide what 'b' or 'b <amount>' should do, before anything is sent.

    args is the command split on whitespace with the leading 'b' removed.
    No amount means "current high + 1", the common case.
    """
    if args:
        if not args[0].isdigit():
            return BidRefused(f"'{args[0]}' is not a dollar amount")
        amount = int(args[0])
    else:
        amount = current_high + 1

    if amount <= current_high:
        return BidRefused(f"${amount} does not beat the current high of ${current_high}")
    if amount > my_max_bid:
        return BidRefused(f"${amount} exceeds your max bid of ${my_max_bid}")

    jump = amount - current_high
    if jump > TYPO_GUARD_JUMP:
        return BidNeedsConfirmation(amount, f"${amount} is ${jump} over the current high of ${current_high}")
    if adjusted_value is not None and amount > adjusted_value * TYPO_GUARD_SHEET_MULTIPLE:
        return BidNeedsConfirmation(amount, f"${amount} is well over the ${adjusted_value} adjusted sheet value")
    return BidReady(amount)


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
    """Wraps DraftRoomClient with the same drain-before-prompt shape
    SyncController gives the REST path, plus the live auction pointer
    ('b'/'n' need to know what nomination is active and at what price)."""

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

    def milestone(self, event: draft_ws.Clock) -> int | None:
        return clock_milestone(event.remaining_ms, self._announced)


def _drain_ws(ws: WsController, state: draft_state.DraftState,
              resolver: draft_sync.PlayerResolver, vals: list["values.Valuation"],
              nomination_list: list[str]) -> None:
    """Print live websocket events and record completed sales, mirroring
    _drain_sync's shape for the REST feed."""
    for alert in ws.drain_alerts():
        console.print(f"[bold red]{alert}[/bold red]")

    lookup = {v.name: v for v in vals}
    for event in ws.drain():
        if isinstance(event, draft_ws.Nomination):
            team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
            console.print(f"[bold]NOMINATION[/bold] {team} is on the clock")
            if event.team_id == config.MY_TEAM_ID:
                console.print("[bold yellow]Your turn to nominate.[/bold yellow]")
                show_nomination_list(state, vals, nomination_list)
        elif isinstance(event, draft_ws.Bid):
            team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
            name, _ = resolver.resolve(event.player_id)
            console.print(f"[dim]BID[/dim] {team} ${event.amount} on {name}")
        elif isinstance(event, draft_ws.Clock) and event.state == 2:
            milestone = ws.milestone(event)
            if milestone:
                console.print(f"[yellow]{milestone}s[/yellow] left, high bid ${event.high_bid_amount}")
        elif isinstance(event, draft_ws.Sold):
            team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
            name, position = resolver.resolve(event.player_id)
            if state.record_pick(name, position, event.price, team, espn_pick_id=event.player_id):
                match = lookup.get(name)
                note = f" (sheet ${match.value}, {match.value - event.price:+d})" if match else ""
                console.print(f"[green]SOLD[/green] {name} ${event.price} -> {team}{note}")
        elif isinstance(event, draft_ws.WsError):
            console.print(f"[yellow]unparsed frame:[/yellow] {event.raw!r} ({event.reason})")


def _ws_printer_loop(ws: WsController, state: draft_state.DraftState,
                      resolver: draft_sync.PlayerResolver, vals: list["values.Valuation"],
                      nomination_list: list[str], stop: threading.Event) -> None:
    """Prints live websocket events on their own cadence instead of only
    between prompts, so Mark sees BID/CLOCK updates while typing a
    response. Safe to print from this thread because main() wraps the
    whole session in patch_stdout()."""
    while not stop.wait(0.3):
        _drain_ws(ws, state, resolver, vals, nomination_list)


def show_nomination_list(state: draft_state.DraftState, vals: list["values.Valuation"],
                          names: list[str]) -> None:
    console.print("[dim]Nomination list not wired up yet.[/dim]")


def load_values() -> list[values.Valuation]:
    if not VALUES_PATH.exists():
        console.print("[red]No data/values.json.[/red] Run scripts/build_values.py first.")
        raise SystemExit(1)
    return [values.Valuation(**row) for row in json.loads(VALUES_PATH.read_text())]


def show_me(state: draft_state.DraftState, vals: list[values.Valuation]) -> None:
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
    console.print(table)

    needs = state.needs(me)
    unfilled = {k: v for k, v in needs.items() if v > 0}
    if unfilled:
        console.print("Still need: " + ", ".join(f"{k} x{v}" for k, v in unfilled.items()))
    else:
        console.print("[green]All starting slots filled.[/green]")


def show_best(state, vals, position=None, limit=15) -> None:
    taken = state.taken()
    pool = [v for v in vals if v.name not in taken]
    if position:
        pool = [v for v in pool if v.position == position.upper()]
    pool = sorted(pool, key=lambda v: v.value, reverse=True)[:limit]

    inflation = state.inflation(vals)
    table = Table(title=f"Best available{' -- ' + position.upper() if position else ''} "
                        f"(market x{inflation:.2f})")
    table.add_column("Player")
    table.add_column("Pos", justify="center")
    table.add_column("Tier", justify="center")
    table.add_column("Proj", justify="right")
    table.add_column("Sheet", justify="right")
    table.add_column("Adjusted", justify="right", style="bold")

    for v in pool:
        adjusted = max(1, round(v.value * inflation))
        table.add_row(v.name, v.position, str(v.tier),
                      f"{v.projected_points:.0f}", f"${v.value}", f"${adjusted}")
    console.print(table)


def show_teams(state: draft_state.DraftState) -> None:
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
    console.print(table)


def _drain_sync(sync: SyncController, state: draft_state.DraftState,
                 vals: list[values.Valuation]) -> None:
    """Pull whatever the poller has queued and record it, before every prompt.

    A pick already present under the same player name -- typed in by hand
    before the feed caught up -- is skipped rather than recorded twice.
    """
    picks = sync.drain()
    if not picks:
        if sync.is_down():
            console.print(f"[bold red]FEED DOWN[/bold red] ({sync.last_error}) -- "
                          "[bold red]ENTER PICKS MANUALLY[/bold red]")
        return

    existing = {p.player.lower() for p in state.purchases}
    lookup = {v.name.lower(): v for v in vals}
    for pick in picks:
        if pick.player.lower() in existing:
            continue
        if not state.record_pick(pick.player, pick.position, pick.price,
                                  pick.team, pick.espn_pick_id):
            continue
        existing.add(pick.player.lower())
        match = lookup.get(pick.player.lower())
        note = f" (sheet ${match.value}, {match.value - pick.price:+d})" if match else ""
        console.print(f"[dim][auto][/dim] {pick.player} ${pick.price} -> {pick.team}{note}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-sync", action="store_true",
                        help="disable automatic pick import; manual entry only")
    parser.add_argument("--replay", metavar="PATH",
                        help="drive the sync feed from a recorded JSONL fixture "
                             "instead of live ESPN (for rehearsal)")
    parser.add_argument("--interval", type=int, default=3,
                        help="seconds between background polls (default 3)")
    parser.add_argument("--ws", action="store_true",
                        help="use the live draft-room websocket instead of polling mDraftDetail")
    parser.add_argument("--join-url-file", type=Path, default=DEFAULT_JOIN_URL_FILE,
                        help=f"file holding the pasted JOIN URL for --ws (default: {DEFAULT_JOIN_URL_FILE})")
    parser.add_argument("--my-team", help="team label to use for 'me'/'need'")
    parser.add_argument("--league-id", help="override ESPN_LEAGUE_ID (e.g. a practice draft)")
    parser.add_argument("--team-id", help="override ESPN_TEAM_ID")
    args = parser.parse_args()

    if args.ws and (args.no_sync or args.replay):
        parser.error("--ws replaces the REST sync path; drop --no-sync/--replay")

    vals = load_values()
    state = draft_state.DraftState.load()
    if args.my_team:
        state.my_team = draft_state.normalize_team(args.my_team)
    lookup = {v.name.lower(): v for v in vals}

    cred = config.EspnCredentials()
    if args.league_id:
        cred.league_id = args.league_id
    if args.team_id:
        cred.team_id = args.team_id

    resolver = draft_sync.PlayerResolver(VALUES_PATH)
    sync: SyncController | None = None
    ws: WsController | None = None
    nomination_list: list[str] = []

    if args.ws:
        join_url = load_join_url(args.join_url_file)
        ws = WsController(join_url, cred)
        ws.start()
    elif not args.no_sync:
        if args.replay:
            source = _replay_source(Path(args.replay))
        else:
            source = lambda: draft_sync.fetch_picks(cred)  # noqa: E731
        sync = SyncController(source, resolver, args.interval)
        sync.start()

    if ws:
        sync_note = "live websocket"
    elif sync:
        sync_note = "auto-sync every %ds" % args.interval
    else:
        sync_note = "sync disabled, manual only"
    console.print(f"[bold]Auction console[/bold] -- {config.LEAGUE_NAME}, league {cred.league_id}, "
                  f"${config.SALARY_CAP} cap, {config.ROSTER_SIZE} spots, {sync_note}. "
                  f"{len(state.purchases)} purchases loaded. Type 'quit' to exit.\n")

    session = PromptSession() if ws else None
    stop_printer = threading.Event()
    printer_thread: threading.Thread | None = None
    if ws:
        printer_thread = threading.Thread(
            target=_ws_printer_loop, args=(ws, state, resolver, vals, nomination_list, stop_printer),
            daemon=True)

    with patch_stdout() if ws else contextlib.nullcontext():
        if printer_thread:
            printer_thread.start()
        while True:
            if sync:
                _drain_sync(sync, state, vals)
            try:
                line = (session.prompt("> ") if session else
                        console.input("[bold cyan]>[/bold cyan] ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue

            cmd = line.split()
            head = cmd[0].lower()

            if head in ("quit", "exit", "q"):
                break
            if head == "me":
                show_me(state, vals)
            elif head == "teams":
                show_teams(state)
            elif head == "sync":
                if sync:
                    sync.wake()
                    time.sleep(0.5)
                    _drain_sync(sync, state, vals)
                elif ws:
                    console.print("[dim]Live websocket -- nothing to force-sync.[/dim]")
                else:
                    console.print("[yellow]Sync disabled (--no-sync).[/yellow]")
            elif head == "market":
                table = Table(title=f"Market vs sheet -- overall x{state.inflation(vals):.2f}")
                table.add_column("Pos")
                table.add_column("Paying", justify="right")
                table.add_column("Read")
                for pos, rate in sorted(state.inflation_by_position(vals).items(),
                                        key=lambda kv: kv[1], reverse=True):
                    if rate > 1.1:
                        read = "[red]over sheet -- let these go[/red]"
                    elif rate < 0.9:
                        read = "[green]under sheet -- buy here[/green]"
                    else:
                        read = "[dim]at sheet[/dim]"
                    table.add_row(pos, f"x{rate:.2f}", read)
                console.print(table)
                console.print(f"Other teams still hold [bold]"
                              f"${state.dollars_remaining_in_room()}[/bold] combined.")
            elif head == "undo":
                removed = state.undo()
                console.print(f"Removed: {removed}" if removed else "Nothing to undo.")
            elif head == "need":
                for pos, count in state.needs(state.my_team).items():
                    if count > 0:
                        show_best(state, vals, pos, limit=6)
            elif head == "best":
                pos = cmd[1] if len(cmd) > 1 and not cmd[1].isdigit() else None
                n = next((int(c) for c in cmd[1:] if c.isdigit()), 15)
                show_best(state, vals, pos, n)
            elif len(cmd) >= 3 and cmd[-2].lstrip("$").isdigit():
                team = cmd[-1]
                price = int(cmd[-2].lstrip("$"))
                name = " ".join(cmd[:-2])
                match = lookup.get(name.lower())
                if not match:
                    candidates = [v for k, v in lookup.items() if name.lower() in k]
                    if len(candidates) == 1:
                        match = candidates[0]
                    elif candidates:
                        console.print("Ambiguous: " + ", ".join(c.name for c in candidates[:8]))
                        continue
                position = match.position if match else "?"
                canonical = draft_state.normalize_team(team)
                if canonical not in state.all_teams() and len(state.purchases) > 3:
                    console.print(f"[yellow]New team label '{canonical}'.[/yellow] "
                                  "Typo? 'undo' reverses it.")
                state.record(match.name if match else name, position, price, team)
                note = ""
                if match:
                    delta = match.value - price
                    note = f" (sheet ${match.value}, {delta:+d})"
                console.print(f"Recorded: {name} ${price} -> {team}{note}")
            else:
                console.print("[yellow]Unrecognized.[/yellow] "
                              "Use: '<player> <price> <team>' or me/best/need/teams/market/undo/quit")
        stop_printer.set()

    if ws:
        ws.client.stop()
    state.save()
    console.print("Saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
