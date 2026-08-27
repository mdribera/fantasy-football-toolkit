"""Unit tests for the pure live-auction logic in scripts/auction.py:
WsAuctionPointer, apply_ws_event, clock_milestone, and evaluate_bid. None
of this touches a socket or a thread -- DraftRoomClient's I/O is covered
separately in tests/test_draft_ws_client.py.
"""

from __future__ import annotations

import auction
from ff import draft_ws


def test_bid_event_sets_pointer():
    pointer = auction.WsAuctionPointer(nominating_team=6)
    updated = auction.apply_ws_event(pointer, draft_ws.Bid(10, 3915511, 42, 25000, 12731))
    assert updated == auction.WsAuctionPointer(3915511, 42, 6)


def test_clock_state_2_sets_pointer():
    pointer = auction.WsAuctionPointer()
    event = draft_ws.Clock(2, 12982, high_bid_team=11, player_id=3915511, high_bid_amount=41)
    updated = auction.apply_ws_event(pointer, event)
    assert updated == auction.WsAuctionPointer(3915511, 41, None)


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
