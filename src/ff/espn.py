"""ESPN Fantasy API access, wrapped so the rest of the project never touches
espn_api types directly.

The league is public, so reads work with just a league ID. Private auth
(SWID + espn_s2) additionally exposes draft detail, FAAB balances and your own
team's view.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from espn_api.football import League

from . import config, scoring

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"


@dataclass
class PlayerRow:
    """Flat, serializable player record scored under league rules."""

    name: str
    position: str
    pro_team: str
    espn_id: int | None = None
    projected_points: float = 0.0     # ESPN's own projection, league-scored
    league_points: float = 0.0        # our scoring applied to raw projections
    percent_owned: float = 0.0
    injury_status: str = ""
    bye_week: int | None = None
    espn_avg: float | None = None     # ESPN's own average auction value across
                                       # its user base -- a market anchor read,
                                       # not a price, and built for a 1QB market
    rostered_by: str | None = None
    draft_cost: int | None = None     # auction price paid, if drafted


def connect(cred: config.EspnCredentials | None = None) -> League:
    """Open a league connection. Falls back to public read if no cookies."""
    cred = cred or config.EspnCredentials()
    if not cred.is_configured:
        raise RuntimeError(
            "ESPN_LEAGUE_ID is not set. Copy .env.example to .env and fill it in."
        )
    kwargs = {"league_id": int(cred.league_id), "year": config.SEASON}
    if cred.has_private_auth:
        kwargs["espn_s2"] = cred.espn_s2
        kwargs["swid"] = cred.swid
    return League(**kwargs)


def _normalize_position(pos: str) -> str:
    return "D/ST" if pos in ("D/ST", "DST", "TEAM") else pos


def to_row(player, rostered_by: str | None = None) -> PlayerRow:
    """Convert an espn_api Player into a PlayerRow.

    `projected_total_points` is the authoritative number: ESPN computes it
    inside the league context, so it already reflects this league's scoring
    (verified -- it prices passing touchdowns at 4 and receptions at 1, not
    ESPN's defaults). It is not a third-party site's generic "PPR points"
    column, which is what `scoring.py` exists to avoid trusting.

    The raw `projected_breakdown` is deliberately not used for scoring. It
    mixes season totals with per-game values under partially-translated stat
    keys, so recomputing from it produces numbers that are quietly wrong.
    """
    position = _normalize_position(getattr(player, "position", "") or "")

    return PlayerRow(
        name=getattr(player, "name", ""),
        position=position,
        pro_team=getattr(player, "proTeam", "") or "",
        espn_id=getattr(player, "playerId", None),
        projected_points=float(getattr(player, "projected_total_points", 0.0) or 0.0),
        league_points=0.0,   # reserved for scoring.py recomputation from a trusted stat line
        percent_owned=float(getattr(player, "percent_owned", 0.0) or 0.0),
        injury_status=getattr(player, "injuryStatus", "") or "",
        rostered_by=rostered_by,
    )


_BREAKDOWN_ALIASES = {
    "passingYards": "passing_yards",
    "passingTouchdowns": "passing_tds",
    "passingInterceptions": "interceptions",
    "rushingYards": "rushing_yards",
    "rushingTouchdowns": "rushing_tds",
    "receivingYards": "receiving_yards",
    "receivingTouchdowns": "receiving_tds",
    "receivingReceptions": "receptions",
    "fumbles": "fumbles_lost",
    "lostFumbles": "fumbles_lost",
}


def _coerce_breakdown(breakdown: dict) -> dict:
    out: dict[str, float] = {}
    for key, value in breakdown.items():
        name = _BREAKDOWN_ALIASES.get(key, key)
        try:
            out[name] = out.get(name, 0.0) + float(value)
        except (TypeError, ValueError):
            continue
    return out


def free_agents(league: League, size: int = 400) -> list[PlayerRow]:
    return [to_row(p) for p in league.free_agents(size=size)]


def rostered_players(league: League) -> list[PlayerRow]:
    rows: list[PlayerRow] = []
    for team in league.teams:
        for player in team.roster:
            rows.append(to_row(player, rostered_by=team.team_name))
    return rows


def bye_weeks(league: League) -> dict[str, int]:
    """Pro team abbreviation -> bye week, from ESPN's schedule view.

    Keyed on the same abbreviation PlayerRow.pro_team already carries, so
    joining this onto a pool of players is a plain dict lookup.
    """
    try:
        data = league.espn_request.get_pro_schedule()
    except Exception:
        return {}
    teams = data.get("settings", {}).get("proTeams", [])
    return {
        t["abbrev"]: t["byeWeek"]
        for t in teams
        if t.get("abbrev") and t.get("byeWeek")
    }


def auction_averages(league: League, size: int = 600) -> dict[int, float]:
    """ESPN player id -> its ownership.auctionValueAverage across ESPN's user
    base. This is a market-anchor read for a 1QB format, never a price for
    this league -- callers must not present it as one.

    Returns an empty dict on any failure so a bad pull degrades the caller's
    column to missing data rather than blocking a values.json rebuild.
    """
    try:
        filt = {"players": {"limit": size,
                             "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}}
        data = league.espn_request.league_get(
            params={"view": "kona_player_info"},
            headers={"x-fantasy-filter": json.dumps(filt)},
        )
    except Exception:
        return {}
    out: dict[int, float] = {}
    for entry in data.get("players", []):
        player = entry.get("player", {})
        avg = player.get("ownership", {}).get("auctionValueAverage")
        player_id = player.get("id")
        if player_id is not None and avg is not None:
            out[player_id] = float(avg)
    return out


def full_player_pool(league: League, fa_size: int = 500) -> list[PlayerRow]:
    """Everyone rostered plus the top free agents, with bye week and ESPN's
    average auction value joined on afterward."""
    rows = rostered_players(league) + free_agents(league, size=fa_size)
    byes = bye_weeks(league)
    averages = auction_averages(league)
    for row in rows:
        row.bye_week = byes.get(row.pro_team)
        if row.espn_id is not None:
            row.espn_avg = averages.get(row.espn_id)
    return rows


def draft_results(league: League) -> list[dict]:
    """Auction draft results with prices paid, once the draft has happened."""
    results = []
    for pick in getattr(league, "draft", []) or []:
        results.append(
            {
                "player": getattr(pick, "playerName", ""),
                "player_id": getattr(pick, "playerId", None),
                "team": getattr(getattr(pick, "team", None), "team_name", ""),
                "round": getattr(pick, "round_num", None),
                "pick": getattr(pick, "round_pick", None),
                "bid_amount": getattr(pick, "bid_amount", None),
                "keeper": getattr(pick, "keeper_status", False),
            }
        )
    return results


def team_summary(league: League) -> list[dict]:
    out = []
    for team in league.teams:
        out.append(
            {
                "team_id": team.team_id,
                "name": team.team_name,
                "owner": getattr(team, "owners", None),
                "wins": team.wins,
                "losses": team.losses,
                "points_for": team.points_for,
                "acquisition_budget_spent": getattr(team, "acquisition_budget_spent", None),
                "roster": [p.name for p in team.roster],
            }
        )
    return out


def cache_write(name: str, payload) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{name}.json"
    serializable = [asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in payload] \
        if isinstance(payload, Iterable) and not isinstance(payload, (dict, str)) else payload
    path.write_text(json.dumps(serializable, indent=2, default=str))
    return path


def cache_read(name: str):
    path = CACHE_DIR / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())
