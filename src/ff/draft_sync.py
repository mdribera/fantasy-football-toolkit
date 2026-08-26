"""Live draft feed: poll ESPN's own draft-detail API instead of the browser.

`view=mDraftDetail` returns the full auction skeleton -- one row per roster
slot across all ten teams, whether filled or not -- as plain JSON. A slot with
`playerId == -1` is unfilled; anything else is a completed purchase with the
price paid and buying team already attached:

    {"id": 1, "overallPickNumber": 1, "nominatingTeamId": 11,
     "teamId": -1, "playerId": -1, "bidAmount": 0}

This does not go through espn_api's `League.draft` / `_fetch_draft()`: that
method returns early unless `draftDetail.drafted` is true, which won't be the
case until the auction is over. We want it live, so this module talks to the
endpoint directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

import json
import requests

from . import config

DRAFT_DETAIL_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leagues/{league_id}"
)


class DraftFeedError(RuntimeError):
    """The feed could not be reached or ESPN returned something unusable.

    Callers should treat this as "sync is degraded," not "the program crashed"
    -- the console's job during a live auction is to keep working with manual
    entry, not to raise.
    """


@dataclass
class ResolvedPick:
    """A completed auction slot, with the player resolved to our board."""

    espn_pick_id: int
    player: str
    position: str
    price: int
    team: str


def fetch_picks(cred: config.EspnCredentials | None = None) -> dict:
    """Raw GET of the draft-detail view. Returns the `draftDetail` object."""
    cred = cred or config.EspnCredentials()
    if not cred.is_configured:
        raise DraftFeedError("ESPN_LEAGUE_ID is not set.")
    url = DRAFT_DETAIL_URL.format(season=config.SEASON, league_id=cred.league_id)
    cookies = {}
    if cred.has_private_auth:
        cookies = {"espn_s2": cred.espn_s2, "SWID": cred.swid}
    try:
        resp = requests.get(url, params={"view": "mDraftDetail"}, cookies=cookies, timeout=10)
    except requests.RequestException as exc:
        raise DraftFeedError(f"Could not reach ESPN: {exc}") from exc
    if not resp.ok:
        raise DraftFeedError(f"ESPN returned {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise DraftFeedError(f"ESPN response was not JSON: {exc}") from exc
    detail = data.get("draftDetail")
    if detail is None:
        raise DraftFeedError("Response had no draftDetail -- wrong league or view?")
    return detail


def completed(picks: Iterable[dict]) -> list[dict]:
    """Filled slots only, in draft order."""
    filled = [p for p in picks if p.get("playerId", -1) != -1 and p.get("teamId", -1) != -1]
    return sorted(filled, key=lambda p: p.get("overallPickNumber", 0))


class PlayerResolver:
    """Resolves an ESPN playerId to a name and position on our value board.

    Values.json covers the ~600 players anyone would actually nominate; a
    fallback to the live API handles the rare player outside that pool
    (a just-signed free agent, a practice-squad callup). Anyone unresolved
    still gets recorded -- with a placeholder name and position "?" -- because
    a wrong budget is worse than a labeled unknown.
    """

    def __init__(self, values_path: Path | None = None, cred: config.EspnCredentials | None = None):
        self._by_id: dict[int, tuple[str, str]] = {}
        if values_path and values_path.exists():
            for row in json.loads(values_path.read_text()):
                espn_id = row.get("espn_id")
                if espn_id is not None:
                    self._by_id[espn_id] = (row["name"], row["position"])
        self._cred = cred

    def resolve(self, player_id: int) -> tuple[str, str]:
        if player_id in self._by_id:
            return self._by_id[player_id]
        looked_up = self._lookup_live(player_id)
        if looked_up:
            self._by_id[player_id] = looked_up
            return looked_up
        return f"ESPN#{player_id}", "?"

    def _lookup_live(self, player_id: int) -> tuple[str, str] | None:
        try:
            from .espn import connect, _normalize_position

            league = connect(self._cred)
            info = league.player_info(playerId=player_id)
        except Exception:
            return None
        if not info:
            return None
        return info.name, _normalize_position(getattr(info, "position", "?"))


def to_resolved(picks: Iterable[dict], resolver: PlayerResolver) -> list[ResolvedPick]:
    out = []
    for pick in picks:
        name, position = resolver.resolve(pick["playerId"])
        out.append(
            ResolvedPick(
                espn_pick_id=pick["id"],
                player=name,
                position=position,
                price=pick.get("bidAmount", 0),
                team=config.TEAMS.get(pick["teamId"], f"TEAM{pick['teamId']}"),
            )
        )
    return out


PickSource = Callable[[], dict]  # returns a draftDetail dict, e.g. fetch_picks


class DraftFeed:
    """Tracks which picks have already been surfaced, so `poll()` only ever
    returns what's new since the last call."""

    def __init__(self, source: PickSource, resolver: PlayerResolver):
        self._source = source
        self._resolver = resolver
        self._seen: set[int] = set()

    def poll(self) -> list[ResolvedPick]:
        return self.poll_snapshot(self._source())

    def poll_snapshot(self, detail: dict) -> list[ResolvedPick]:
        """Diff a draftDetail snapshot against what's already been surfaced.

        Split out from `poll()` so replay can feed pre-fetched snapshots (from
        a JSONL fixture) through the identical dedup logic the live poller
        uses, without needing a fake PickSource per line.
        """
        fresh = [p for p in completed(detail.get("picks", [])) if p["id"] not in self._seen]
        for p in fresh:
            self._seen.add(p["id"])
        return to_resolved(fresh, self._resolver)

    def in_progress(self) -> bool | None:
        """Best-effort read of ESPN's own in-progress flag, for the caller to
        decide whether a quiet feed is expected (draft hasn't started) or a
        problem (it's live and nothing is coming through)."""
        try:
            detail = self._source()
        except DraftFeedError:
            return None
        return detail.get("inProgress")


def replay_snapshots(path: Path) -> Iterator[dict]:
    """Yield each draftDetail snapshot from a recorded JSONL fixture, in
    order. Feed these to `DraftFeed.poll_snapshot()` to replay a recording
    through the same dedup logic the live poller uses, with no network.
    """
    for line in path.read_text().splitlines():
        if line.strip():
            yield json.loads(line)
