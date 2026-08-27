"""Integration tests for the Textual --ws console, driven through Textual's
own Pilot harness against a scripted fake WsController. Deliberately not
exhaustive UI coverage: this repo verifies live-I/O behavior with a rehearsal,
not by simulating a terminal. What is covered here is the wiring between a
drained websocket event and what ends up on screen.
"""

from __future__ import annotations

import json

import pytest

import auction
import ws_console
from ff import draft_state, draft_sync, draft_ws, values

FIXTURE_ROWS = [
    {"name": "Bijan Robinson", "position": "RB", "pro_team": "ATL",
     "projected_points": 300.0, "replacement_points": 120.0, "vorp": 180.0,
     "value": 43, "tier": 2, "espn_id": 3915511},
    {"name": "Kenneth Walker III", "position": "RB", "pro_team": "SEA",
     "projected_points": 280.0, "replacement_points": 120.0, "vorp": 160.0,
     "value": 38, "tier": 2, "espn_id": 3915512},
    {"name": "Tony Pollard", "position": "RB", "pro_team": "TEN",
     "projected_points": 230.0, "replacement_points": 120.0, "vorp": 110.0,
     "value": 22, "tier": 3, "espn_id": 3915513},
    {"name": "Justin Jefferson", "position": "WR", "pro_team": "MIN",
     "projected_points": 310.0, "replacement_points": 130.0, "vorp": 180.0,
     "value": 52, "tier": 1, "espn_id": 3915514},
]


class FakeClient:
    """Stands in for DraftRoomClient: records sends, never opens a socket."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.fail_with: Exception | None = None

    def send_bid(self, player_id: int, amount: int) -> None:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(("BID", player_id, amount))

    def send_nomination(self, player_id: int, opening_bid: int) -> None:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(("NOMINATE", player_id, opening_bid))

    def stop(self) -> None:
        pass


class FakeWsController:
    """The surface TextualWsApp uses from auction.WsController, fed by a
    scripted event list. Pointer folding and milestone tracking go through
    the real auction.py functions so the test exercises them."""

    def __init__(self):
        self.client = FakeClient()
        self.pointer = auction.WsAuctionPointer()
        self.alerts: list[str] = []
        self._pending: list = []
        self._announced: set[int] = set()

    def feed(self, *events) -> None:
        self._pending.extend(events)

    def drain(self) -> list:
        events, self._pending = self._pending, []
        for event in events:
            updated = auction.apply_ws_event(self.pointer, event)
            if updated.player_id != self.pointer.player_id:
                self._announced.clear()
            self.pointer = updated
        return events

    def drain_alerts(self) -> list[str]:
        alerts, self.alerts = self.alerts, []
        return alerts

    def milestone(self, event):
        return auction.clock_milestone(event.remaining_ms, self._announced)


def make_app(tmp_path, nomination_list=("Justin Jefferson", "Kenneth Walker III")):
    """Returns (app, ws, state). state_path is always under tmp_path: the
    DraftState default writes over the real live-draft file."""
    values_path = tmp_path / "values.json"
    values_path.write_text(json.dumps(FIXTURE_ROWS))
    vals = [values.Valuation(**row) for row in FIXTURE_ROWS]
    state = draft_state.DraftState(my_team="ME", state_path=tmp_path / "draft-state.json")
    resolver = draft_sync.PlayerResolver(values_path)
    ws = FakeWsController()
    app = ws_console.TextualWsApp(ws, state, resolver, vals, list(nomination_list))
    return app, ws, state


@pytest.mark.asyncio
async def test_app_mounts_every_panel(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test():
        assert app.query_one("#status", ws_console.StatusPanel)
        assert app.query_one("#bidlog", ws_console.BidLog)
        assert app.query_one("#roster", ws_console.RosterPanel)
        assert app.query_one("#analysis", ws_console.AnalysisPanel)
        assert app.query_one("#nominations", ws_console.NominationList) is not None


@pytest.mark.asyncio
async def test_q_exits(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
    assert not app.is_running


@pytest.mark.asyncio
async def test_bid_updates_the_status_panel_and_log(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 54, 25000, 12731))
        await app._poll()
        await pilot.pause()
        assert "Bijan Robinson" in app.status.nominee
        assert app.status.high_bid == 54
        assert app.status.high_bidder == "CCT"


@pytest.mark.asyncio
async def test_clock_state_2_drives_the_countdown(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Clock(2, 6400, high_bid_team=4, player_id=3915511,
                               high_bid_amount=54))
        await app._poll()
        await pilot.pause()
        assert app.status.clock_s == 6


@pytest.mark.asyncio
async def test_status_shows_sheet_and_inflation_adjusted_value(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 54, 25000, 12731))
        await app._poll()
        await pilot.pause()
        assert app.status.sheet_value == 43
        assert app.status.adjusted_value == 43   # no sales yet, inflation is 1.0


@pytest.mark.asyncio
async def test_sold_records_the_purchase_once(tmp_path):
    app, ws, state = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Sold(4, 3915511, 1, 54, 0))
        await app._poll()
        await pilot.pause()
        ws.feed(draft_ws.Sold(4, 3915511, 1, 54, 0))
        await app._poll()
        await pilot.pause()
    assert len(state.purchases) == 1
    assert state.purchases[0].player == "Bijan Robinson"
    assert state.purchases[0].price == 54
    assert state.purchases[0].team == "CCT"


@pytest.mark.asyncio
async def test_sold_skips_a_pick_already_entered_by_hand(tmp_path):
    app, ws, state = make_app(tmp_path)
    state.record("Bijan Robinson", "RB", 54, "CCT")
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Sold(4, 3915511, 1, 54, 0))
        await app._poll()
        await pilot.pause()
    assert len(state.purchases) == 1


@pytest.mark.asyncio
async def test_bid_log_clears_when_the_nomination_changes(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 54, 25000, 12731))
        await app._poll()
        await pilot.pause()
        assert app.bidlog.lines
        ws.feed(draft_ws.Bid(7, 3915514, 12, 25000, 24000))
        await app._poll()
        await pilot.pause()
        # Only the new nomination's single bid survives the clear.
        assert len(app.bidlog.lines) == 1


@pytest.mark.asyncio
async def test_watchdog_alert_reaches_the_banner(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.alerts.append("no frames received in 45s -- forcing reconnect")
        await app._poll()
        await pilot.pause()
        assert app.banner.display


@pytest.mark.asyncio
async def test_roster_panel_tracks_budget_and_slots(tmp_path):
    app, ws, state = make_app(tmp_path)
    async with app.run_test() as pilot:
        state.record("Justin Jefferson", "WR", 52, "ME")
        app._refresh_panels()
        await pilot.pause()
        assert app.roster.budget_left == 148
        assert app.roster.spots_left == 15
        assert ("Justin Jefferson", "WR", 52) in app.roster.roster
        assert ("WR", 1, 2) in app.roster.slots
        assert ("QB", 0, 2) in app.roster.slots


@pytest.mark.asyncio
async def test_analysis_names_the_next_equivalent_in_tier(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 54, 25000, 12731))
        await app._poll()
        await pilot.pause()
        assert "Tier 2 RB" in app.analysis.tier_line
        assert "Kenneth Walker III" in app.analysis.tier_line


@pytest.mark.asyncio
async def test_analysis_verdict_reflects_the_current_high(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 70, 25000, 12731))  # sheet $43
        await app._poll()
        await pilot.pause()
        assert "overpaying" in app.analysis.verdict_line


@pytest.mark.asyncio
async def test_analysis_best_remaining_targets_the_neediest_position(tmp_path):
    app, ws, state = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 44, 25000, 12731))
        await app._poll()
        await pilot.pause()
        # QB is the first unfilled slot in STARTERS order and the board has none.
        assert app._neediest_position() == "QB"
        assert "QB" in app.analysis.best_line


@pytest.mark.asyncio
async def test_analysis_falls_back_to_overall_inflation(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 44, 25000, 12731))
        await app._poll()
        await pilot.pause()
        assert "overall" in app.analysis.market_line


@pytest.mark.asyncio
async def test_nomination_list_shows_available_players_only(tmp_path):
    app, ws, state = make_app(tmp_path)
    state.record("Justin Jefferson", "WR", 52, "HH")
    async with app.run_test() as pilot:
        await app._reload_nominations()
        await pilot.pause()
        names = [row.player_name for row in app.nominations.children]
        assert names == ["Kenneth Walker III"]


@pytest.mark.asyncio
async def test_n_nominates_the_highlighted_row(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Nomination(6, 25000))     # config.MY_TEAM_ID is 6
        await app._poll()
        await pilot.pause()
        app.nominations.focus()
        await pilot.press("n")
        await pilot.pause()
    assert ws.client.sent == [("NOMINATE", 3915514, 1)]


@pytest.mark.asyncio
async def test_n_refuses_when_it_is_not_your_turn(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Nomination(7, 25000))
        await app._poll()
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
    assert ws.client.sent == []


@pytest.mark.asyncio
async def test_your_nomination_turn_raises_the_banner(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Nomination(6, 25000))
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert app.screen.has_class("my-turn")


@pytest.mark.asyncio
async def test_someone_elses_turn_lowers_the_banner(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Nomination(6, 25000))
        await app._poll()
        await pilot.pause()
        ws.feed(draft_ws.Nomination(7, 25000))
        await app._poll()
        await pilot.pause()
        assert not app.screen.has_class("my-turn")


@pytest.mark.asyncio
async def test_a_failed_nomination_send_is_reported_not_swallowed(tmp_path):
    app, ws, _ = make_app(tmp_path)
    ws.client.fail_with = RuntimeError("not connected to the draft room")
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Nomination(6, 25000))
        await app._poll()
        await pilot.pause()
        app.nominations.focus()
        await pilot.press("n")
        await pilot.pause()
        assert any("Not sent" in str(line) for line in app.bidlog.lines)


@pytest.mark.asyncio
async def test_b_bids_one_over_the_current_high(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
    assert ws.client.sent == [("BID", 3915511, 41)]


@pytest.mark.asyncio
async def test_b_with_no_active_nomination_sends_nothing(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("b")
        await pilot.pause()
    assert ws.client.sent == []


@pytest.mark.asyncio
async def test_a_big_jump_opens_the_modal_and_y_confirms(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        app._start_bid(["60"])          # $20 over, trips TYPO_GUARD_JUMP
        await pilot.pause()
        assert isinstance(app.screen, ws_console.ConfirmBidScreen)
        await pilot.press("y")
        await pilot.pause()
    assert ws.client.sent == [("BID", 3915511, 60)]


@pytest.mark.asyncio
async def test_the_modal_refuses_on_n(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        app._start_bid(["60"])
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
    assert ws.client.sent == []


@pytest.mark.asyncio
async def test_a_nomination_change_while_the_modal_is_open_cancels_the_bid(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        app._start_bid(["60"])
        await pilot.pause()
        ws.feed(draft_ws.Sold(4, 3915511, 1, 40, 0))     # pointer moves on
        await app._poll()
        await pilot.press("y")
        await pilot.pause()
    assert ws.client.sent == []


@pytest.mark.asyncio
async def test_a_bid_over_your_max_is_refused_without_a_modal(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        app._start_bid(["999"])
        await pilot.pause()
        assert not isinstance(app.screen, ws_console.ConfirmBidScreen)
    assert ws.client.sent == []


@pytest.mark.asyncio
async def test_colon_opens_the_command_input(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await pilot.pause()
        command = app.query_one("#command")
        assert command.display
        assert command.has_focus


@pytest.mark.asyncio
async def test_command_b_with_an_amount_bids_that_amount(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        app._run_command("b 45")
        await pilot.pause()
    assert ws.client.sent == [("BID", 3915511, 45)]


@pytest.mark.asyncio
async def test_command_undo_removes_the_last_purchase(tmp_path):
    app, ws, state = make_app(tmp_path)
    state.record("Justin Jefferson", "WR", 52, "ME")
    async with app.run_test() as pilot:
        app._run_command("undo")
        await pilot.pause()
    assert state.purchases == []


@pytest.mark.asyncio
async def test_command_market_and_teams_render_without_error(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        for line in ("market", "teams", "best RB", "need", "me"):
            app._run_command(line)
        await pilot.pause()
        assert len(app.bidlog.lines) > 5


@pytest.mark.asyncio
async def test_command_quit_exits(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app._run_command("quit")
        await pilot.pause()
    assert not app.is_running


@pytest.mark.asyncio
async def test_typing_in_the_command_input_does_not_fire_hotkeys(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        await pilot.press("colon")
        await pilot.pause()
        await pilot.press("b", "e", "s", "t")
        await pilot.pause()
        assert app.query_one("#command").value == "best"
    assert ws.client.sent == []
