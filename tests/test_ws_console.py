"""Integration tests for the Textual --ws console, driven through Textual's
own Pilot harness against a scripted fake WsController. Deliberately not
exhaustive UI coverage: this repo verifies live-I/O behavior with a rehearsal,
not by simulating a terminal. What is covered here is the wiring between a
drained websocket event and what ends up on screen.
"""

from __future__ import annotations

import base64
import json
import struct

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
        self.connected = False
        self._pending: list = []
        self._announced: set[int] = set()
        self.fail_with: Exception | None = None

    def feed(self, *events) -> None:
        self._pending.extend(events)

    def drain(self) -> list:
        if self.fail_with:
            raise self.fail_with
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


def _roster_rows(app) -> list[tuple]:
    """Every row currently in the roster DataTable, as plain tuples."""
    return [tuple(app.roster_table.get_row_at(i))
            for i in range(app.roster_table.row_count)]


def make_app(tmp_path, nomination_list=("Justin Jefferson", "Kenneth Walker III"),
             nomination_list_path=None):
    """Returns (app, ws, state). state_path is always under tmp_path: the
    DraftState default writes over the real live-draft file."""
    values_path = tmp_path / "values.json"
    values_path.write_text(json.dumps(FIXTURE_ROWS))
    vals = [values.Valuation(**row) for row in FIXTURE_ROWS]
    state = draft_state.DraftState(my_team="ME", state_path=tmp_path / "draft-state.json")
    resolver = draft_sync.PlayerResolver(values_path)
    ws = FakeWsController()
    app = ws_console.TextualWsApp(ws, state, resolver, vals, list(nomination_list),
                                   nomination_list_path)
    return app, ws, state


@pytest.mark.asyncio
async def test_nominations_has_a_minimum_height_floor(tmp_path):
    """At a standard 80x24 terminal the fixed heights of #status, #middle
    and #analysis alone sum to 25 rows, leaving 1fr no room -- the nomination
    list that `n` acts on must stay visible regardless of terminal size."""
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        min_height = app.nominations.styles.min_height
        assert min_height is not None
        assert min_height.value >= 5


@pytest.mark.asyncio
async def test_app_mounts_every_panel(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test():
        assert app.query_one("#status", ws_console.StatusPanel)
        assert app.query_one("#bidlog", ws_console.BidLog)
        assert app.query_one("#roster-header", ws_console.RosterPanel)
        assert app.query_one("#roster-table", ws_console.RosterTable)
        assert app.query_one("#analysis", ws_console.AnalysisPanel)
        assert app.query_one("#nominations", ws_console.NominationTable) is not None


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


def _build_init_blob(league_id: int, sales: dict[int, tuple[int, int, int]]) -> str:
    """pick_number -> (team_id, player_id, price); unlisted picks are unsold.
    Duplicated from tests/test_draft_ws_parser.py's builder, which is the one
    validated against the real captures in data/ -- this mirrors the same
    layout to drive it through the console rather than the raw parser."""
    header = b"\x00" * 8 + struct.pack(">I", league_id) + b"\x00" * 8
    records = bytearray()
    for pick in range(1, 161):
        team, player, price = sales.get(pick, (0, -1, 0))
        records += (
            struct.pack(">iiiiiiiii", 1, 3, league_id, team, pick, player, 0, price, 0)
            + b"\x00" * 9
        )
    blob = header + struct.pack(">I", 160) + bytes(records)
    return base64.b64encode(blob).decode()


@pytest.mark.asyncio
async def test_init_backfills_a_sale_the_console_never_witnessed(tmp_path):
    app, ws, state = make_app(tmp_path)
    blob = _build_init_blob(999, {1: (6, 3915511, 54)})  # team 6 = ME
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Init(blob))
        await app._poll()
        await pilot.pause()
    assert len(state.purchases) == 1
    assert state.purchases[0].player == "Bijan Robinson"
    assert state.purchases[0].team == "ME"
    assert state.purchases[0].price == 54


@pytest.mark.asyncio
async def test_init_backfill_refreshes_the_roster_panel(tmp_path):
    app, ws, state = make_app(tmp_path)
    blob = _build_init_blob(999, {1: (6, 3915511, 54)})
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Init(blob))
        await app._poll()
        await pilot.pause()
        assert app.roster.budget_left == 146
        assert ("Bijan Robinson", "RB", "$54") in _roster_rows(app)


@pytest.mark.asyncio
async def test_init_with_only_additions_shows_a_non_alert_banner(tmp_path):
    app, ws, state = make_app(tmp_path)
    blob = _build_init_blob(999, {1: (6, 3915511, 54)})
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Init(blob))
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert not app.banner.has_class("alert")


@pytest.mark.asyncio
async def test_init_conflict_overwrites_local_state_and_alerts(tmp_path):
    app, ws, state = make_app(tmp_path)
    state.record("Bijan Robinson", "RB", 40, "CCT")  # local: wrong team/price
    blob = _build_init_blob(999, {1: (6, 3915511, 54)})  # server: ME, $54
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Init(blob))
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert app.banner.has_class("alert")
    assert len(state.purchases) == 1
    assert state.purchases[0].team == "ME"
    assert state.purchases[0].price == 54


@pytest.mark.asyncio
async def test_init_removes_a_local_purchase_the_server_does_not_have(tmp_path):
    app, ws, state = make_app(tmp_path)
    state.record("Ghost Player", "RB", 5, "ME")
    blob = _build_init_blob(999, {})  # nothing sold according to the server
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Init(blob))
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert app.banner.has_class("alert")
    assert state.purchases == []


@pytest.mark.asyncio
async def test_init_with_no_changes_stays_silent(tmp_path):
    app, ws, state = make_app(tmp_path)
    state.purchases.append(draft_state.Purchase("Bijan Robinson", "RB", 54, "ME", 3915511))
    blob = _build_init_blob(999, {1: (6, 3915511, 54)})
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Init(blob))
        await app._poll()
        await pilot.pause()
        assert not app.banner.display


@pytest.mark.asyncio
async def test_init_undecodable_blob_alerts_instead_of_crashing(tmp_path):
    app, ws, state = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Init("short"))
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert app.banner.has_class("alert")


@pytest.mark.asyncio
async def test_init_backs_up_state_before_reconciling(tmp_path):
    app, ws, state = make_app(tmp_path)
    state.record("Ghost Player", "RB", 5, "ME")
    blob = _build_init_blob(999, {})
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Init(blob))
        await app._poll()
        await pilot.pause()
    backup = state.state_path.with_suffix(".json.bak")
    assert backup.exists()
    assert "Ghost Player" in backup.read_text()


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
        assert ("Justin Jefferson", "WR", "$52") in _roster_rows(app)
        assert ("WR", 1, 2) in app.roster.slots
        assert ("QB", 0, 2) in app.roster.slots


@pytest.mark.asyncio
async def test_roster_table_keeps_every_player_as_the_roster_grows(tmp_path):
    """Regression for the 2026-08-27 rehearsal: budget/spots/slot counts on
    the header updated correctly but only the first drafted name ever
    rendered below it. Root cause was RosterPanel being a plain Static whose
    "auto" height gets fixed at the first render and never grows -- a
    DataTable doesn't have that failure mode, so every recorded purchase
    must show up as a row regardless of how many came before it."""
    app, ws, state = make_app(tmp_path)
    async with app.run_test() as pilot:
        for player, position, price in [
            ("Puka Nacua", "WR", 55),
            ("Amon-Ra St. Brown", "WR", 46),
            ("Jonathan Taylor", "RB", 45),
            ("Justin Jefferson", "WR", 36),
        ]:
            state.record(player, position, price, "ME")
            app._refresh_panels()
            await pilot.pause()
        rows = _roster_rows(app)
        assert len(rows) == 4
        assert ("Amon-Ra St. Brown", "WR", "$46") in rows
        assert ("Justin Jefferson", "WR", "$36") in rows


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
async def test_board_hides_sold_players(tmp_path):
    """The board is the full priced pool, not just the starred set -- once
    Jefferson sells, every other undrafted fixture player stays visible."""
    app, ws, state = make_app(tmp_path)
    state.record("Justin Jefferson", "WR", 52, "HH")
    async with app.run_test() as pilot:
        app._reload_board()
        await pilot.pause()
        names = {row.name for row in app._board_rows}
        assert names == {"Bijan Robinson", "Kenneth Walker III", "Tony Pollard"}


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
async def test_a_watchdog_alert_survives_an_unrelated_nomination(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.alerts.append("no frames received in 45s -- forcing reconnect")
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert app.banner.has_class("alert")
        # Someone else's nomination fires constantly during a live draft and
        # must not silently wipe a live alert before it can be read.
        ws.feed(draft_ws.Nomination(7, 25000))
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert app.banner.has_class("alert")


@pytest.mark.asyncio
async def test_a_watchdog_alert_survives_a_sold_event(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.alerts.append("no frames received in 45s -- forcing reconnect")
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        ws.feed(draft_ws.Sold(4, 3915511, 1, 54, 0))
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert app.banner.has_class("alert")


@pytest.mark.asyncio
async def test_disconnect_banner_clears_once_reconnected(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.alerts.append(f"{draft_ws.DISCONNECT_ALERT_PREFIX} -- reconnecting (attempt 1)")
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert app.banner.has_class("alert")

        ws.connected = True
        await app._poll()
        await pilot.pause()
        assert not app.banner.display


@pytest.mark.asyncio
async def test_disconnect_banner_does_not_wipe_a_later_alert_on_reconnect(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.alerts.append(f"{draft_ws.DISCONNECT_ALERT_PREFIX} -- reconnecting (attempt 1)")
        await app._poll()
        await pilot.pause()

        ws.alerts.append("no frames received in 45s -- forcing reconnect")
        await app._poll()
        await pilot.pause()

        ws.connected = True
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        assert app.banner.has_class("alert")


@pytest.mark.asyncio
async def test_drained_alerts_and_feed_errors_also_land_in_the_bid_log(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.alerts.append("no frames received in 45s -- forcing reconnect")
        await app._poll()
        await pilot.pause()
        assert any("forcing reconnect" in str(line) for line in app.bidlog.lines)


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
async def test_b_refuses_when_you_already_hold_the_high(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(6, 3915511, 40, 25000, 12731))    # team 6 is us
        await app._poll()
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
    assert ws.client.sent == []
    assert any("already hold" in str(line) for line in app.bidlog.lines)


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
async def test_slash_command_filters_the_board_by_name(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app._run_command("/jeff")
        await pilot.pause()
        assert [r.name for r in app._board_rows] == ["Justin Jefferson"]
        app._run_command("/")  # empty search clears the filter
        await pilot.pause()
        assert len(app._board_rows) == 4


@pytest.mark.asyncio
async def test_search_command_is_equivalent_to_the_slash_prefix(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app._run_command("search pollard")
        await pilot.pause()
        assert [r.name for r in app._board_rows] == ["Tony Pollard"]


@pytest.mark.asyncio
async def test_pos_command_filters_by_position(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app._run_command("pos WR")
        await pilot.pause()
        assert [r.name for r in app._board_rows] == ["Justin Jefferson"]
        app._run_command("pos all")
        await pilot.pause()
        assert len(app._board_rows) == 4


@pytest.mark.asyncio
async def test_sort_command_reorders_by_the_given_key(tmp_path):
    # No starred players here: starring pins rows to the top regardless of
    # sort, which would otherwise mask the sort order this test checks.
    app, _, _ = make_app(tmp_path, nomination_list=())
    async with app.run_test() as pilot:
        app._run_command("sort name")
        await pilot.pause()
        names = [r.name for r in app._board_rows]
        assert names == sorted(names)


@pytest.mark.asyncio
async def test_sort_command_rejects_an_unknown_key(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app._run_command("sort nonsense")
        await pilot.pause()
        assert any("Unknown sort key" in str(line) for line in app.bidlog.lines)


@pytest.mark.asyncio
async def test_star_command_filters_to_the_starred_set(tmp_path):
    app, _, _ = make_app(
        tmp_path, nomination_list=("Justin Jefferson", "Kenneth Walker III"))
    async with app.run_test() as pilot:
        app._run_command("star")
        await pilot.pause()
        assert {r.name for r in app._board_rows} == {"Justin Jefferson", "Kenneth Walker III"}
        app._run_command("star all")
        await pilot.pause()
        assert len(app._board_rows) == 4


@pytest.mark.asyncio
async def test_clear_command_resets_every_filter(tmp_path):
    app, _, _ = make_app(
        tmp_path, nomination_list=("Justin Jefferson",))
    async with app.run_test() as pilot:
        app._run_command("/robinson")
        app._run_command("pos RB")
        app._run_command("star")
        await pilot.pause()
        assert app._board_rows == []   # Robinson is a RB but not starred
        app._run_command("clear")
        await pilot.pause()
        assert len(app._board_rows) == 4


@pytest.mark.asyncio
async def test_space_stars_the_highlighted_row_and_saves_it(tmp_path):
    list_path = tmp_path / "nomination-list.txt"
    app, _, _ = make_app(tmp_path, nomination_list=(), nomination_list_path=list_path)
    async with app.run_test() as pilot:
        app.nominations.move_cursor(row=0)  # Justin Jefferson, top by rank
        await pilot.press("space")
        await pilot.pause()
    assert "Justin Jefferson" in app.starred
    assert auction.load_nomination_list(list_path) == ["Justin Jefferson"]


@pytest.mark.asyncio
async def test_space_again_unstars_and_persists_the_removal(tmp_path):
    list_path = tmp_path / "nomination-list.txt"
    app, _, _ = make_app(
        tmp_path, nomination_list=("Justin Jefferson",), nomination_list_path=list_path)
    async with app.run_test() as pilot:
        app.nominations.move_cursor(row=0)  # starred rows lead, so this is Jefferson
        await pilot.press("space")
        await pilot.pause()
    assert "Justin Jefferson" not in app.starred
    assert auction.load_nomination_list(list_path) == []


@pytest.mark.asyncio
async def test_slash_hotkey_opens_the_command_input_prefilled(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        await pilot.pause()
        assert app.command.display
        assert app.command.has_focus
        assert app.command.value == "/"


@pytest.mark.asyncio
async def test_a_nomination_turn_does_not_steal_focus_from_an_open_command(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await pilot.pause()
        assert app.command.has_focus
        ws.feed(draft_ws.Nomination(6, 25000))     # config.MY_TEAM_ID is 6
        await app._poll()
        await pilot.pause()
        # The turn alert still fires, but must not pull focus off the command
        # input the user was mid-way through typing into.
        assert app.command.has_focus
        assert app.banner.display


@pytest.mark.asyncio
async def test_a_nomination_turn_still_grabs_focus_when_command_is_closed(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Nomination(6, 25000))
        await app._poll()
        await pilot.pause()
        assert app.nominations.has_focus


@pytest.mark.asyncio
async def test_reload_after_a_sale_preserves_the_highlighted_player(tmp_path):
    app, ws, state = make_app(
        tmp_path, nomination_list=("Justin Jefferson", "Kenneth Walker III", "Tony Pollard"))
    async with app.run_test() as pilot:
        app.nominations.move_cursor(row=2)  # highlight Tony Pollard
        assert app._board_rows[app.nominations.cursor_row].name == "Tony Pollard"
        # A Sold event for an unrelated player triggers _reload_board, which
        # must not reset the highlight back to row 0.
        ws.feed(draft_ws.Sold(4, 3915511, 1, 54, 0))    # Bijan Robinson, not starred
        await app._poll()
        await pilot.pause()
        assert app._board_rows[app.nominations.cursor_row].name == "Tony Pollard"


@pytest.mark.asyncio
async def test_reload_falls_back_to_row_zero_when_the_highlighted_player_is_taken(tmp_path):
    app, ws, state = make_app(
        tmp_path, nomination_list=("Justin Jefferson", "Kenneth Walker III"))
    async with app.run_test() as pilot:
        app.nominations.move_cursor(row=1)  # highlight Kenneth Walker III
        assert app._board_rows[app.nominations.cursor_row].name == "Kenneth Walker III"
        state.record("Kenneth Walker III", "RB", 38, "HH")
        app._reload_board()
        await pilot.pause()
        assert app._board_rows[app.nominations.cursor_row].name == "Justin Jefferson"
        assert app.nominations.cursor_row == 0


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


@pytest.mark.asyncio
async def test_b_drains_pending_events_before_evaluating_the_bid(tmp_path):
    """A rival's raise can arrive over the wire but sit undrained until the
    next 300ms poll tick. Pressing b must not evaluate against a pointer
    that's already stale."""
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        ws.feed(draft_ws.Bid(8, 3915511, 45, 25000, 12000))   # left undrained
        await pilot.press("b")
        await pilot.pause()
    assert ws.client.sent == [("BID", 3915511, 46)]


@pytest.mark.asyncio
async def test_b_shows_the_banner_and_does_not_crash_when_the_drain_raises(tmp_path):
    """_start_bid's own drain call must go through the same crash guard as
    the poll timer's -- a malformed frame on a bid keypress must show the
    LIVE FEED ERROR banner, not take the whole app down. No nomination was
    ever successfully drained, so the pointer stays empty and 'b' has
    nothing to bid on either."""
    app, ws, _ = make_app(tmp_path)
    ws.fail_with = RuntimeError("boom")
    async with app.run_test() as pilot:
        await pilot.press("b")
        await pilot.pause()
        assert app.is_running
        assert app.banner.display
        assert app.banner.has_class("alert")
        assert "LIVE FEED ERROR" in str(app.banner.content)
        assert any("LIVE FEED ERROR" in str(line) for line in app.bidlog.lines)
    assert ws.client.sent == []


@pytest.mark.asyncio
async def test_sold_flags_a_mismatched_duplicate_loudly(tmp_path):
    """A stale record from an unrelated earlier practice draft, for the same
    real player, must not be mistaken for a harmless by-hand duplicate."""
    app, ws, state = make_app(tmp_path)
    state.record_pick("Bijan Robinson", "RB", 40, "HH", espn_pick_id=9999999)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Sold(4, 3915511, 1, 54, 0))    # Bijan Robinson, different team/price
        await app._poll()
        await pilot.pause()
        assert app.banner.display
        # The flashed message is long enough to wrap across several bid log
        # rows at the panel's width, so check the joined plain text rather
        # than one row at a time.
        assert "may now be wrong" in " ".join(line.text for line in app.bidlog.lines)
    assert len(state.purchases) == 1                    # not double-recorded either


@pytest.mark.asyncio
async def test_sold_stays_quiet_for_a_genuine_matching_duplicate(tmp_path):
    app, ws, state = make_app(tmp_path)
    state.record("Bijan Robinson", "RB", 54, "CCT")
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Sold(4, 3915511, 1, 54, 0))    # same team, same price
        await app._poll()
        await pilot.pause()
        assert not app.banner.display
        assert any("already recorded by hand" in str(line) for line in app.bidlog.lines)


@pytest.mark.asyncio
async def test_bid_watchdog_alerts_when_a_sent_bid_never_gets_confirmed(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        await pilot.press("b")                          # sends BID 3915511 41
        await pilot.pause()
        assert app._pending_bid is not None
        player_id, amount, _ = app._pending_bid
        app._pending_bid = (player_id, amount, app._pending_bid[2] - 100)  # backdate
        ws.feed(draft_ws.Clock(2, 12000, high_bid_team=4, player_id=3915511,
                               high_bid_amount=40))       # still $40, our $41 never landed
        await app._poll()
        await pilot.pause()
        # banner.display reads back False once the app has torn down, so
        # check it before the `async with` block exits.
        assert app.banner.display
        assert app._pending_bid is None                  # alerts once, doesn't keep spamming
        lines_after_alert = len(app.bidlog.lines)
        # A second check with nothing pending must not write another alert.
        app._check_bid_watchdog()
        assert len(app.bidlog.lines) == lines_after_alert
    assert any("unconfirmed" in str(line) for line in app.bidlog.lines)


@pytest.mark.asyncio
async def test_self_bid_guard_holds_when_my_team_label_diverges_from_config(tmp_path):
    """config.TEAMS[config.MY_TEAM_ID] is 'ME', but state.my_team can be
    overridden (--my-team, or a persisted value) to something else. The
    self-bid guard has to key off ESPN team id, not the label, or it
    silently stops refusing self-bids the moment the two diverge."""
    app, ws, state = make_app(tmp_path)
    state.my_team = "QBK"
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(6, 3915511, 40, 25000, 12731))    # team 6 is us
        await app._poll()
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
    assert ws.client.sent == []
    assert any("already hold" in str(line) for line in app.bidlog.lines)


@pytest.mark.asyncio
async def test_watchdog_confirms_a_bid_when_my_team_label_diverges_from_config(tmp_path):
    """Same divergence as above, but for the watchdog's confirm check: it
    must still recognize a bid confirmed under team id 6 as ours even when
    state.my_team isn't 'ME'."""
    app, ws, state = make_app(tmp_path)
    state.my_team = "QBK"
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        await pilot.press("b")                          # sends BID 3915511 41
        await pilot.pause()
        ws.feed(draft_ws.Bid(6, 3915511, 41, 25000, 12000))   # server confirms it: team 6 is us
        await app._poll()
        await pilot.pause()
        assert not app.banner.display
    assert app._pending_bid is None


@pytest.mark.asyncio
async def test_bid_watchdog_fires_on_total_socket_silence(tmp_path):
    """The watchdog check has to run every poll tick regardless of whether
    _drain() produced any events -- a silent socket (nothing arriving at
    all) is exactly the case it exists to catch, not just a busy one."""
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        await pilot.press("b")                          # sends BID 3915511 41
        await pilot.pause()
        assert app._pending_bid is not None
        player_id, amount, sent_at = app._pending_bid
        app._pending_bid = (player_id, amount, sent_at - 100)  # backdate
        # No new events fed at all -- the queue stays empty.
        await app._poll()
        await pilot.pause()
        assert app.banner.display
    assert any("unconfirmed" in str(line) for line in app.bidlog.lines)


@pytest.mark.asyncio
async def test_bid_watchdog_clears_when_the_bid_is_confirmed(tmp_path):
    app, ws, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        ws.feed(draft_ws.Bid(4, 3915511, 40, 25000, 12731))
        await app._poll()
        await pilot.pause()
        await pilot.press("b")                          # sends BID 3915511 41
        await pilot.pause()
        ws.feed(draft_ws.Bid(6, 3915511, 41, 25000, 12000))   # server confirms it: team 6 is us
        await app._poll()
        await pilot.pause()
        # banner.display reads back False once the app has torn down
        # regardless of alert state, so check it before the block exits.
        assert not app.banner.display
    assert app._pending_bid is None


@pytest.mark.asyncio
async def test_board_rows_carry_tier_and_adjusted_value(tmp_path):
    app, ws, _ = make_app(tmp_path)   # default starred: Justin Jefferson, Kenneth Walker III
    async with app.run_test() as pilot:
        app._reload_board()
        await pilot.pause()
        row = app._board_rows[0]         # Justin Jefferson: tier 1, $52, starred
    assert row.name == "Justin Jefferson"
    assert row.valuation.tier == 1
    assert row.valuation.value == 52
    assert row.adjusted == 52            # no sales yet, inflation is 1.0
    assert row.starred is True


@pytest.mark.asyncio
async def test_a_starred_name_missing_from_values_json_is_not_a_row(tmp_path):
    """A starred name that isn't in values.json (cut, renamed, a typo) has no
    Valuation to build a row from, so it's silently absent from the board --
    but see test_save_nomination_list_round_trips: it must not be dropped
    from the starred set on the next save."""
    app, ws, _ = make_app(
        tmp_path, nomination_list=("Justin Jefferson", "Not On The Board"))
    async with app.run_test() as pilot:
        app._reload_board()
        await pilot.pause()
        names = [row.name for row in app._board_rows]
    assert "Not On The Board" not in names
    assert "Justin Jefferson" in names
