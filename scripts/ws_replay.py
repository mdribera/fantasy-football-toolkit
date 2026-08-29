"""Replay a raw ws-log-*.jsonl capture through the live --ws console.

`scripts/draft_ws.py --replay` already reads this exact capture format
(`ff.draft_ws.iter_frames` + `parse_frame`), but it only ever folds `Sold`
events into a scratch `DraftState` and ignores everything else -- nothing
drives the full Textual console (`ws_console.TextualWsApp`) from a capture,
which is the gap `auction.py --ws --ws-replay PATH` closes. Useful on its
own, and it's how the T25 `ERROR`-frame fix got exercised end to end against
a real rejection (`data/ws-log-1787942937.jsonl`) instead of only synthetic
test events.

`ReplayController` mirrors `auction.WsController` exactly: it wraps
`ReplayClient` (a stand-in for `draft_ws.DraftRoomClient`) the same way
`WsController` wraps the real client, and folds drained events through the
identical `auction.apply_ws_event` pointer logic. `TextualWsApp` never knows
the difference -- it only ever calls `.client`, `.pointer`, `.alerts`,
`.connected`, `.drain()`, `.drain_alerts()`, and `.milestone()`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterator

import auction
from ff import draft_ws


def _iter_frames_with_ts(path: Path, include_sent: bool = False) -> Iterator[tuple[float, str]]:
    """Like draft_ws.iter_frames, but keeps each frame's timestamp so
    playback can be paced against the capture's own real-time gaps --
    iter_frames itself discards it, since none of its other callers need it."""
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not include_sent and row.get("dir", "receive") != "receive":
            continue
        yield row.get("ts", 0.0), row["msg"]


MAX_GAP_S = 5.0  # cap the longest real gap between frames so a capture that
                  # sat quiet for minutes doesn't stall a replay for minutes


class ReplayClient:
    """Stands in for draft_ws.DraftRoomClient: a background thread reads a
    raw capture and pushes parsed events onto a queue at `speed`x the
    capture's own pacing, instead of a live socket. `send_bid`/
    `send_nomination` record what would have been sent rather than
    transmitting anything -- there is no real draft room to answer during a
    replay."""

    def __init__(self, path: Path, speed: float = 1.0, include_sent: bool = False) -> None:
        self.path = path
        self.speed = speed
        self.connected = True
        self.sent: list[tuple] = []
        self.alerts: list[str] = []
        self._queue: list[draft_ws.Event] = []
        self._queue_lock = threading.Lock()
        self._alert_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(include_sent,), daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def send_bid(self, player_id: int, amount: int) -> None:
        self.sent.append(("BID", player_id, amount))

    def send_nomination(self, player_id: int, opening_bid: int) -> None:
        self.sent.append(("NOMINATE", player_id, opening_bid))

    def drain(self) -> list[draft_ws.Event]:
        with self._queue_lock:
            events, self._queue = self._queue, []
        return events

    def drain_alerts(self) -> list[str]:
        with self._alert_lock:
            alerts, self.alerts = self.alerts, []
        return alerts

    def _alert(self, message: str) -> None:
        with self._alert_lock:
            self.alerts.append(message)

    def _run(self, include_sent: bool) -> None:
        previous_ts: float | None = None
        count = 0
        for ts, msg in _iter_frames_with_ts(self.path, include_sent=include_sent):
            if self._stop.is_set():
                return
            if previous_ts is not None and self.speed > 0:
                gap = max(0.0, (ts - previous_ts) / self.speed)
                if self._stop.wait(min(gap, MAX_GAP_S)):
                    return
            previous_ts = ts
            event = draft_ws.parse_frame(msg)
            with self._queue_lock:
                self._queue.append(event)
            count += 1
        self.connected = False
        self._alert(f"Replay finished: {count} frames from {self.path.name}.")


class ReplayController:
    """The same surface TextualWsApp uses from auction.WsController -- see
    that class's docstring. Wraps ReplayClient instead of a real
    draft_ws.DraftRoomClient."""

    def __init__(self, path: Path, speed: float = 1.0, include_sent: bool = False) -> None:
        self.client = ReplayClient(path, speed=speed, include_sent=include_sent)
        self.pointer = auction.WsAuctionPointer()
        self._announced: set[int] = set()

    def start(self) -> None:
        self.client.start()

    def drain(self) -> list[draft_ws.Event]:
        events = self.client.drain()
        for event in events:
            updated = auction.apply_ws_event(self.pointer, event)
            if updated.player_id != self.pointer.player_id:
                self._announced.clear()
            self.pointer = updated
        return events

    def drain_alerts(self) -> list[str]:
        return self.client.drain_alerts()

    @property
    def connected(self) -> bool:
        return self.client.connected

    def milestone(self, event: draft_ws.Clock) -> int | None:
        return auction.clock_milestone(event.remaining_ms, self._announced)
