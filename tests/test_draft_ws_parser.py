"""Unit tests for ff.draft_ws's frame parser, against two real captures:
a three-way bidding war (data/ws-live-test.jsonl) and a full bidirectional
HAR export (data/ws-from-har.jsonl). See docs/notes/ws-protocol.md for what
each fixture settles.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ff import draft_ws

DATA = Path(__file__).resolve().parents[1] / "data"
LIVE_FIXTURE = DATA / "ws-live-test.jsonl"
HAR_FIXTURE = DATA / "ws-from-har.jsonl"


def test_every_live_test_frame_parses_without_error():
    for msg in draft_ws.iter_frames(LIVE_FIXTURE):
        event = draft_ws.parse_frame(msg)
        assert not isinstance(event, draft_ws.WsError), f"{msg!r} -> {event}"


def test_every_har_frame_parses_without_error_in_either_direction():
    for msg in draft_ws.iter_frames(HAR_FIXTURE, include_sent=True):
        event = draft_ws.parse_frame(msg)
        assert not isinstance(event, draft_ws.WsError), f"{msg!r} -> {event}"


def test_clock_state_2_carries_high_bid_fields():
    event = draft_ws.parse_frame("CLOCK 2 12982 11 3915511 41\n")
    assert event == draft_ws.Clock(2, 12982, high_bid_team=11, player_id=3915511,
                                    high_bid_amount=41)


def test_clock_state_3_has_no_extra_fields():
    event = draft_ws.parse_frame("CLOCK 3 1248\n")
    assert event == draft_ws.Clock(3, 1248)


def test_clock_state_1_carries_nominating_team():
    event = draft_ws.parse_frame("CLOCK 1 25000 10\n")
    assert event == draft_ws.Clock(1, 25000, nominating_team=10)


def test_clock_state_0_is_the_pre_draft_countdown():
    event = draft_ws.parse_frame("CLOCK 0 28068\n")
    assert event == draft_ws.Clock(0, 28068)


def test_broadcast_bid_has_five_fields():
    event = draft_ws.parse_frame("BID 10 3915511 42 25000 12731\n")
    assert event == draft_ws.Bid(10, 3915511, 42, 25000, 12731)


def test_outgoing_bid_has_two_fields():
    event = draft_ws.parse_frame("BID 4426348 56\n")
    assert event == draft_ws.BidCommand(4426348, 56)


def test_sold_frame():
    event = draft_ws.parse_frame("SOLD 7 3915511 1 43 0\n")
    assert event == draft_ws.Sold(7, 3915511, 1, 43, 0)


def test_nomination_frame():
    event = draft_ws.parse_frame("NOMINATION 1 25000\n")
    assert event == draft_ws.Nomination(1, 25000)


def test_passed_frame():
    event = draft_ws.parse_frame("PASSED 6 3915511 false\n")
    assert event == draft_ws.Passed(6, 3915511, False)


def test_token_frame():
    event = draft_ws.parse_frame("TOKEN 1:1721228630:6:{REDACTED-SWID}:REDACTED-SESSION\n")
    assert event == draft_ws.Token(1, 1721228630, 6, "{REDACTED-SWID}", "REDACTED-SESSION")


def test_autodraft_frame():
    assert draft_ws.parse_frame("AUTODRAFT 6 false\n") == draft_ws.Autodraft(6, False)


def test_ping_and_pong_frames():
    assert draft_ws.parse_frame("PING PING%201787783293012\n") == \
        draft_ws.Ping("PING%201787783293012")
    assert draft_ws.parse_frame("PONG PING%201787783293012\n") == \
        draft_ws.Pong("PING%201787783293012")


def test_prenominate_and_draft_list():
    assert draft_ws.parse_frame("PRENOMINATE 3918298 1\n") == \
        draft_ws.Prenominate(((3918298, 1),))
    assert draft_ws.parse_frame("DRAFT_LIST 3918298\n") == draft_ws.DraftList((3918298,))


def test_state_frame():
    assert draft_ws.parse_frame("STATE 1\n") == draft_ws.State(1)


def test_joined_frame():
    assert draft_ws.parse_frame("JOINED 6 {REDACTED-SWID}\n") == \
        draft_ws.Joined(6, "{REDACTED-SWID}")


def test_bid_ack_frame():
    assert draft_ws.parse_frame("BID_ACK 6 4426348 56\n") == draft_ws.BidAck(6, 4426348, 56)


def test_nominate_and_auto_nomination_frames():
    assert draft_ws.parse_frame("NOMINATE 4426502 1\n") == draft_ws.Nominate(4426502, 1)
    assert draft_ws.parse_frame("AUTO_NOMINATION 4262921\n") == draft_ws.AutoNomination(4262921)


def test_init_frame_is_decodable_as_a_blob():
    event = draft_ws.parse_frame("INIT AAAABBBB\n")
    assert isinstance(event, draft_ws.Init)
    assert event.blob == "AAAABBBB"


def test_parse_frame_empty_string_is_an_error():
    assert isinstance(draft_ws.parse_frame(""), draft_ws.WsError)


def test_parse_frame_unrecognized_kind_is_an_error():
    event = draft_ws.parse_frame("FROBNICATE 1 2 3\n")
    assert isinstance(event, draft_ws.WsError)
    assert "unrecognized" in event.reason


def test_parse_frame_malformed_known_kind_is_an_error():
    event = draft_ws.parse_frame("BID 1 2 3\n")  # 3 fields matches neither 2 nor 5
    assert isinstance(event, draft_ws.WsError)
    assert "malformed" in event.reason


def test_redact_token_strips_swid_and_session():
    raw = "TOKEN 1:123:6:{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}:987654321\n"
    assert draft_ws.redact_token(raw) == "TOKEN 1:123:6:{REDACTED-SWID}:REDACTED-SESSION\n"


def test_redact_token_strips_swid_outside_token_frame():
    raw = "JOINED 6 {AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}\n"
    assert draft_ws.redact_token(raw) == "JOINED 6 {REDACTED-SWID}\n"


def test_parse_join_url_extracts_league_and_team():
    url = "wss://fantasydraft.espn.com/game-1/league-999/JOIN?1=abc&2=999&3=6&4=tok"
    assert draft_ws.parse_join_url(url) == ("999", "6")


def test_parse_join_url_missing_params_raises():
    with pytest.raises(ValueError):
        draft_ws.parse_join_url("wss://fantasydraft.espn.com/game-1/league-999/JOIN?1=abc")


def test_iter_frames_default_skips_sent_frames():
    received = list(draft_ws.iter_frames(HAR_FIXTURE))
    both = list(draft_ws.iter_frames(HAR_FIXTURE, include_sent=True))
    assert len(both) > len(received)
    assert "PING PING%201787783293012\n" not in received
    assert "PING PING%201787783293012\n" in both
