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
class Joined:
    """Our own connection's identity, echoed back by the TOKEN frame."""

    game_id: int
    league_id: int
    team_id: int
    swid: str
    session_id: str


@dataclass(frozen=True)
class Clock:
    """State 2 is live bidding, with the high-bid fields populated; state 3
    is the countdown between nominations, with no high bid to report. Other
    states are unconfirmed -- see docs/draft-ws-plan.md rehearsal question 5."""

    state: int
    remaining_ms: int
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


@dataclass(frozen=True)
class WsError:
    raw: str
    reason: str


Event = (
    Autodraft | Init | Joined | Clock | AutoSuggest | Passed | Bid | Sold | Nomination | WsError
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
            (blob,) = fields
            return Init(blob)
        if kind == "TOKEN":
            (token,) = fields
            game_id, league_id, team_id, swid, session_id = token.split(":")
            return Joined(int(game_id), int(league_id), int(team_id), swid, session_id)
        if kind == "CLOCK":
            state, remaining_ms = int(fields[0]), int(fields[1])
            if state == 2:
                high_bid_team, player_id, high_bid_amount = fields[2:5]
                return Clock(state, remaining_ms, int(high_bid_team),
                             int(player_id), int(high_bid_amount))
            return Clock(state, remaining_ms)
        if kind == "AUTOSUGGEST":
            (player_id,) = fields
            return AutoSuggest(int(player_id))
        if kind == "PASSED":
            team_id, player_id, auto = fields
            return Passed(int(team_id), int(player_id), _bool(auto))
        if kind == "BID":
            team_id, player_id, amount, clock_reset_ms, clock_remaining_at_bid_ms = fields
            return Bid(int(team_id), int(player_id), int(amount),
                       int(clock_reset_ms), int(clock_remaining_at_bid_ms))
        if kind == "SOLD":
            team_id, player_id, field3, price, field5 = fields
            return Sold(int(team_id), int(player_id), int(field3), int(price), int(field5))
        if kind == "NOMINATION":
            team_id, clock_reset_ms = fields
            return Nomination(int(team_id), int(clock_reset_ms))
    except (ValueError, IndexError) as exc:
        return WsError(msg, f"malformed {kind} frame: {exc}")
    return WsError(msg, f"unrecognized frame kind: {kind}")


def iter_frames(path: Path) -> Iterator[str]:
    """Yield each raw frame string from a recorded JSONL fixture, in order."""
    for line in path.read_text().splitlines():
        if line.strip():
            yield json.loads(line)["msg"]
