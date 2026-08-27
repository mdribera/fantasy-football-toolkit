"""Parser for ESPN's live draft-room websocket protocol.

The draft room speaks plain space-delimited text frames over a websocket at
wss://fantasydraft.espn.com/game-{gameId}/league-{leagueId}/JOIN -- a
completely separate host from the mDraftDetail REST feed draft_sync.py polls,
and the only one that updates during a live auction. See
docs/draft-ws-plan.md for the reverse-engineering notes, field meanings, and
open questions.

parse_frame() never raises: an unrecognized or malformed frame comes back as
WsError so a live connection can log and move on instead of crashing on a
frame shape not yet seen.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class Autodraft:
    team_id: int
    enabled: bool


@dataclass(frozen=True)
class Init:
    blob: str  # base64, undecoded -- full decode is a stretch goal, see the plan


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
    (only ever seen as 1). See docs/draft-ws-plan.md."""

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
           unconfirmed -- see docs/draft-ws-plan.md rehearsal question 6.
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
    and tracking sales doesn't need them. See docs/draft-ws-plan.md."""

    team_id: int
    player_id: int
    field3: int
    price: int
    field5: int


@dataclass(frozen=True)
class Nomination:
    team_id: int
    clock_reset_ms: int


# --- client-to-server frames -------------------------------------------
# Confirmed from a DevTools HAR export of a real practice draft (2026-08-26,
# see docs/draft-ws-plan.md) -- captured from ESPN's own client, never
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
    | AutoSuggest | Passed | Bid | Sold | Nomination | Ping | BidCommand | Nominate
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
            # The blob itself can apparently contain literal spaces, so
            # reassemble it rather than requiring exactly one field.
            return Init(" ".join(fields))
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
