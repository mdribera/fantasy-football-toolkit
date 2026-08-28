"""Unit tests for DraftRoomClient's threading, ping, watchdog, and
reconnect behavior. FakeWebSocketApp stands in for websocket.WebSocketApp
so these run with no real socket: run_forever() blocks until close() is
called (by the watchdog, by client.stop(), or by the test), then fires
on_close the same way a real self-triggered close does -- see
docs/notes/rehearsal-log.md's "on_close behaves differently..." finding for why
that distinction matters.
"""

from __future__ import annotations

import threading
import time

import pytest

from ff import config, draft_ws

JOIN_URL = "wss://fantasydraft.espn.com/game-1/league-999/JOIN?1=abc&2=999&3=6&4=tok"


class FakeWebSocketApp:
    instances: list["FakeWebSocketApp"] = []

    def __init__(self, url, cookie=None, header=None, on_message=None, on_open=None,
                 on_error=None, on_close=None):
        self.url = url
        self.sent: list[str] = []
        self.on_message = on_message
        self.on_open = on_open
        self.on_error = on_error
        self.on_close = on_close
        self.closed = False
        self._close_event = threading.Event()
        FakeWebSocketApp.instances.append(self)

    def send(self, frame: str) -> None:
        if self.closed:
            raise RuntimeError("send on a closed fake socket")
        self.sent.append(frame)

    def close(self) -> None:
        self.closed = True
        self._close_event.set()

    def run_forever(self) -> None:
        if self.on_open:
            self.on_open(self)
        self._close_event.wait(timeout=5)
        if self.on_close:
            self.on_close(self, None, None)


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    FakeWebSocketApp.instances.clear()
    yield
    FakeWebSocketApp.instances.clear()


@pytest.fixture
def cred() -> config.EspnCredentials:
    return config.EspnCredentials(league_id="999", swid="{SWID}", espn_s2="s2", team_id="6")


@pytest.fixture
def patch_websocket(monkeypatch):
    monkeypatch.setattr(draft_ws.websocket, "WebSocketApp", FakeWebSocketApp)


def test_ping_loop_sends_newline_terminated_ping(patch_websocket, cred, tmp_path):
    client = draft_ws.DraftRoomClient(JOIN_URL, cred, log_path=tmp_path / "log.jsonl")
    client.PING_INTERVAL_S = 0.05
    client.start()
    time.sleep(0.2)
    client.stop()

    fake = FakeWebSocketApp.instances[0]
    assert fake.sent, "expected at least one PING frame"
    assert all(f.startswith("PING PING%20") and f.endswith("\n") for f in fake.sent)


def test_send_bid_and_send_nomination_are_newline_terminated(patch_websocket, cred, tmp_path):
    client = draft_ws.DraftRoomClient(JOIN_URL, cred, log_path=tmp_path / "log.jsonl")
    client.PING_INTERVAL_S = 10
    client.start()
    time.sleep(0.05)

    client.send_bid(4426348, 56)
    client.send_nomination(4426502, 1)
    client.stop()

    fake = FakeWebSocketApp.instances[0]
    assert fake.sent == ["BID 4426348 56\n", "NOMINATE 4426502 1\n"]


def test_send_bid_raises_when_not_connected(cred, tmp_path):
    client = draft_ws.DraftRoomClient(JOIN_URL, cred, log_path=tmp_path / "log.jsonl")
    with pytest.raises(RuntimeError):
        client.send_bid(1, 2)


def test_watchdog_forces_reconnect_after_silence(patch_websocket, cred, tmp_path):
    client = draft_ws.DraftRoomClient(JOIN_URL, cred, log_path=tmp_path / "log.jsonl")
    client.PING_INTERVAL_S = 10
    client.LIVENESS_TIMEOUT_S = 0.05
    client.WATCHDOG_POLL_INTERVAL_S = 0.02
    client.RECONNECT_BACKOFF_S = 0.02
    client.start()
    time.sleep(0.4)
    client.stop()

    assert client.reconnect_count >= 1
    assert len(FakeWebSocketApp.instances) >= 2
    alerts = client.drain_alerts()
    assert any("forcing reconnect" in a for a in alerts)
    assert any(a.startswith(draft_ws.DISCONNECT_ALERT_PREFIX) for a in alerts)


def test_watchdog_does_not_reconnect_while_frames_keep_arriving(patch_websocket, cred, tmp_path):
    client = draft_ws.DraftRoomClient(JOIN_URL, cred, log_path=tmp_path / "log.jsonl")
    client.PING_INTERVAL_S = 10
    client.LIVENESS_TIMEOUT_S = 0.2
    client.WATCHDOG_POLL_INTERVAL_S = 0.03
    client.start()
    time.sleep(0.05)

    fake = FakeWebSocketApp.instances[0]
    for _ in range(8):
        fake.on_message(fake, "CLOCK 3 1000\n")
        time.sleep(0.03)
    client.stop()

    assert client.reconnect_count == 0
    events = client.drain()
    assert len(events) == 8
    assert all(isinstance(e, draft_ws.Clock) for e in events)


def test_log_file_redacts_swid_and_session(patch_websocket, cred, tmp_path):
    client = draft_ws.DraftRoomClient(JOIN_URL, cred, log_path=tmp_path / "log.jsonl")
    client.PING_INTERVAL_S = 10
    client.start()
    time.sleep(0.05)

    fake = FakeWebSocketApp.instances[0]
    fake.on_message(fake, "TOKEN 1:999:6:{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}:987654\n")
    time.sleep(0.05)
    client.stop()

    log_text = client.log_path.read_text()
    assert "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE" not in log_text
    assert "987654" not in log_text
    assert "REDACTED-SWID" in log_text
