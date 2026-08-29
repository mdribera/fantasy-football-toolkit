"""Parser for ESPN's live draft-room websocket protocol.

The draft room speaks plain space-delimited text frames over a websocket at
wss://fantasydraft.espn.com/game-{gameId}/league-{leagueId}/JOIN -- a
completely separate host from the mDraftDetail REST feed draft_sync.py polls,
and the only one that updates during a live auction. See
docs/notes/ws-protocol.md for the reverse-engineering notes, field meanings,
and open questions.

parse_frame() never raises: an unrecognized or malformed frame comes back as
WsError so a live connection can log and move on instead of crashing on a
frame shape not yet seen.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import websocket

from . import config


@dataclass(frozen=True)
class Autodraft:
    team_id: int
    enabled: bool


@dataclass(frozen=True)
class Init:
    blob: str  # base64; see parse_init_state() for the decoded pick table


@dataclass(frozen=True)
class Token:
    """Our own connection's identity, echoed back by the TOKEN frame."""

    game_id: int
    league_id: int
    team_id: int
    swid: str
    session_id: str


@dataclass(frozen=True)
class Joined:
    """A team's connection joining the room. Only seen once (our own join)
    in the captured session, since the other nine teams were already
    connected before capture started -- unconfirmed whether this repeats
    per-team on every connect or just describes our own."""

    team_id: int
    swid: str


@dataclass(frozen=True)
class Left:
    """A team's connection leaving the room -- counterpart to Joined. Seen
    once: our own connection, disconnected by the server ~1.5s before its
    own nomination clock would have hit 0 while sitting connected but silent
    (2026-08-26 live rehearsal). The trailing flag's meaning isn't confirmed
    (only ever seen as 1). See docs/notes/ws-protocol.md."""

    team_id: int
    swid: str
    flag: int


@dataclass(frozen=True)
class Pong:
    """Echoes the payload of the Ping we just sent, for latency measurement."""

    payload: str


@dataclass(frozen=True)
class BidAck:
    team_id: int
    player_id: int
    amount: int


@dataclass(frozen=True)
class DraftList:
    """The server's echo of our current prenomination queue (see
    Prenominate) -- just player ids, in queue order."""

    player_ids: tuple[int, ...]


@dataclass(frozen=True)
class State:
    """A single overall draft-state code, seen once at connect (value 1).
    Meaning beyond that isn't confirmed."""

    value: int


@dataclass(frozen=True)
class Clock:
    """CLOCK states confirmed so far:
      0 -- pre-draft countdown: remaining_ms only.
      1 -- nomination pending: nominating_team has remaining_ms to submit a
           nomination. What happens if it hits 0 with no nomination sent is
           unconfirmed -- see docs/notes/ws-protocol.md's "Nomination
           timeout" section.
      2 -- live bidding: high_bid_team/player_id/high_bid_amount populated.
      3 -- the countdown between nominations: remaining_ms only.
    Other states are unconfirmed."""

    state: int
    remaining_ms: int
    nominating_team: int | None = None
    high_bid_team: int | None = None
    player_id: int | None = None
    high_bid_amount: int | None = None


@dataclass(frozen=True)
class AutoSuggest:
    player_id: int


@dataclass(frozen=True)
class Passed:
    team_id: int
    player_id: int
    auto: bool


@dataclass(frozen=True)
class Bid:
    team_id: int
    player_id: int
    amount: int
    clock_reset_ms: int
    clock_remaining_at_bid_ms: int


@dataclass(frozen=True)
class Sold:
    """Fields 3 and 5 are observational only: their meaning isn't confirmed,
    and tracking sales doesn't need them. See docs/notes/ws-protocol.md."""

    team_id: int
    player_id: int
    field3: int
    price: int
    field5: int


@dataclass(frozen=True)
class Nomination:
    team_id: int
    clock_reset_ms: int


@dataclass(frozen=True)
class Error:
    """A rejection aimed at this connection alone, not broadcast to the
    room -- confirmed by three examples in one capture (2026-08-28) where
    NOMINATE 3042519 1 (Aaron Jones Sr.) got ERROR back every time, ~13ms
    later, with no broadcast Bid ever following. `message` arrives
    percent-and-plus encoded on the wire (the same family as PING's
    "PING%20<epochMs>") and is decoded here. `code` has only been observed
    as 1. See docs/notes/ws-protocol.md and docs/notes/rehearsal-log.md --
    why ESPN rejects a given nomination is still unconfirmed, but a
    rejection leaves the turn exactly where a silently-ignored NOMINATE
    would: still open, and burned if nothing else is sent before the clock
    runs out."""

    code: int
    message: str


# --- client-to-server frames -------------------------------------------
# Confirmed from a DevTools HAR export of a real practice draft (2026-08-26,
# see docs/notes/ws-protocol.md) -- captured from ESPN's own client, never
# guessed. Parsed here so the decoder can be verified end to end against
# that capture; nothing in this module ever sends a frame.


@dataclass(frozen=True)
class Ping:
    payload: str  # e.g. "PING%201787783293012" -- literal text, not URL-decoded


@dataclass(frozen=True)
class BidCommand:
    """Our own outgoing bid: BID <playerId> <amount>. Distinct from the
    5-field `Bid` broadcast the server sends to the whole room."""

    player_id: int
    amount: int


@dataclass(frozen=True)
class Nominate:
    player_id: int
    opening_bid: int


@dataclass(frozen=True)
class Prenominate:
    """Our full prenomination queue, resent whenever it changes. Each pair
    is (playerId, flag); the flag has been a constant 1 in every captured
    example, so its meaning beyond that isn't confirmed."""

    entries: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class AutoNomination:
    """Sent alongside a same-player Nominate a moment later in the one
    capture that has it -- looks like arming a fallback nomination, but the
    exact trigger/relationship isn't confirmed."""

    player_id: int


@dataclass(frozen=True)
class WsError:
    raw: str
    reason: str


Event = (
    Autodraft | Init | Token | Joined | Left | Pong | BidAck | DraftList | State | Clock
    | AutoSuggest | Passed | Bid | Sold | Nomination | Error | Ping | BidCommand | Nominate
    | Prenominate | AutoNomination | WsError
)


def _bool(field: str) -> bool:
    if field not in ("true", "false"):
        raise ValueError(f"expected true/false, got {field!r}")
    return field == "true"


def parse_frame(raw: str) -> Event:
    msg = raw.strip()
    if not msg:
        return WsError(msg, "empty frame")
    kind, *fields = msg.split(" ")
    try:
        if kind == "AUTODRAFT":
            team_id, enabled = fields
            return Autodraft(int(team_id), _bool(enabled))
        if kind == "INIT":
            # INIT <base64> <2048 literal '#' chars>. The '#' run is ESPN's
            # own padding, not part of the blob -- field 0 is the whole thing.
            return Init(fields[0] if fields else "")
        if kind == "TOKEN":
            (token,) = fields
            game_id, league_id, team_id, swid, session_id = token.split(":")
            return Token(int(game_id), int(league_id), int(team_id), swid, session_id)
        if kind == "JOINED":
            team_id, swid = fields
            return Joined(int(team_id), swid)
        if kind == "LEFT":
            team_id, swid, flag = fields
            return Left(int(team_id), swid, int(flag))
        if kind == "PONG":
            (payload,) = fields
            return Pong(payload)
        if kind == "BID_ACK":
            team_id, player_id, amount = fields
            return BidAck(int(team_id), int(player_id), int(amount))
        if kind == "DRAFT_LIST":
            return DraftList(tuple(int(f) for f in fields))
        if kind == "STATE":
            (value,) = fields
            return State(int(value))
        if kind == "CLOCK":
            state, remaining_ms = int(fields[0]), int(fields[1])
            if state == 2:
                high_bid_team, player_id, high_bid_amount = fields[2:5]
                return Clock(state, remaining_ms, high_bid_team=int(high_bid_team),
                             player_id=int(player_id), high_bid_amount=int(high_bid_amount))
            if state == 1:
                (nominating_team,) = fields[2:3]
                return Clock(state, remaining_ms, nominating_team=int(nominating_team))
            return Clock(state, remaining_ms)
        if kind == "AUTOSUGGEST":
            (player_id,) = fields
            return AutoSuggest(int(player_id))
        if kind == "PASSED":
            team_id, player_id, auto = fields
            return Passed(int(team_id), int(player_id), _bool(auto))
        if kind == "BID":
            if len(fields) == 2:
                player_id, amount = fields
                return BidCommand(int(player_id), int(amount))
            team_id, player_id, amount, clock_reset_ms, clock_remaining_at_bid_ms = fields
            return Bid(int(team_id), int(player_id), int(amount),
                       int(clock_reset_ms), int(clock_remaining_at_bid_ms))
        if kind == "SOLD":
            team_id, player_id, field3, price, field5 = fields
            return Sold(int(team_id), int(player_id), int(field3), int(price), int(field5))
        if kind == "NOMINATION":
            team_id, clock_reset_ms = fields
            return Nomination(int(team_id), int(clock_reset_ms))
        if kind == "ERROR":
            code, *rest = fields
            return Error(int(code), urllib.parse.unquote_plus(" ".join(rest)))
        if kind == "PING":
            (payload,) = fields
            return Ping(payload)
        if kind == "NOMINATE":
            player_id, opening_bid = fields
            return Nominate(int(player_id), int(opening_bid))
        if kind == "PRENOMINATE":
            if len(fields) % 2 != 0:
                raise ValueError(f"expected an even number of fields, got {len(fields)}")
            entries = tuple((int(fields[i]), int(fields[i + 1])) for i in range(0, len(fields), 2))
            return Prenominate(entries)
        if kind == "AUTO_NOMINATION":
            (player_id,) = fields
            return AutoNomination(int(player_id))
    except (ValueError, IndexError) as exc:
        return WsError(msg, f"malformed {kind} frame: {exc}")
    return WsError(msg, f"unrecognized frame kind: {kind}")


# --- token handling -----------------------------------------------------
# SWID (a brace-wrapped GUID) and TOKEN's trailing sessionId are per-account
# auth material and must never land in a recording on disk. SWID shows up
# bare in more than just the TOKEN frame -- JOINED and LEFT carry one too --
# so this redacts it wherever it appears, not just after a "TOKEN " prefix.

_SWID_RE = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")
_TOKEN_SESSION_RE = re.compile(r"^(TOKEN \d+:\d+:\d+:\{REDACTED-SWID\}:)\d+")


def redact_token(msg: str) -> str:
    msg = _SWID_RE.sub("{REDACTED-SWID}", msg)
    return _TOKEN_SESSION_RE.sub(lambda m: m.group(1) + "REDACTED-SESSION", msg)


def parse_join_url(url: str) -> tuple[str, str]:
    """Pull leagueId and teamId out of a pasted JOIN URL, for logging and the
    Referer header. The token and the rest of the query string are used
    verbatim from `url` -- never reconstructed."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    try:
        league_id = query["2"][0]
        team_id = query["3"][0]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Could not find leagueId/teamId in the join URL: {exc}") from exc
    return league_id, team_id


# --- INIT's pick table -----------------------------------------------------
# INIT's blob carries the room's complete state, but only the pick table is
# decoded here: the full 160-slot roster (config.NUM_TEAMS * ROSTER_SIZE),
# which is what reconnect reconciliation needs. See docs/notes/ws-protocol.md
# for how this layout was confirmed against real captures, and why the
# in-flight nomination in INIT's header is deliberately not decoded (the
# CLOCK frame that follows every INIT already covers it unambiguously).

_PICK_RECORD_STRIDE = 45  # bytes per slot; confirmed against real captures


@dataclass(frozen=True)
class InitPick:
    pick_number: int
    team_id: int
    player_id: int
    price: int


@dataclass(frozen=True)
class InitState:
    league_id: int
    picks: tuple[InitPick, ...]  # completed sales only -- unsold slots are dropped


def parse_init_state(blob: str) -> InitState | None:
    """Decode INIT's base64 blob into the room's full pick table.

    Returns None rather than raising on anything short of a fully validated
    160-slot table -- a partial or misaligned decode must never look like a
    trustworthy one, since the caller uses this to overwrite local state.
    """
    padded = blob + "=" * (-len(blob) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) < 12:
        return None
    league_id = struct.unpack(">I", raw[8:12])[0]
    slot_count = config.NUM_TEAMS * config.ROSTER_SIZE
    signature = struct.pack(">iiI", 1, 3, league_id)
    needle = struct.pack(">I", slot_count) + signature
    start = raw.find(needle)
    if start == -1:
        return None
    table_start = start + 4  # skip the slot-count prefix just matched
    table_end = table_start + slot_count * _PICK_RECORD_STRIDE
    if table_end > len(raw):
        return None
    picks = []
    for slot in range(slot_count):
        offset = table_start + slot * _PICK_RECORD_STRIDE
        record = raw[offset:offset + _PICK_RECORD_STRIDE]
        if record[:12] != signature:
            return None  # not actually a uniform table at this offset
        _, _, _, team_id, pick_number, player_id, _field3, price, _nominator = \
            struct.unpack(">iiiiiiiii", record[:36])
        if player_id != -1:
            picks.append(InitPick(pick_number, team_id, player_id, price))
    return InitState(league_id, tuple(picks))


# --- live client ----------------------------------------------------------

# Shared with the console so it can pick this one alert back out of the
# drained list and clear it once `connected` is true again, without
# weakening any other alert (which stays until manually checked).
DISCONNECT_ALERT_PREFIX = "disconnected from the draft room"


class DraftRoomClient:
    """Background-threaded websocket connection to the live draft room, in
    the same shape as scripts/auction.py's SyncController: a thread that
    only ever reads (plus the automated PING keepalive) and pushes parsed
    events onto a queue, with the main loop the sole consumer -- so there's
    no race with the console's own reads.

    Reconnects on its own, with the same token, if frames stop arriving
    (the watchdog) rather than trusting websocket-client to notice an
    unexpected server-side close -- confirmed necessary by the 2026-08-26
    live rehearsal, see docs/notes/rehearsal-log.md. `alerts` collects messages
    the console should surface loudly (reconnects, unparsed frames, send
    failures) rather than log quietly.

    Nothing is ever sent automatically except PING. `send_bid` and
    `send_nomination` only fire when the console calls them, which only
    happens on Mark's own typed command -- that's the confirmation.
    """

    PING_INTERVAL_S = 15  # confirmed cadence from a HAR capture
    # No real timeout example exists past 65s of total silence (the LEFT
    # rehearsal). This is well short of that with a wide margin for the
    # normal 1s-ish CLOCK cadence, so a stall is caught long before ESPN
    # would otherwise drop the connection.
    LIVENESS_TIMEOUT_S = 45
    RECONNECT_BACKOFF_S = 3
    WATCHDOG_POLL_INTERVAL_S = 5  # how often the watchdog checks for silence

    def __init__(self, join_url: str, cred: config.EspnCredentials, log_path: Path | None = None):
        self.join_url = join_url
        self.cred = cred
        self.league_id, self.team_id = parse_join_url(join_url)
        self.log_path = log_path or Path(__file__).resolve().parents[2] / "data" / \
            f"ws-log-{int(time.time())}.jsonl"
        self.connected = False
        self.reconnect_count = 0
        self.alerts: list[str] = []

        self._ws: websocket.WebSocketApp | None = None
        self._queue: list[Event] = []
        self._alert_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._stop = threading.Event()
        self._last_frame_at = time.monotonic()
        self._fh = self.log_path.open("a")
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            self._ws.close()
        self._fh.close()

    def drain(self) -> list[Event]:
        with self._queue_lock:
            events, self._queue = self._queue, []
        return events

    def drain_alerts(self) -> list[str]:
        with self._alert_lock:
            alerts, self.alerts = self.alerts, []
        return alerts

    def send_bid(self, player_id: int, amount: int) -> None:
        self._send(f"BID {player_id} {amount}\n")

    def send_nomination(self, player_id: int, opening_bid: int) -> None:
        self._send(f"NOMINATE {player_id} {opening_bid}\n")

    def _send(self, frame: str) -> None:
        if self._ws is None or not self.connected:
            raise RuntimeError("not connected to the draft room")
        self._ws.send(frame)
        self._log("send", frame)

    def _alert(self, message: str) -> None:
        with self._alert_lock:
            self.alerts.append(message)

    def _log(self, direction: str, msg: str) -> None:
        self._fh.write(json.dumps({"ts": time.time(), "dir": direction, "msg": redact_token(msg)}) + "\n")
        self._fh.flush()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._connect_once()
            if self._stop.is_set():
                break
            self.reconnect_count += 1
            self._alert(f"{DISCONNECT_ALERT_PREFIX} -- reconnecting (attempt {self.reconnect_count})")
            time.sleep(self.RECONNECT_BACKOFF_S)

    def _connect_once(self) -> None:
        stop_helpers = threading.Event()
        self._last_frame_at = time.monotonic()

        def on_message(ws, message):
            self._last_frame_at = time.monotonic()
            self._log("receive", message)
            event = parse_frame(message)
            if isinstance(event, WsError):
                self._alert(f"unparsed frame: {event.raw!r} ({event.reason})")
            with self._queue_lock:
                self._queue.append(event)

        def on_open(ws):
            self.connected = True
            threading.Thread(target=self._ping_loop, args=(ws, stop_helpers), daemon=True).start()
            threading.Thread(target=self._watchdog, args=(ws, stop_helpers), daemon=True).start()

        def on_error(ws, error):
            self._alert(f"websocket error: {error}")

        def on_close(ws, code, msg):
            self.connected = False
            stop_helpers.set()

        self._ws = websocket.WebSocketApp(
            self.join_url,
            cookie=f"espn_s2={self.cred.espn_s2}; SWID={self.cred.swid}",
            header=[
                "Origin: https://fantasy.espn.com",
                f"Referer: https://fantasy.espn.com/football/draft?leagueId={self.league_id}",
                "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            ],
            on_message=on_message,
            on_open=on_open,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws.run_forever()
        stop_helpers.set()

    def _ping_loop(self, ws: websocket.WebSocketApp, stop_helpers: threading.Event) -> None:
        while not stop_helpers.wait(self.PING_INTERVAL_S):
            # Every sent frame in the HAR capture is newline-terminated, PING
            # included -- omitting it means the server never sends a PONG
            # back (confirmed live, see docs/notes/rehearsal-log.md).
            frame = f"PING PING%20{int(time.time() * 1000)}\n"
            try:
                ws.send(frame)
            except Exception as exc:
                self._alert(f"ping send failed: {exc}")
                return
            self._log("send", frame)

    def _watchdog(self, ws: websocket.WebSocketApp, stop_helpers: threading.Event) -> None:
        while not stop_helpers.wait(self.WATCHDOG_POLL_INTERVAL_S):
            if time.monotonic() - self._last_frame_at > self.LIVENESS_TIMEOUT_S:
                self._alert(f"no frames received in {self.LIVENESS_TIMEOUT_S}s -- forcing reconnect")
                ws.close()
                return


def iter_frames(path: Path, include_sent: bool = False) -> Iterator[str]:
    """Yield each raw received frame string from a recorded JSONL fixture, in
    order. A HAR-derived fixture also holds our own outgoing frames tagged
    "dir": "send" -- skipped by default, since parse_frame's job is
    interpreting what the server sends us. Pass include_sent=True to get
    every frame in the fixture regardless of direction (e.g. to verify the
    decoder against a HAR capture end to end)."""
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not include_sent and row.get("dir", "receive") != "receive":
            continue
        yield row["msg"]
