#!/usr/bin/env python3
"""Validate the valuation pipeline without needing ESPN credentials.

Builds a synthetic projection curve shaped like a real NFL season, prices it
under this league's 2QB rules, then re-prices the identical player pool under
1QB rules. The difference between the two is the entire thesis of this project,
so it is worth being able to demonstrate on demand.

The projections here are synthetic and exist only to exercise the math. Real
values come from scripts/build_values.py.
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table

from ff import config, draft_state, draft_sync, draft_ws, scoring, values

WS_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data" / "ws-live-test.jsonl"
WS_HAR_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data" / "ws-from-har.jsonl"

console = Console()

# (position, count, top-end points, decay) -- shaped to resemble real positional
# scoring curves in full PPR.
CURVES = [
    ("QB", 40, 400, 0.985),
    ("RB", 70, 330, 0.975),
    ("WR", 90, 320, 0.982),
    ("TE", 30, 260, 0.955),
    ("K", 20, 150, 0.990),
    ("D/ST", 20, 140, 0.975),
]


def synthetic_pool() -> list[dict]:
    pool = []
    for position, count, top, decay in CURVES:
        for i in range(count):
            pool.append(
                {
                    "name": f"{position}{i + 1}",
                    "position": position,
                    "pro_team": "SYN",
                    "league_points": round(top * (decay ** i), 1),
                }
            )
    return pool


def price_with_replacement(pool, qb_replacement: int):
    original = config.REPLACEMENT.QB
    object.__setattr__(config.REPLACEMENT, "QB", qb_replacement)
    try:
        return values.compute_values(pool)
    finally:
        object.__setattr__(config.REPLACEMENT, "QB", original)


def top_n(vals, position, n=5):
    return [v for v in vals if v.position == position][:n]


def _fake_snapshot(picks: list[dict]) -> dict:
    return {"drafted": False, "inProgress": True, "picks": picks}


def _pick(pick_id, team_id, player_id, bid) -> dict:
    return {"id": pick_id, "overallPickNumber": pick_id, "roundId": 1,
            "roundPickNumber": pick_id, "nominatingTeamId": team_id,
            "teamId": team_id, "playerId": player_id, "bidAmount": bid}


def test_draft_sync() -> None:
    """Exercise the draft-day sync pipeline with no network and no credentials.

    This is the regression test for scripts/draft_sync.py --replay: dedup by
    ESPN's pick id, unresolved players falling back to a placeholder instead
    of crashing, and DraftState.record_pick's idempotency.
    """
    console.print("[bold]Draft sync[/bold]")

    unfilled = _pick(1, -1, -1, 0)
    filled_known = _pick(2, config.MY_TEAM_ID, 4429795, 46)   # Jahmyr Gibbs, from values.json
    filled_unknown = _pick(3, 7, 999999999, 3)                # not on any board

    picks = draft_sync.completed([unfilled, filled_known, filled_unknown])
    assert [p["id"] for p in picks] == [2, 3], "unfilled slots must be excluded"

    no_creds = config.EspnCredentials(league_id="", swid="", espn_s2="", team_id="")
    resolver = draft_sync.PlayerResolver(
        Path(__file__).resolve().parents[1] / "data" / "values.json", cred=no_creds
    )
    name, position = resolver.resolve(4429795)
    assert name == "Jahmyr Gibbs" and position == "RB", "known player should resolve off the board"

    # Forcing empty credentials means the live fallback lookup can't reach
    # ESPN even if this machine's .env is configured -- it should degrade to
    # a placeholder, not raise, exactly as it would for an actual outage.
    name, position = resolver.resolve(999999999)
    assert name == "ESPN#999999999" and position == "?", "unknown player must degrade, not crash"

    feed = draft_sync.DraftFeed(source=lambda: {}, resolver=resolver)
    first = feed.poll_snapshot(_fake_snapshot([unfilled, filled_known]))
    assert [p.espn_pick_id for p in first] == [2]
    second = feed.poll_snapshot(_fake_snapshot([unfilled, filled_known, filled_unknown]))
    assert [p.espn_pick_id for p in second] == [3], "already-seen pick ids must not resurface"

    # An explicit scratch path -- this test must never touch the real
    # data/draft-state.json, since DraftState.save() writes on every record.
    scratch_path = Path(__file__).resolve().parents[1] / "data" / "cache" / "draft-state-selftest.json"
    state = draft_state.DraftState(state_path=scratch_path)
    assert state.all_teams() == sorted(config.TEAMS.values()), \
        "all_teams should be seeded from config.TEAMS, not just recorded purchases"

    recorded = state.record_pick("Jahmyr Gibbs", "RB", 46, "ME", espn_pick_id=2)
    duplicate = state.record_pick("Jahmyr Gibbs", "RB", 46, "ME", espn_pick_id=2)
    assert recorded and not duplicate, "re-polling the same pick id must not double-record"
    assert state.spent_by("ME") == 46

    state.undo()
    resurfaced = state.record_pick("Jahmyr Gibbs", "RB", 46, "ME", espn_pick_id=2)
    assert not resurfaced, "a pick just undone should stay suppressed for this session"

    scratch_path.unlink(missing_ok=True)
    console.print("[green]Sync pipeline: dedup, placeholder fallback and "
                  "undo-suppression all hold.[/green]\n")


def test_draft_ws() -> None:
    """Exercise the draft-room websocket parser.

    The literal frame strings below are lifted verbatim from a genuine
    practice-draft capture (2026-08-26, see docs/draft-ws-plan.md), so this
    doubles as regression coverage against the one schema doubt that capture
    already resolved: BID's amount is field 3, not the constant field 4.
    """
    console.print("[bold]Draft websocket parser[/bold]")

    assert draft_ws.parse_frame("AUTODRAFT 6 false\n") == draft_ws.Autodraft(6, False)
    assert draft_ws.parse_frame("PASSED 6 3915511 false\n") == draft_ws.Passed(6, 3915511, False)
    assert draft_ws.parse_frame("BID 10 3915511 42 25000 12731\n") == \
        draft_ws.Bid(10, 3915511, 42, 25000, 12731)
    assert draft_ws.parse_frame("CLOCK 0 28068\n") == draft_ws.Clock(0, 28068)
    assert draft_ws.parse_frame("CLOCK 1 25000 11\n") == draft_ws.Clock(1, 25000, nominating_team=11)
    assert draft_ws.parse_frame("CLOCK 2 12982 11 3915511 41\n") == \
        draft_ws.Clock(2, 12982, high_bid_team=11, player_id=3915511, high_bid_amount=41)
    assert draft_ws.parse_frame("CLOCK 3 1248\n") == draft_ws.Clock(3, 1248)
    assert draft_ws.parse_frame("SOLD 7 3915511 1 43 0\n") == draft_ws.Sold(7, 3915511, 1, 43, 0)
    assert draft_ws.parse_frame("NOMINATION 1 25000\n") == draft_ws.Nomination(1, 25000)
    assert draft_ws.parse_frame("AUTOSUGGEST 4431452\n") == draft_ws.AutoSuggest(4431452)
    assert draft_ws.parse_frame("TOKEN 1:1721228630:6:{REDACTED-SWID}:REDACTED-SESSION\n") == \
        draft_ws.Token(1, 1721228630, 6, "{REDACTED-SWID}", "REDACTED-SESSION")
    assert draft_ws.parse_frame("INIT abc123\n") == draft_ws.Init("abc123")
    assert draft_ws.parse_frame("JOINED 6 {REDACTED}\n") == draft_ws.Joined(6, "{REDACTED}")
    assert draft_ws.parse_frame("LEFT 6 {REDACTED} 1\n") == draft_ws.Left(6, "{REDACTED}", 1)
    assert draft_ws.parse_frame("PONG PING%201787783293012\n") == draft_ws.Pong("PING%201787783293012")
    assert draft_ws.parse_frame("BID_ACK 6 4426348 56\n") == draft_ws.BidAck(6, 4426348, 56)
    assert draft_ws.parse_frame("DRAFT_LIST 3918298 3916387\n") == draft_ws.DraftList((3918298, 3916387))
    assert draft_ws.parse_frame("STATE 1\n") == draft_ws.State(1)

    # Client-to-server frames, confirmed from a HAR capture -- parsed here for
    # decoder coverage only, never sent.
    assert draft_ws.parse_frame("PING PING%201787783293012\n") == draft_ws.Ping("PING%201787783293012")
    assert draft_ws.parse_frame("BID 4426348 56\n") == draft_ws.BidCommand(4426348, 56)
    assert draft_ws.parse_frame("NOMINATE 4426502 1\n") == draft_ws.Nominate(4426502, 1)
    assert draft_ws.parse_frame("PRENOMINATE 3918298 1 3916387 1\n") == \
        draft_ws.Prenominate(((3918298, 1), (3916387, 1)))
    assert draft_ws.parse_frame("AUTO_NOMINATION 4262921\n") == draft_ws.AutoNomination(4262921)

    short_bid = draft_ws.parse_frame("BID 1 2 3\n")
    assert isinstance(short_bid, draft_ws.WsError), "a 3-field BID must not raise or silently mis-parse"
    unknown_kind = draft_ws.parse_frame("FOOBAR 1 2 3\n")
    assert isinstance(unknown_kind, draft_ws.WsError), "an unrecognized frame kind must not raise"

    if not WS_FIXTURE_PATH.exists():
        console.print("[yellow]data/ws-live-test.jsonl not present locally "
                       "(gitignored) -- skipping the full-capture replay check.[/yellow]")
    else:
        events = [draft_ws.parse_frame(msg) for msg in draft_ws.iter_frames(WS_FIXTURE_PATH)]
        errors = [e for e in events if isinstance(e, draft_ws.WsError)]
        assert not errors, f"every frame in a real capture must parse: {errors}"

        sales = [e for e in events if isinstance(e, draft_ws.Sold)]
        assert len(sales) == 1 and sales[0].price == 43, \
            "the one completed sale in the fixture must match ESPN's on-screen price"

        console.print(f"[green]{len(events)} frames parsed clean against the real capture, "
                      f"{len(sales)} sale(s) matched against the recap.[/green]")

    if not WS_HAR_FIXTURE_PATH.exists():
        console.print("[yellow]data/ws-from-har.jsonl not present locally "
                       "(gitignored, run scripts/draft_ws.py --from-har to produce it) -- "
                       "skipping the HAR-derived decode check.[/yellow]\n")
        return

    # Unlike the fixture above, this one has no independent recap to check
    # prices against -- it's a mock draft, not cross-referenced against
    # ESPN's UI. The check here is that the decoder covers 100% of a real,
    # much larger, bidirectional capture: every send and receive frame kind
    # produced across a full mock draft, not just the kinds one earlier
    # session happened to exercise.
    all_events = [draft_ws.parse_frame(msg)
                  for msg in draft_ws.iter_frames(WS_HAR_FIXTURE_PATH, include_sent=True)]
    all_errors = [e for e in all_events if isinstance(e, draft_ws.WsError)]
    assert not all_errors, f"every frame in the HAR capture must parse: {all_errors}"

    har_sales = [e for e in all_events if isinstance(e, draft_ws.Sold)]
    assert len(har_sales) > 1, "expected multiple completed sales in a full mock draft"

    console.print(f"[green]{len(all_events)} frames (both directions) parsed clean against "
                  f"the HAR capture, {len(har_sales)} sale(s) seen.[/green]\n")


def main() -> int:
    test_draft_sync()
    test_draft_ws()

    console.print("[bold]Scoring engine[/bold]")
    qb = scoring.score_offense(
        {"passing_yards": 4500, "passing_tds": 35, "interceptions": 10,
         "rushing_yards": 300, "rushing_tds": 3}
    )
    wr = scoring.score_offense(
        {"receptions": 90, "receiving_yards": 1200, "receiving_tds": 8}
    )
    dst = scoring.score_dst(
        {"sacks": 3, "interceptions": 1, "points_allowed": 17, "yards_allowed": 280}
    )
    console.print(f"  4500/35/10 QB + 300 rush yds : {qb:.1f} pts")
    console.print(f"  90 rec / 1200 yds / 8 TD WR  : {wr:.1f} pts")
    console.print(f"  3 sack, 1 INT, 17 PA, 280 YA : {dst:.1f} pts (one game)\n")

    pool = synthetic_pool()

    two_qb = price_with_replacement(pool, config.REPLACEMENT.QB)   # 30
    one_qb = price_with_replacement(pool, 14)                      # 10 tm x ~1.4

    console.print(f"[bold]Same player pool, priced two ways[/bold] "
                  f"(${config.TOTAL_LEAGUE_BUDGET} league-wide, "
                  f"${config.BIDDABLE_SURPLUS} biddable)\n")

    table = Table()
    table.add_column("Player")
    table.add_column("Pos", justify="center")
    table.add_column("Proj", justify="right")
    table.add_column("2QB value", justify="right", style="bold green")
    table.add_column("1QB value", justify="right", style="dim")
    table.add_column("Delta", justify="right")

    one_lookup = {v.name: v.value for v in one_qb}
    shown = (top_n(two_qb, "QB", 4) + top_n(two_qb, "RB", 3)
             + top_n(two_qb, "WR", 3) + top_n(two_qb, "TE", 2))
    for v in shown:
        other = one_lookup.get(v.name, 0)
        delta = v.value - other
        style = "green" if delta > 0 else "red" if delta < 0 else "dim"
        table.add_row(v.name, v.position, f"{v.projected_points:.0f}",
                      f"${v.value}", f"${other}", f"[{style}]{delta:+d}[/{style}]")
    console.print(table)

    def spend(vals, position):
        drafted = sorted([v for v in vals if v.position == position],
                         key=lambda v: v.value, reverse=True)
        return sum(v.value for v in drafted[:config.NUM_TEAMS * 3]) if position == "QB" \
            else sum(v.value for v in drafted[:config.NUM_TEAMS * 4])

    console.print("\n[bold]League-wide spend by position[/bold]")
    share = Table()
    share.add_column("Pos")
    share.add_column("2QB", justify="right")
    share.add_column("1QB", justify="right")
    for position in ("QB", "RB", "WR", "TE"):
        share.add_row(position, f"${spend(two_qb, position)}", f"${spend(one_qb, position)}")
    console.print(share)

    # The claim being tested is about budget conservation, not any one price:
    # the league spends the same $2,000 either way, so money that flows into
    # quarterbacks has to flow out of the skill positions. Absolute prices here
    # depend on the synthetic decay constants above and mean nothing; the
    # direction and rough magnitude of the shift is the real result.
    qb_shift = spend(two_qb, "QB") / max(spend(one_qb, "QB"), 1)
    skill_shift = (
        (spend(two_qb, "RB") + spend(two_qb, "WR"))
        / max(spend(one_qb, "RB") + spend(one_qb, "WR"), 1)
    )
    console.print(f"\nQB spend multiple, 2QB vs 1QB : [bold]{qb_shift:.2f}x[/bold]")
    console.print(f"RB+WR spend multiple           : [bold]{skill_shift:.2f}x[/bold]")

    assert qb_shift > 2.0, "2QB format must pull far more budget into QB"
    assert skill_shift < 0.95, "that budget must come out of RB/WR"
    console.print("[green]Budget conservation confirmed: "
                  "QB dollars come directly out of the RB/WR market.[/green]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
