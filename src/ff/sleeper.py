"""Sleeper's public read-only API.

No authentication, no key, free for non-commercial use. Useful here as a
cross-check on ESPN: it carries a cleaner injury feed and a trending add/drop
signal that is the single best early-warning system for waiver season.

Rate limit is roughly 1000 calls/minute; the player dump is ~5MB so it is
cached to disk and refreshed at most daily.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

BASE = "https://api.sleeper.app/v1"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
PLAYER_CACHE = CACHE_DIR / "sleeper_players.json"
PLAYER_CACHE_TTL = 60 * 60 * 24     # one day; Sleeper asks you not to poll this


def _get(path: str, **params):
    response = requests.get(f"{BASE}/{path}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def all_players(force_refresh: bool = False) -> dict:
    """The full NFL player universe keyed by Sleeper player ID."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fresh = (
        PLAYER_CACHE.exists()
        and (time.time() - PLAYER_CACHE.stat().st_mtime) < PLAYER_CACHE_TTL
    )
    if fresh and not force_refresh:
        return json.loads(PLAYER_CACHE.read_text())

    data = _get("players/nfl")
    PLAYER_CACHE.write_text(json.dumps(data))
    return data


def trending(kind: str = "add", lookback_hours: int = 24, limit: int = 50) -> list[dict]:
    """Most-added or most-dropped players across all of Sleeper.

    Leading indicator for waiver runs: a player spiking here on Tuesday is
    usually the player your leaguemates bid FAAB on come Wednesday.
    """
    rows = _get(
        f"players/nfl/trending/{kind}",
        lookback_hours=lookback_hours,
        limit=limit,
    )
    universe = all_players()
    out = []
    for row in rows:
        player = universe.get(row["player_id"], {})
        out.append(
            {
                "name": player.get("full_name") or player.get("last_name", "?"),
                "position": player.get("position"),
                "team": player.get("team"),
                "injury_status": player.get("injury_status"),
                "count": row.get("count", 0),
            }
        )
    return out


def injuries() -> list[dict]:
    """Everyone currently carrying an injury designation."""
    universe = all_players()
    out = [
        {
            "name": p.get("full_name"),
            "position": p.get("position"),
            "team": p.get("team"),
            "status": p.get("injury_status"),
            "body_part": p.get("injury_body_part"),
            "notes": p.get("injury_notes"),
        }
        for p in universe.values()
        if p.get("injury_status") and p.get("team") and p.get("position") in
        ("QB", "RB", "WR", "TE", "K")
    ]
    return sorted(out, key=lambda r: (r["position"] or "", r["name"] or ""))


def find(name: str) -> list[dict]:
    """Fuzzy-ish lookup by name fragment."""
    needle = name.lower()
    return [
        {
            "id": pid,
            "name": p.get("full_name"),
            "position": p.get("position"),
            "team": p.get("team"),
            "status": p.get("injury_status"),
        }
        for pid, p in all_players().items()
        if needle in (p.get("full_name") or "").lower()
    ]
