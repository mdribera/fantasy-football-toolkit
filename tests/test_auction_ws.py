"""Unit tests for the pure live-auction logic in scripts/auction.py:
WsAuctionPointer, apply_ws_event, clock_milestone, and evaluate_bid. None
of this touches a socket or a thread -- DraftRoomClient's I/O is covered
separately in tests/test_draft_ws_client.py.
"""

from __future__ import annotations

import auction
from ff import draft_state, draft_ws, values


def test_bid_event_sets_pointer():
    pointer = auction.WsAuctionPointer(nominating_team=6)
    updated = auction.apply_ws_event(pointer, draft_ws.Bid(10, 3915511, 42, 25000, 12731))
    assert updated == auction.WsAuctionPointer(3915511, 42, 6)


def test_clock_state_2_sets_pointer():
    pointer = auction.WsAuctionPointer()
    event = draft_ws.Clock(2, 12982, high_bid_team=11, player_id=3915511, high_bid_amount=41)
    updated = auction.apply_ws_event(pointer, event)
    assert updated == auction.WsAuctionPointer(3915511, 41, None)


def test_nomination_event_sets_nominating_team_and_clears_player():
    pointer = auction.WsAuctionPointer(3915511, 43, None)
    updated = auction.apply_ws_event(pointer, draft_ws.Nomination(7, 25000))
    assert updated == auction.WsAuctionPointer(None, 0, 7)


def test_clock_state_1_clears_player_and_sets_nominating_team():
    pointer = auction.WsAuctionPointer(3915511, 43, None)
    event = draft_ws.Clock(1, 25000, nominating_team=7)
    updated = auction.apply_ws_event(pointer, event)
    assert updated == auction.WsAuctionPointer(None, 0, 7)


def test_sold_resets_pointer():
    pointer = auction.WsAuctionPointer(3915511, 43, None)
    updated = auction.apply_ws_event(pointer, draft_ws.Sold(7, 3915511, 1, 43, 0))
    assert updated == auction.WsAuctionPointer(None, 0, None)


def test_clock_state_3_leaves_pointer_unchanged():
    pointer = auction.WsAuctionPointer(None, 0, None)
    updated = auction.apply_ws_event(pointer, draft_ws.Clock(3, 1248))
    assert updated is pointer


def test_unrelated_event_leaves_pointer_unchanged():
    pointer = auction.WsAuctionPointer(3915511, 43, 6)
    updated = auction.apply_ws_event(pointer, draft_ws.BidAck(6, 4426348, 56))
    assert updated is pointer


def test_clock_milestone_announces_each_threshold_once():
    announced: set[int] = set()
    assert auction.clock_milestone(11000, announced) is None
    assert auction.clock_milestone(9500, announced) == 10
    assert auction.clock_milestone(9000, announced) is None
    assert auction.clock_milestone(4800, announced) == 5
    assert auction.clock_milestone(1000, announced) is None


def test_evaluate_bid_no_amount_bids_high_plus_one():
    plan = auction.evaluate_bid([], current_high=40, my_max_bid=100, adjusted_value=45)
    assert plan == auction.BidReady(41)


def test_evaluate_bid_explicit_amount():
    plan = auction.evaluate_bid(["45"], current_high=40, my_max_bid=100, adjusted_value=45)
    assert plan == auction.BidReady(45)


def test_evaluate_bid_refuses_non_numeric():
    plan = auction.evaluate_bid(["forty"], current_high=40, my_max_bid=100, adjusted_value=45)
    assert isinstance(plan, auction.BidRefused)


def test_evaluate_bid_refuses_at_or_below_current_high():
    plan = auction.evaluate_bid(["40"], current_high=40, my_max_bid=100, adjusted_value=45)
    assert isinstance(plan, auction.BidRefused)


def test_evaluate_bid_refuses_above_max_bid():
    plan = auction.evaluate_bid(["101"], current_high=40, my_max_bid=100, adjusted_value=200)
    assert isinstance(plan, auction.BidRefused)


def test_evaluate_bid_confirms_big_jump_over_current_high():
    plan = auction.evaluate_bid(["55"], current_high=40, my_max_bid=100, adjusted_value=200)
    assert isinstance(plan, auction.BidNeedsConfirmation)
    assert plan.amount == 55


def test_evaluate_bid_confirms_far_over_sheet_value():
    plan = auction.evaluate_bid(["46"], current_high=40, my_max_bid=100, adjusted_value=20)
    assert isinstance(plan, auction.BidNeedsConfirmation)


def test_evaluate_bid_ready_when_small_jump_and_near_sheet():
    plan = auction.evaluate_bid(["41"], current_high=40, my_max_bid=100, adjusted_value=45)
    assert plan == auction.BidReady(41)


def test_evaluate_bid_ready_at_exact_jump_boundary():
    # jump == TYPO_GUARD_JUMP exactly ($10): only "more than" $10 should confirm.
    plan = auction.evaluate_bid(["50"], current_high=40, my_max_bid=100, adjusted_value=None)
    assert plan == auction.BidReady(50)


def test_evaluate_bid_ready_at_exact_sheet_multiple_boundary():
    # amount == adjusted_value * TYPO_GUARD_SHEET_MULTIPLE exactly (1.5x): only
    # "exceeds" should confirm. Keep the jump small so only the sheet-multiple
    # check is in play.
    plan = auction.evaluate_bid(["30"], current_high=25, my_max_bid=100, adjusted_value=20)
    assert plan == auction.BidReady(30)


# --- next_equivalent / bid_verdict -------------------------------------

def _val(name, position, value, tier):
    return values.Valuation(
        name=name, position=position, pro_team="", projected_points=0.0,
        replacement_points=0.0, vorp=0.0, value=value, tier=tier,
    )


BOARD = [
    _val("Bijan Robinson", "RB", 43, 2),
    _val("Kenneth Walker III", "RB", 38, 2),
    _val("Tony Pollard", "RB", 22, 3),
    _val("Justin Jefferson", "WR", 52, 1),
]


def test_next_equivalent_prefers_the_same_tier():
    match = auction.next_equivalent(BOARD, set(), "RB", 2, "Bijan Robinson")
    assert match.name == "Kenneth Walker III"


def test_next_equivalent_falls_through_to_the_next_tier_down():
    taken = {"Kenneth Walker III"}
    match = auction.next_equivalent(BOARD, taken, "RB", 2, "Bijan Robinson")
    assert match.name == "Tony Pollard"
    assert match.tier == 3


def test_next_equivalent_never_returns_a_better_tier():
    match = auction.next_equivalent(BOARD, set(), "WR", 2, None)
    assert match is None


def test_next_equivalent_returns_none_when_the_position_is_exhausted():
    taken = {"Kenneth Walker III", "Tony Pollard"}
    assert auction.next_equivalent(BOARD, taken, "RB", 2, "Bijan Robinson") is None


def test_bid_verdict_bands():
    assert auction.bid_verdict(30, 40).label == "good value"
    assert auction.bid_verdict(40, 40).label == "fair"
    assert auction.bid_verdict(50, 40).label == "pricey"
    assert auction.bid_verdict(70, 40).label == "overpaying"


def test_bid_verdict_boundaries_match_the_market_and_typo_guard_cutoffs():
    # 0.9 and 1.1 are the 'market' read's cutoffs; 1.5 is TYPO_GUARD_SHEET_MULTIPLE.
    assert auction.bid_verdict(36, 40).label == "fair"        # exactly 0.9
    assert auction.bid_verdict(44, 40).label == "fair"        # exactly 1.1
    assert auction.bid_verdict(60, 40).label == "pricey"      # exactly 1.5
    assert auction.bid_verdict(61, 40).label == "overpaying"


def test_bid_verdict_without_a_sheet_value_is_neutral():
    verdict = auction.bid_verdict(40, None)
    assert verdict.label == "unpriced"
    assert verdict.style == "dim"


def test_bid_verdict_styles_run_green_to_red():
    assert auction.bid_verdict(30, 40).style == "green"
    assert auction.bid_verdict(40, 40).style == "dim"
    assert auction.bid_verdict(50, 40).style == "yellow"
    assert auction.bid_verdict(70, 40).style == "red"


def test_evaluate_bid_refuses_when_you_already_hold_the_high():
    plan = auction.evaluate_bid([], 40, 100, 50, already_high=True)
    assert isinstance(plan, auction.BidRefused)
    assert "already hold" in plan.reason


def test_load_state_fresh_ignores_an_existing_file(tmp_path):
    path = tmp_path / "draft-state.json"
    draft_state.DraftState(
        purchases=[draft_state.Purchase("Lamar Jackson", "QB", 62, "FWD")],
        state_path=path,
    ).save()
    state = auction.load_state(fresh=True, path=path)
    assert state.purchases == []


def test_load_state_not_fresh_loads_existing_data(tmp_path):
    path = tmp_path / "draft-state.json"
    draft_state.DraftState(
        purchases=[draft_state.Purchase("Lamar Jackson", "QB", 62, "FWD")],
        state_path=path,
    ).save()
    state = auction.load_state(fresh=False, path=path)
    assert state.purchases[0].player == "Lamar Jackson"


def test_load_state_fresh_backs_up_the_existing_file(tmp_path):
    path = tmp_path / "draft-state.json"
    draft_state.DraftState(
        purchases=[draft_state.Purchase("Lamar Jackson", "QB", 62, "FWD")],
        state_path=path,
    ).save()
    auction.load_state(fresh=True, path=path)
    backup = path.with_suffix(".json.bak")
    assert backup.exists()
    assert "Lamar Jackson" in backup.read_text()


def test_load_state_fresh_without_an_existing_file_does_not_create_a_backup(tmp_path):
    path = tmp_path / "draft-state.json"
    auction.load_state(fresh=True, path=path)
    backup = path.with_suffix(".json.bak")
    assert not backup.exists()
