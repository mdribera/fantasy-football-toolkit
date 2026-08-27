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
from ff import draft_state, draft_sync, values

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
