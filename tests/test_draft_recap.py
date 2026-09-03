"""Unit tests for scripts/draft_recap.py (T53): folding a captured draft into
a pick table, and grading the resulting rosters.
"""

from __future__ import annotations

import json
from pathlib import Path

import draft_recap
import pytest

from ff import draft_state, values

DATA = Path(__file__).resolve().parents[1] / "data"
LIVE_FIXTURE = DATA / "ws-live-test.jsonl"


def write_jsonl(path: Path, frames: list[tuple[float, str, str]]) -> None:
    """frames: (ts, dir, msg)."""
    with path.open("w") as fh:
        for ts, direction, msg in frames:
            fh.write(json.dumps({"ts": ts, "dir": direction, "msg": msg}) + "\n")


def test_nominator_matches_the_players_opening_bidder():
    """The team named in NOMINATION places the opening BID on that player --
    confirmed across all 160 picks of the real 2026 draft (see the plan's
    data-source notes). ws-live-test.jsonl's second player (4262921) is
    nominated by team 1, which then places the opening $1 bid.

    Checked directly against the raw frame stream rather than fold_logs'
    resolved (SOLD-only) pick table, since this fixture never sells that
    player -- fold_logs would drop it entirely."""
    from ff import draft_ws

    nominator = None
    opening_bidder = None
    for msg in draft_ws.iter_frames(LIVE_FIXTURE):
        event = draft_ws.parse_frame(msg)
        if isinstance(event, draft_ws.Nomination):
            nominator = event.team_id
        elif isinstance(event, draft_ws.Bid) and event.player_id == 4262921:
            opening_bidder = event.team_id
            break
    assert nominator == 1
    assert opening_bidder == nominator


def test_late_bid_share_counts_only_the_clock_floor_not_opening_bids(tmp_path):
    """clock_remaining_at_bid_ms floors at 10000 rather than counting down
    further -- confirmed against the real capture (docs/notes/ws-protocol.md)
    -- so that floor is the "landed in the final stretch" signal. An opening
    nomination's own $1 bid reads 25000 and must not count as late."""
    path = tmp_path / "synthetic.jsonl"
    write_jsonl(path, [
        (1.0, "receive", "NOMINATION 1 25000\n"),
        (1.1, "receive", "BID 1 555 1 25000 25000\n"),   # opening bid, not late
        (2.0, "receive", "BID 2 555 5 25000 10000\n"),   # late
        (2.1, "receive", "BID 1 555 6 25000 10000\n"),   # late
        (2.2, "receive", "SOLD 1 555 1 6 0\n"),
    ])
    fold = draft_recap.fold_logs([path])
    late = draft_recap.late_bid_share(fold)
    assert late[1] == (1, 2)  # team 1: 1 of its 2 bids landed late
    assert late[2] == (1, 1)  # team 2: its only bid was late


def test_chat_dedupes_across_a_reconnect(tmp_path):
    """The server replays chat history on reconnect (an INIT-triggered
    resend), so the same (team_id, sent_ms, text) shows up in both the
    original capture and the reconnect's capture. Folding both must not
    double-count it."""
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    write_jsonl(first, [
        (1.0, "receive", "CHAT 1 {REDACTED-SWID} 1000 hello+room\n"),
    ])
    write_jsonl(second, [
        (2.0, "receive", "CHAT 1 {REDACTED-SWID} 1000 hello+room\n"),  # replayed
        (2.1, "receive", "CHAT 2 {REDACTED-SWID} 2000 nice+pick\n"),   # new
    ])
    fold = draft_recap.fold_logs([first, second])
    assert len(fold.chats) == 2
    assert [c.text for c in fold.chats] == ["hello room", "nice pick"]


def make_valuation(name: str, position: str, value: int, espn_id: int,
                    projected_points: float = 100.0, espn_avg: float | None = None,
                    bye: int | None = None) -> values.Valuation:
    return values.Valuation(
        name=name, position=position, pro_team="", projected_points=projected_points,
        replacement_points=0.0, vorp=0.0, value=value, espn_id=espn_id,
        espn_avg=espn_avg if espn_avg is not None else float(value), bye=bye,
    )


def test_grade_teams_ranks_the_better_roster_first():
    """A minimal two-team draft: TEAM_A buys strictly under Sheet on every
    pick (a bargain roster) and TEAM_B buys strictly over Sheet on every
    pick -- TEAM_A must grade out ahead on both the sheet-surplus axis and
    the blended grade."""
    vals_by_id = {
        1: make_valuation("Good QB", "QB", 20, 1),
        2: make_valuation("Bad QB", "QB", 20, 2),
    }
    rosters = {
        "TEAM_A": [draft_state.Purchase("Good QB", "QB", 10, "TEAM_A", espn_pick_id=1)],
        "TEAM_B": [draft_state.Purchase("Bad QB", "QB", 30, "TEAM_B", espn_pick_id=2)],
    }
    grades = draft_recap.grade_teams(rosters, vals_by_id)
    assert [g.team for g in grades] == ["TEAM_A", "TEAM_B"]
    assert grades[0].sheet_surplus == 10   # 20 - 10
    assert grades[1].sheet_surplus == -10  # 20 - 30
    assert grades[0].blended > grades[1].blended


def test_compute_risk_flags_thin_qb_depth():
    """This league starts 2 QBs with an empty in-season waiver wire, so
    config.ROSTER_TARGETS wants 3 -- a roster with only 1 must be flagged
    and penalized, matching the -2-per-missing-QB weighting. Every other
    position is filled to its own target so only the QB deduction shows up
    in the score."""
    positions = [("QB", 1)] + [("RB", 4), ("WR", 5), ("TE", 2), ("D/ST", 1), ("K", 1)]
    vals_by_id = {}
    purchases = []
    pid = 1
    for position, count in positions:
        for i in range(count):
            name = f"{position}{i}"
            vals_by_id[pid] = make_valuation(name, position, 5, pid)
            purchases.append(draft_state.Purchase(name, position, 1, "TEAM_A", espn_pick_id=pid))
            pid += 1
    slots = draft_state.lineup_slots(
        purchases, lambda p: draft_recap.projected_points(p, vals_by_id)
    )
    score, flags = draft_recap.compute_risk(purchases, slots, vals_by_id)
    assert score == -4  # 2 missing QBs * -2
    assert any("QB" in f for f in flags)
    assert len(flags) == 1


def test_letter_grade_bands_are_ordered_and_cover_the_full_range():
    assert draft_recap.letter_grade(5.0) == "A+"
    assert draft_recap.letter_grade(0.0) == "B"
    assert draft_recap.letter_grade(-1.9) == "D"
    assert draft_recap.letter_grade(-5.0) == "F"  # below every named band
