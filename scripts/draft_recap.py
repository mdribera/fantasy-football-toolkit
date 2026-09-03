#!/usr/bin/env python3
"""Post-draft recap: grades and highlights from a captured live auction.

Rebuilds the complete pick record from one or more console captures (see
draft_ws.py) and grades every team on four independent axes -- Sheet
surplus, market surplus against ESPN's own auction averages, projected
starting-lineup points, and roster construction risk -- rather than a single
number, since grading purely against our own values.json is circular (we bid
to that sheet, so it always scores us best).

    scripts/draft_recap.py [LOG ...] [--out PATH]

With no LOG arguments, reads the two captures that cover the 2026 draft
(the room reconnected once, splitting it). `--out -` writes to stdout;
the default writes docs/notes/draft-recap-2026.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff import config, draft_state, draft_sync, draft_ws, values

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
VALUES_PATH = DATA / "values.json"
DEFAULT_LOGS = [DATA / "ws-log-1788397138.jsonl", DATA / "ws-log-1788398240.jsonl"]
DEFAULT_OUT = ROOT / "docs" / "notes" / "draft-recap-2026.md"
LEAGUE_TZ = ZoneInfo("America/Los_Angeles")

# A bid's clock_remaining_at_bid_ms floors at 10000 (the server bumping a
# short clock back up) rather than continuing to count down -- confirmed
# against the real capture, see docs/notes/ws-protocol.md. That floor is the
# only reliable "landed in the final stretch" signal on the wire; there is no
# sub-3s data.
LATE_BID_CLOCK_MS = 10000

# How much each grading axis counts toward the blended letter grade. Points
# carries the most weight since it is the only axis that is entirely
# price-blind; risk is a set of deductions, not a continuous read, so it
# counts least.
AXIS_WEIGHTS = {"sheet": 0.25, "market": 0.25, "points": 0.35, "risk": 0.15}

GRADE_BANDS = [
    (1.25, "A+"), (0.9, "A"), (0.6, "A-"),
    (0.3, "B+"), (0.0, "B"), (-0.3, "B-"),
    (-0.6, "C+"), (-0.9, "C"), (-1.25, "C-"),
    (-1.6, "D+"), (-2.0, "D"),
]


# --- folding the capture into a pick table ---------------------------------

@dataclass(frozen=True)
class BidRecord:
    team_id: int
    amount: int
    clock_reset_ms: int
    clock_remaining_ms: int
    ts: float


@dataclass
class PickRecord:
    player_id: int
    team_id: int | None = None
    price: int | None = None
    sold_ts: float | None = None
    nominator_id: int | None = None
    nom_ts: float | None = None
    bids: list[BidRecord] = field(default_factory=list)
    from_init_only: bool = False


@dataclass(frozen=True)
class ChatMsg:
    team_id: int
    sent_ms: int
    text: str


@dataclass
class FoldResult:
    picks: dict[int, PickRecord]
    chats: list[ChatMsg]
    autodraft_events: list[tuple[float, int, bool]]
    left_events: list[tuple[float, int, int]]


def fold_logs(paths: Sequence[Path]) -> FoldResult:
    """Replay one or more captures, in timestamp order, into a per-player
    pick table plus the room's chat and connection events.

    Each player sells at most once, so player_id is a stable key across
    however many capture files the draft spans (the room disconnects and
    reconnects, splitting the log). SOLD carries the authoritative team and
    price; the preceding NOMINATION/BID stream is bonus detail that a given
    capture may not have (a reconnect's own INIT frame back-fills any player
    sold before that capture started, but with no bid history)."""
    frames: list[tuple[float, str]] = []
    for path in paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("dir", "receive") != "receive":
                continue
            frames.append((row["ts"], row["msg"]))
    frames.sort(key=lambda row: row[0])

    picks: dict[int, PickRecord] = {}
    chats: list[ChatMsg] = []
    seen_chat_keys: set[tuple[int, int, str]] = set()
    autodraft_events: list[tuple[float, int, bool]] = []
    left_events: list[tuple[float, int, int]] = []
    last_init: draft_ws.InitState | None = None
    current_nominator: int | None = None

    for ts, msg in frames:
        event = draft_ws.parse_frame(msg)
        if isinstance(event, draft_ws.Nomination):
            current_nominator = event.team_id
        elif isinstance(event, draft_ws.Bid):
            rec = picks.setdefault(event.player_id, PickRecord(event.player_id))
            if not rec.bids:
                rec.nominator_id = current_nominator
                rec.nom_ts = ts
            rec.bids.append(BidRecord(event.team_id, event.amount, event.clock_reset_ms,
                                       event.clock_remaining_at_bid_ms, ts))
        elif isinstance(event, draft_ws.Sold):
            rec = picks.setdefault(event.player_id, PickRecord(event.player_id))
            rec.team_id = event.team_id
            rec.price = event.price
            rec.sold_ts = ts
        elif isinstance(event, draft_ws.Chat):
            key = (event.team_id, event.sent_ms, event.text)
            if key not in seen_chat_keys:
                seen_chat_keys.add(key)
                chats.append(ChatMsg(event.team_id, event.sent_ms, event.text))
        elif isinstance(event, draft_ws.Init):
            state = draft_ws.parse_init_state(event.blob)
            if state is not None:
                last_init = state
        elif isinstance(event, draft_ws.Autodraft):
            autodraft_events.append((ts, event.team_id, event.enabled))
        elif isinstance(event, draft_ws.Left):
            left_events.append((ts, event.team_id, event.flag))

    # A reconnect's INIT carries the full pick table, so it back-fills any
    # player sold before this run of captures started -- team and price
    # only, since there is no bid history to recover for them.
    if last_init is not None:
        for init_pick in last_init.picks:
            rec = picks.get(init_pick.player_id)
            if rec is None or rec.team_id is None:
                picks[init_pick.player_id] = PickRecord(
                    init_pick.player_id, team_id=init_pick.team_id,
                    price=init_pick.price, from_init_only=True,
                )

    resolved = {pid: rec for pid, rec in picks.items() if rec.team_id is not None}
    chats.sort(key=lambda c: c.sent_ms)
    return FoldResult(resolved, chats, autodraft_events, left_events)


def build_rosters(fold: FoldResult, resolver: draft_sync.PlayerResolver
                   ) -> dict[str, list[draft_state.Purchase]]:
    rosters: dict[str, list[draft_state.Purchase]] = defaultdict(list)
    for player_id, rec in fold.picks.items():
        name, position = resolver.resolve(player_id)
        team = config.TEAMS.get(rec.team_id, f"TEAM{rec.team_id}")
        rosters[team].append(
            draft_state.Purchase(name, position, rec.price, team, espn_pick_id=player_id)
        )
    return rosters


# --- grading -----------------------------------------------------------

def projected_points(purchase: draft_state.Purchase, vals_by_id: dict) -> float:
    v = vals_by_id.get(purchase.espn_pick_id)
    return v.projected_points if v else 0.0


def compute_risk(purchases: list[draft_state.Purchase], slots: list,
                  vals_by_id: dict) -> tuple[float, list[str]]:
    """Roster-construction deductions: thin QB depth in a 2QB league with no
    in-season waiver replacement, bench short of config.ROSTER_TARGETS,
    wasted spend on a second K or D/ST, and a bye-week collision between a
    team's two starters at the same position."""
    counts = Counter(p.position for p in purchases)
    flags: list[str] = []
    score = 0.0

    qb = counts.get("QB", 0)
    if qb < 3:
        flags.append(f"only {qb} QB{'s' if qb != 1 else ''} rostered "
                      "(this league needs 3 -- the in-season QB waiver wire is empty)")
        score -= 2 * (3 - qb)

    for pos in ("RB", "WR", "TE"):
        short = max(0, config.ROSTER_TARGETS[pos] - counts.get(pos, 0))
        if short:
            flags.append(f"{short} short of the {config.ROSTER_TARGETS[pos]}-{pos} bench target")
            score -= short

    for pos in ("K", "D/ST"):
        extra = max(0, counts.get(pos, 0) - config.ROSTER_TARGETS[pos])
        if extra:
            flags.append(f"{counts[pos]} {pos}s drafted (only {config.ROSTER_TARGETS[pos]} needed)")
            score -= extra

    by_slot_label: dict[str, list[draft_state.Purchase]] = defaultdict(list)
    for label, pick in slots:
        if label in ("QB", "RB", "WR") and pick is not None:
            by_slot_label[label].append(pick)
    for label, starters in by_slot_label.items():
        byes = [vals_by_id[p.espn_pick_id].bye for p in starters
                if p.espn_pick_id in vals_by_id and vals_by_id[p.espn_pick_id].bye is not None]
        if len(byes) >= 2 and len(set(byes)) < len(byes):
            dupe = statistics.mode(byes)
            flags.append(f"both starting {label}s are on bye week {dupe}")
            score -= 1

    return score, flags


@dataclass
class TeamGrade:
    team: str
    spent: int
    n_picks: int
    positions: Counter
    sheet_surplus: int
    market_surplus: float
    starter_points: float
    bench_points: float
    risk_score: float
    risk_flags: list[str]
    best_buy: tuple[str, int] | None
    worst_buy: tuple[str, int] | None
    z: dict[str, float] = field(default_factory=dict)
    blended: float = 0.0
    grade: str = "?"


def letter_grade(z: float) -> str:
    for threshold, label in GRADE_BANDS:
        if z >= threshold:
            return label
    return "F"


def grade_teams(rosters: dict[str, list[draft_state.Purchase]],
                 vals_by_id: dict) -> list[TeamGrade]:
    rows = []
    for team, purchases in rosters.items():
        spent = sum(p.price for p in purchases)
        edges = []
        sheet_surplus = 0
        market_surplus = 0.0
        for p in purchases:
            v = vals_by_id.get(p.espn_pick_id)
            sheet_surplus += (v.value if v else 0) - p.price
            market_surplus += (v.espn_avg if v and v.espn_avg is not None else 0.0) - p.price
            edges.append((p.player, (v.value if v else 0) - p.price))
        edges.sort(key=lambda e: -e[1])

        slots = draft_state.lineup_slots(purchases, lambda p: projected_points(p, vals_by_id))
        starter_points = sum(projected_points(p, vals_by_id) for label, p in slots
                              if label != "BE" and p is not None)
        bench_points = sum(projected_points(p, vals_by_id) for label, p in slots
                            if label == "BE" and p is not None)
        risk_score, risk_flags = compute_risk(purchases, slots, vals_by_id)

        rows.append(TeamGrade(
            team=team, spent=spent, n_picks=len(purchases),
            positions=Counter(p.position for p in purchases),
            sheet_surplus=sheet_surplus, market_surplus=market_surplus,
            starter_points=starter_points, bench_points=bench_points,
            risk_score=risk_score, risk_flags=risk_flags,
            best_buy=edges[0] if edges else None, worst_buy=edges[-1] if edges else None,
        ))

    for axis, attr in (("sheet", "sheet_surplus"), ("market", "market_surplus"),
                        ("points", "starter_points"), ("risk", "risk_score")):
        xs = [getattr(r, attr) for r in rows]
        mean = statistics.fmean(xs)
        std = statistics.pstdev(xs) or 1.0
        for r in rows:
            r.z[axis] = (getattr(r, attr) - mean) / std

    for r in rows:
        r.blended = sum(AXIS_WEIGHTS[axis] * r.z[axis] for axis in AXIS_WEIGHTS)
        r.grade = letter_grade(r.blended)

    rows.sort(key=lambda r: -r.blended)
    return rows


# --- highlights and lowlights -------------------------------------------

def bidding_wars(fold: FoldResult, resolver: draft_sync.PlayerResolver, top_n: int = 8):
    rows = []
    for pid, rec in fold.picks.items():
        if not rec.bids:
            continue
        distinct = len({b.team_id for b in rec.bids})
        if distinct < 2:
            continue
        others = [b for b in rec.bids if b.team_id != rec.team_id]
        runner_amt = max((b.amount for b in others), default=0)
        name, _ = resolver.resolve(pid)
        rows.append({
            "name": name, "winner": config.TEAMS.get(rec.team_id, f"TEAM{rec.team_id}"),
            "price": rec.price, "distinct_bidders": distinct,
            "margin": rec.price - runner_amt,
        })
    rows.sort(key=lambda r: (-r["distinct_bidders"], -r["price"]))
    return rows[:top_n]


def near_misses(fold: FoldResult) -> Counter:
    counts = Counter()
    for rec in fold.picks.values():
        others = [b for b in rec.bids if b.team_id != rec.team_id]
        if not others:
            continue
        runner = max(others, key=lambda b: b.amount)
        counts[runner.team_id] += 1
    return counts


def nomination_stats(fold: FoldResult) -> tuple[Counter, Counter]:
    made, won = Counter(), Counter()
    for rec in fold.picks.values():
        if rec.nominator_id is None:
            continue
        made[rec.nominator_id] += 1
        if rec.team_id == rec.nominator_id:
            won[rec.nominator_id] += 1
    return made, won


def bid_volume(fold: FoldResult) -> Counter:
    counts = Counter()
    for rec in fold.picks.values():
        for b in rec.bids:
            counts[b.team_id] += 1
    return counts


def late_bid_share(fold: FoldResult) -> dict[int, tuple[int, int]]:
    """(late, total) bids per team -- late meaning the bid landed with the
    clock already floored at LATE_BID_CLOCK_MS, not an opening nomination."""
    late = Counter()
    total = Counter()
    for rec in fold.picks.values():
        for b in rec.bids:
            total[b.team_id] += 1
            if b.clock_remaining_ms == LATE_BID_CLOCK_MS:
                late[b.team_id] += 1
    return {team: (late[team], total[team]) for team in total}


def sale_durations(fold: FoldResult) -> list[float]:
    return sorted(rec.sold_ts - rec.nom_ts for rec in fold.picks.values()
                  if rec.sold_ts is not None and rec.nom_ts is not None)


def spend_pace(fold: FoldResult) -> tuple[list[tuple[int, int, int]], int, int]:
    """Cumulative dollars spent in 20-pick blocks, using only picks with a
    known sale timestamp (a capture that starts mid-draft is missing the
    ones its own reconnect INIT back-filled -- reported as reduced coverage
    rather than mislabeled pick numbers)."""
    timestamped = sorted(
        (rec.sold_ts, rec.price) for rec in fold.picks.values()
        if rec.sold_ts is not None and rec.price is not None
    )
    blocks = []
    running = 0
    for start in range(0, len(timestamped), 20):
        block = timestamped[start:start + 20]
        running += sum(price for _, price in block)
        blocks.append((start + 1, start + len(block), running))
    return blocks, len(timestamped), len(fold.picks)


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, LEAGUE_TZ).strftime("%-I:%M:%S %p")


# --- rendering -----------------------------------------------------------

def render_markdown(fold: FoldResult, rosters: dict[str, list[draft_state.Purchase]],
                     grades: list[TeamGrade], vals_by_id: dict, log_names: list[str]) -> str:
    lines: list[str] = []
    total_spent = sum(g.spent for g in grades)
    total_picks = sum(g.n_picks for g in grades)

    lines.append("# 2026 draft recap")
    lines.append("")
    lines.append(f"Built from {', '.join(log_names)} via `scripts/draft_recap.py`. "
                  f"{total_picks} picks, ${total_spent} of ${config.TOTAL_LEAGUE_BUDGET} spent.")
    lines.append("")
    lines.append("Grading purely against our own Sheet is circular -- we bid to that board, "
                  "so it always scores us best. Every team gets four independent reads instead: "
                  "surplus against our Sheet, surplus against ESPN's own market average, "
                  "the projected points of the best legal starting lineup, and roster "
                  "construction risk. The blended grade weights them "
                  f"{int(AXIS_WEIGHTS['sheet']*100)}/{int(AXIS_WEIGHTS['market']*100)}/"
                  f"{int(AXIS_WEIGHTS['points']*100)}/{int(AXIS_WEIGHTS['risk']*100)} "
                  "(sheet/market/points/risk) -- the per-axis numbers are printed so any "
                  "argument about the weighting is checkable against the raw numbers.")
    lines.append("")

    lines.append("## Scorecard")
    lines.append("")
    lines.append("| Grade | Team | Spent | Sheet | Market | Starters pts | Risk |")
    lines.append("|---|---|---|---|---|---|---|")
    for g in grades:
        lines.append(f"| {g.grade} | {g.team} | ${g.spent} | {g.sheet_surplus:+d} | "
                      f"{g.market_surplus:+.0f} | {g.starter_points:.0f} | {g.risk_score:+.0f} |")
    lines.append("")

    lines.append("## Team by team")
    lines.append("")
    for g in grades:
        lines.append(f"### {g.team} -- {g.grade}")
        lines.append("")
        pos_str = " ".join(f"{pos}{g.positions[pos]}" for pos in
                            ("QB", "RB", "WR", "TE", "D/ST", "K") if g.positions.get(pos))
        lines.append(f"${g.spent} spent, {g.n_picks} picks ({pos_str}). "
                      f"{g.starter_points:.0f} projected starting points, "
                      f"{g.bench_points:.0f} on the bench.")
        if g.best_buy:
            lines.append(f"- Best buy: {g.best_buy[0]} ({g.best_buy[1]:+d} vs Sheet)")
        if g.worst_buy and g.worst_buy != g.best_buy:
            lines.append(f"- Worst buy: {g.worst_buy[0]} ({g.worst_buy[1]:+d} vs Sheet)")
        if g.risk_flags:
            for flag in g.risk_flags:
                lines.append(f"- {flag}")
        else:
            lines.append("- No construction flags.")
        lines.append("")

    lines.append("## Bidding wars")
    lines.append("")
    lines.append("Deepest contests, by number of distinct bidders and final price.")
    lines.append("")
    lines.append("| Player | Winner | Price | Bidders | Margin |")
    lines.append("|---|---|---|---|---|")
    for row in bidding_wars(fold, draft_sync.PlayerResolver(VALUES_PATH)):
        lines.append(f"| {row['name']} | {row['winner']} | ${row['price']} | "
                      f"{row['distinct_bidders']} | +${row['margin']} |")
    lines.append("")

    misses = near_misses(fold)
    made, won = nomination_stats(fold)
    volume = bid_volume(fold)
    late = late_bid_share(fold)
    uncontested = sum(1 for rec in fold.picks.values()
                       if rec.bids and len({b.team_id for b in rec.bids}) == 1)

    lines.append("## Nomination and bidding behavior")
    lines.append("")
    lines.append(f"{uncontested} of {sum(1 for r in fold.picks.values() if r.bids)} "
                  "bid-covered picks drew no rival bid at all.")
    lines.append("")
    lines.append("| Team | Nominated | Won own nom. | Bids placed | Runner-up | Late bids |")
    lines.append("|---|---|---|---|---|---|")
    for team_id, label in config.TEAMS.items():
        if label not in rosters:
            continue
        late_n, late_total = late.get(team_id, (0, 0))
        late_pct = f"{late_n}/{late_total}" if late_total else "-"
        lines.append(f"| {label} | {made.get(team_id, 0)} | {won.get(team_id, 0)} | "
                      f"{volume.get(team_id, 0)} | {misses.get(team_id, 0)} | {late_pct} |")
    lines.append("")

    durations = sale_durations(fold)
    if durations:
        lines.append(f"Sale duration (nomination to SOLD): min {durations[0]:.0f}s, "
                      f"median {durations[len(durations)//2]:.0f}s, max {durations[-1]:.0f}s.")
        lines.append("")

    if fold.autodraft_events or fold.left_events:
        lines.append("## Disconnects and autodraft")
        lines.append("")
        for ts, team_id, enabled in fold.autodraft_events:
            state = "enabled" if enabled else "disabled"
            lines.append(f"- {fmt_ts(ts)}: {config.TEAMS.get(team_id, team_id)} "
                          f"autodraft {state}")
        if fold.left_events:
            left_counts = Counter(team_id for _ts, team_id, _flag in fold.left_events)
            summary = ", ".join(f"{config.TEAMS.get(t, t)} {n}" for t, n in
                                 sorted(left_counts.items(), key=lambda kv: -kv[1]))
            lines.append(f"- {len(fold.left_events)} reconnects total: {summary}")
        lines.append("")

    blocks, covered, total = spend_pace(fold)
    lines.append("## Spend pace")
    lines.append("")
    if covered < total:
        lines.append(f"Covers {covered} of {total} picks with a known sale time -- "
                      "the rest were back-filled from a reconnect's INIT frame with no "
                      "timestamp, so they're excluded here rather than mislabeled.")
        lines.append("")
    lines.append("| Picks | Cumulative $ | % of league money |")
    lines.append("|---|---|---|")
    for start, end, running in blocks:
        pct = running / config.TOTAL_LEAGUE_BUDGET * 100
        lines.append(f"| {start}-{end} | ${running} | {pct:.1f}% |")
    lines.append("")
    lines.append("Compare against `docs/notes/auction-room-history.md`'s 2023-2025 curves "
                  "(63-72% of league money gone after the first 40 picks).")
    lines.append("")

    if fold.chats:
        lines.append("## Chat")
        lines.append("")
        by_team = Counter(c.team_id for c in fold.chats)
        lines.append(", ".join(f"{config.TEAMS.get(t, t)} {n}" for t, n in
                                sorted(by_team.items(), key=lambda kv: -kv[1])))
        lines.append("")
        for c in fold.chats:
            when = datetime.fromtimestamp(c.sent_ms / 1000, LEAGUE_TZ).strftime("%-I:%M %p")
            lines.append(f"- **{config.TEAMS.get(c.team_id, c.team_id)}** ({when}): {c.text}")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="*", type=Path, default=None,
                         help="ws-log-*.jsonl capture(s), in any order (default: the two "
                              "captures covering the 2026 draft)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                         help=f"output path, or - for stdout (default: {DEFAULT_OUT})")
    args = parser.parse_args(argv)

    log_paths = args.logs or DEFAULT_LOGS
    for path in log_paths:
        if not path.exists():
            parser.error(f"no such capture: {path}")

    if not VALUES_PATH.exists():
        parser.error(f"no {VALUES_PATH} -- run scripts/build_values.py first")
    vals = [values.Valuation(**row) for row in json.loads(VALUES_PATH.read_text())]
    vals_by_id = {v.espn_id: v for v in vals if v.espn_id is not None}
    resolver = draft_sync.PlayerResolver(VALUES_PATH)

    fold = fold_logs(log_paths)
    rosters = build_rosters(fold, resolver)
    grades = grade_teams(rosters, vals_by_id)
    report = render_markdown(fold, rosters, grades, vals_by_id,
                              [p.name for p in log_paths])

    if str(args.out) == "-":
        print(report)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n")
        print(f"Wrote {args.out} ({len(fold.picks)} picks, {len(grades)} teams).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
