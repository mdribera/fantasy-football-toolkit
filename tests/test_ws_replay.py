"""Unit tests for ws_replay -- the shim that drives the --ws console from a
recorded ws-log-*.jsonl capture instead of a live socket. Fixtures are
synthesized in tmp_path rather than relying on a real capture, so these tests
don't depend on the gitignored data/ recordings other test files use.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import ws_replay
from ff import draft_ws


def _write_capture(path: Path, rows: list[tuple[str, str, float]]) -> None:
    """rows: (dir, msg, ts) triples, written one JSON object per line."""
    with path.open("w") as fh:
        for direction, msg, ts in rows:
            fh.write(json.dumps({"ts": ts, "dir": direction, "msg": msg}) + "\n")


def test_iter_frames_with_ts_keeps_timestamps_and_skips_sent_by_default(tmp_path):
    path = tmp_path / "capture.jsonl"
    _write_capture(path, [
        ("receive", "CLOCK 0 1000\n", 100.0),
        ("send", "PING PING%20100000\n", 100.5),
        ("receive", "CLOCK 0 500\n", 101.0),
    ])
    received = list(ws_replay._iter_frames_with_ts(path))
    assert received == [(100.0, "CLOCK 0 1000\n"), (101.0, "CLOCK 0 500\n")]

    both = list(ws_replay._iter_frames_with_ts(path, include_sent=True))
    assert len(both) == 3


def test_replay_client_parses_every_frame_and_reports_finished(tmp_path):
    path = tmp_path / "capture.jsonl"
    _write_capture(path, [
        ("receive", "NOMINATION 6 25000\n", 0.0),
        ("receive", "BID 6 3915511 1 25000 25000\n", 0.01),
        ("receive", "ERROR 1 The+bid+is+not+valid.\n", 0.02),
    ])
    client = ws_replay.ReplayClient(path, speed=0)
    client.start()
    deadline = time.monotonic() + 5
    events: list = []
    while time.monotonic() < deadline:
        events.extend(client.drain())
        if not client.connected and len(events) >= 3:
            break
        time.sleep(0.01)
    client.stop()

    assert len(events) == 3
    assert isinstance(events[0], draft_ws.Nomination)
    assert isinstance(events[1], draft_ws.Bid)
    assert events[2] == draft_ws.Error(1, "The bid is not valid.")
    assert client.connected is False
    alerts = client.drain_alerts()
    assert len(alerts) == 1
    assert "Replay finished: 3 frames" in alerts[0]


def test_replay_client_send_methods_record_without_transmitting(tmp_path):
    path = tmp_path / "capture.jsonl"
    _write_capture(path, [])
    client = ws_replay.ReplayClient(path, speed=0)
    client.send_bid(3915511, 42)
    client.send_nomination(3915511, 1)
    assert client.sent == [("BID", 3915511, 42), ("NOMINATE", 3915511, 1)]


def test_replay_client_paces_playback_by_speed(tmp_path):
    path = tmp_path / "capture.jsonl"
    _write_capture(path, [
        ("receive", "CLOCK 0 1000\n", 0.0),
        ("receive", "CLOCK 0 500\n", 0.4),
    ])
    client = ws_replay.ReplayClient(path, speed=10)  # 0.4s gap -> ~0.04s wait
    start = time.monotonic()
    client.start()
    events: list = []
    while time.monotonic() - start < 5:
        events.extend(client.drain())
        if len(events) == 2:
            break
        time.sleep(0.01)
    elapsed = time.monotonic() - start
    client.stop()
    assert len(events) == 2
    assert elapsed >= 0.03  # paced, not instant
    assert elapsed < 2.0    # but nowhere near the unscaled 0.4s+ real gap


def test_replay_client_speed_zero_plays_back_with_no_pacing(tmp_path):
    path = tmp_path / "capture.jsonl"
    _write_capture(path, [
        ("receive", "CLOCK 0 1000\n", 0.0),
        ("receive", "CLOCK 0 500\n", 500.0),  # a huge gap that must be skipped entirely
    ])
    client = ws_replay.ReplayClient(path, speed=0)
    start = time.monotonic()
    client.start()
    events: list = []
    while time.monotonic() - start < 3:
        events.extend(client.drain())
        if len(events) == 2:
            break
        time.sleep(0.01)
    elapsed = time.monotonic() - start
    client.stop()
    assert len(events) == 2
    assert elapsed < 1.0


def test_replay_controller_folds_events_into_the_auction_pointer(tmp_path):
    path = tmp_path / "capture.jsonl"
    _write_capture(path, [
        ("receive", "NOMINATION 6 25000\n", 0.0),
        ("receive", "BID 6 3915511 42 25000 12731\n", 0.01),
    ])
    controller = ws_replay.ReplayController(path, speed=0)
    controller.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        controller.drain()
        if controller.pointer.player_id == 3915511:
            break
        time.sleep(0.01)
    controller.client.stop()
    assert controller.pointer.player_id == 3915511
    assert controller.pointer.high_bid == 42


def test_replay_controller_connected_flips_false_once_the_capture_ends(tmp_path):
    path = tmp_path / "capture.jsonl"
    _write_capture(path, [("receive", "CLOCK 0 1000\n", 0.0)])
    controller = ws_replay.ReplayController(path, speed=0)
    controller.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and controller.connected:
        controller.drain()
        time.sleep(0.01)
    controller.client.stop()
    assert controller.connected is False
