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
PROJECTIONS_BASE = "https://api.sleeper.com/projections/nfl"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
PLAYER_CACHE = CACHE_DIR / "sleeper_players.json"
PLAYER_CACHE_TTL = 60 * 60 * 24     # one day; Sleeper asks you not to poll this
PROJECTIONS_CACHE_TTL = 60 * 60 * 24     # projections move at most daily

# Sleeper's raw stat keys -> this project's scoring.py field names.
PROJECTION_STAT_MAP = {
    "pass_yd": "passing_yards",
    "pass_td": "passing_tds",
    "pass_int": "interceptions",
    "pass_2pt": "passing_2pt",
    "rush_yd": "rushing_yards",
    "rush_td": "rushing_tds",
    "rush_2pt": "rushing_2pt",
    "rec": "receptions",
    "rec_yd": "receiving_yards",
    "rec_td": "receiving_tds",
    "rec_2pt": "receiving_2pt",
    "fum_lost": "fumbles_lost",
}


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


def _position_projections(season: int, position: str, force_refresh: bool = False) -> list[dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"sleeper_projections_{season}_{position}.json"
    fresh = (
        cache_path.exists()
        and (time.time() - cache_path.stat().st_mtime) < PROJECTIONS_CACHE_TTL
    )
    if fresh and not force_refresh:
        return json.loads(cache_path.read_text())

    response = requests.get(
        f"{PROJECTIONS_BASE}/{season}",
        params={"season_type": "regular", "position[]": position, "order_by": "pts_ppr"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    cache_path.write_text(json.dumps(data))
    return data


def season_projections(
    season: int,
    positions: tuple[str, ...] = ("QB", "RB", "WR", "TE"),
    force_refresh: bool = False,
) -> list[dict]:
    """Full-season projected stat lines, one row per player per position.

    A second, independent projection source alongside ESPN's: these are raw
    stat lines (Rotowire's, per the `company` field), not pre-scored points,
    so `scoring.py` can apply this league's exact rules to them the same way
    it does for ESPN's `projected_breakdown`.
    """
    out = []
    for position in positions:
        for row in _position_projections(season, position, force_refresh=force_refresh):
            player = row.get("player") or {}
            stats = row.get("stats") or {}
            name = " ".join(
                part for part in (player.get("first_name"), player.get("last_name")) if part
            )
            out.append(
                {
                    "name": name,
                    "position": player.get("position"),
                    "pro_team": player.get("team") or row.get("team"),
                    "stats": {
                        mapped: float(stats[key])
                        for key, mapped in PROJECTION_STAT_MAP.items()
                        if key in stats
                    },
                    "adp_2qb": stats.get("adp_2qb"),
                }
            )
    return out


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
