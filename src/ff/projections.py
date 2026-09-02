"""Outside projection sources, for cross-checking the ESPN-derived board.

Every source here returns full-season **raw stat lines**, never someone
else's pre-scored points column, so `scoring.py` can apply this league's
exact rules to them. That controls scoring differences away: a disagreement
with ESPN is then a genuine projection disagreement, not a 1QB-vs-2QB or
half-vs-full-PPR artifact.

Sources:

- CBS Sports season projections (server-rendered HTML, one page per position
  covering roughly the top 100 at QB/RB/WR/TE and every kicker).
- FFToday season projections (server-rendered HTML, 50 rows per page for
  QB/RB/WR/TE; kickers are pre-scored only, so they are not pulled).
- FantasyFootballCalculator's 2QB ADP feed (JSON), the one public ADP source
  that is both 10-team and 2QB. Draft position rather than dollars, but it
  is the market's own ordering for this exact format.

Sleeper's Rotowire projections are the fourth source and already live in
`sleeper.season_projections()`.

Pages are cached under data/cache/ for a few hours so a re-run during the
same session does not re-fetch; pass `force_refresh=True` to refetch.
"""

from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from pathlib import Path

import requests

from . import scoring

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CACHE_TTL = 60 * 60 * 6
FETCH_PAUSE = 1.5
RETRY_PAUSE = 8.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

CBS_URL = "https://www.cbssports.com/fantasy/football/stats/{pos}/{season}/season/projections/ppr/"
FFTODAY_URL = (
    "https://www.fftoday.com/rankings/playerproj.php"
    "?Season={season}&PosID={posid}&LeagueID=1&order_by=FFPts&sort_order=DESC&cur_page={page}"
)
FFTODAY_POSITION_IDS = {"QB": 10, "RB": 20, "WR": 30, "TE": 40}
FFC_URL = "https://fantasyfootballcalculator.com/api/v1/adp/{fmt}?teams={teams}&year={season}"


def normalize_name(name: str) -> str:
    """Join key across sources: ASCII-folded, lowercased, no punctuation,
    no generational suffix. 'Amon-Ra St. Brown' and 'Travis Etienne Jr.'
    match their unsuffixed, unpunctuated forms on every source."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    folded = re.sub(r"[.'’-]", "", folded.lower())
    return re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", folded.strip())


def _fetch(url: str, cache_name: str, force_refresh: bool = False) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / cache_name
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL
    if fresh and not force_refresh:
        return path.read_text()
    # FFToday answers a burst of page requests with 403s, so pace every fetch
    # and give one transient failure a second chance after a pause.
    time.sleep(FETCH_PAUSE)
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    if response.status_code in (403, 429, 503):
        time.sleep(RETRY_PAUSE)
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    response.raise_for_status()
    path.write_text(response.text)
    return response.text


def _clean(cell: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>|\s+", " ", cell)).strip()


def _num(cell: str) -> float:
    try:
        return float(cell.replace(",", ""))
    except ValueError:
        return 0.0


def _rows(page: str) -> list[list[str]]:
    """Every <tr> on the page as a list of cleaned <td> strings."""
    out = []
    for chunk in re.split(r"<tr\b", page, flags=re.I)[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.S | re.I)
        if cells:
            out.append([_clean(c) for c in cells])
    return out


# --- CBS Sports ---------------------------------------------------------------

# CBS's player cell reads "J. Allen QB BUF Josh Allen QB BUF": short name,
# then full name, each followed by position and team.
_CBS_PLAYER = re.compile(
    r".*?\s+(?:QB|RB|WR|TE|K)\s+[A-Z]{2,3}\s+(.+?)\s+(QB|RB|WR|TE|K)\s+([A-Z]{2,3})$"
)


def _cbs_stats(pos: str, v: list[float]) -> dict[str, float] | None:
    """Map one CBS row's numeric cells onto scoring.py field names.

    Column orders are CBS's, per position, with the games-played column
    first and fantasy points / points-per-game last.
    """
    if pos == "QB" and len(v) == 15:
        _, _, _, pyd, _, ptd, pint, _, _, ryd, _, rtd, fl, _, _ = v
        return dict(passing_yards=pyd, passing_tds=ptd, interceptions=pint,
                    rushing_yards=ryd, rushing_tds=rtd, fumbles_lost=fl)
    if pos == "RB" and len(v) == 14:
        _, _, ryd, _, rtd, _, rec, recyd, _, _, rectd, fl, _, _ = v
        return dict(rushing_yards=ryd, rushing_tds=rtd, receptions=rec,
                    receiving_yards=recyd, receiving_tds=rectd, fumbles_lost=fl)
    if pos == "WR" and len(v) == 14:
        _, _, rec, recyd, _, _, rectd, _, ryd, _, rtd, fl, _, _ = v
        return dict(rushing_yards=ryd, rushing_tds=rtd, receptions=rec,
                    receiving_yards=recyd, receiving_tds=rectd, fumbles_lost=fl)
    if pos == "TE" and len(v) == 10:
        _, _, rec, recyd, _, _, rectd, fl, _, _ = v
        return dict(receptions=rec, receiving_yards=recyd, receiving_tds=rectd,
                    fumbles_lost=fl)
    if pos == "K" and len(v) == 18:
        _, fgm, fga, _, m19, _, m29, _, m39, _, m49, _, m50, _, xpm, _, _, _ = v
        return dict(pat_made=xpm, fg_missed=fga - fgm, fg_0_39=m19 + m29 + m39,
                    fg_40_49=m49, fg_50_59=m50)
    return None


def cbs_projections(
    season: int,
    positions: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K"),
    force_refresh: bool = False,
) -> list[dict]:
    """CBS season projections as scored rows: name, position, pro_team,
    stats (scoring.py names), league_points, source_points (CBS's own PPR
    column, for a sanity read only)."""
    out = []
    for pos in positions:
        page = _fetch(CBS_URL.format(pos=pos, season=season),
                      f"cbs_projections_{season}_{pos}.html", force_refresh)
        for cells in _rows(page):
            match = _CBS_PLAYER.match(cells[0]) if cells else None
            if not match:
                continue
            stats = _cbs_stats(pos, [_num(c) for c in cells[1:]])
            if stats is None:
                continue
            out.append({
                "name": match.group(1),
                "position": match.group(2),
                "pro_team": match.group(3),
                "stats": stats,
                "league_points": scoring.score(pos, stats),
                "source_points": _num(cells[-2]),
            })
    return out


# --- FFToday -------------------------------------------------------------------

def _fftoday_stats(pos: str, v: list[float]) -> dict[str, float] | None:
    """FFToday's columns after name and team: bye week first, its own
    fantasy points last."""
    if pos == "QB" and len(v) == 10:
        _, _, _, pyd, ptd, pint, _, ryd, rtd, _ = v
        return dict(passing_yards=pyd, passing_tds=ptd, interceptions=pint,
                    rushing_yards=ryd, rushing_tds=rtd)
    if pos == "RB" and len(v) == 8:
        _, _, ryd, rtd, rec, recyd, rectd, _ = v
        return dict(rushing_yards=ryd, rushing_tds=rtd, receptions=rec,
                    receiving_yards=recyd, receiving_tds=rectd)
    if pos == "WR" and len(v) == 8:
        _, rec, recyd, rectd, _, ryd, rtd, _ = v
        return dict(rushing_yards=ryd, rushing_tds=rtd, receptions=rec,
                    receiving_yards=recyd, receiving_tds=rectd)
    if pos == "TE" and len(v) == 5:
        _, rec, recyd, rectd, _ = v
        return dict(receptions=rec, receiving_yards=recyd, receiving_tds=rectd)
    return None


def fftoday_projections(
    season: int,
    positions: tuple[str, ...] = ("QB", "RB", "WR", "TE"),
    pages: int = 3,
    force_refresh: bool = False,
) -> list[dict]:
    """FFToday season projections as scored rows, same shape as
    `cbs_projections`. Pages past the end of a position's list come back
    with no player rows and are skipped."""
    out = []
    for pos in positions:
        for page_no in range(pages):
            page = _fetch(
                FFTODAY_URL.format(season=season, posid=FFTODAY_POSITION_IDS[pos], page=page_no),
                f"fftoday_projections_{season}_{pos}_{page_no}.html", force_refresh,
            )
            found = False
            for chunk in re.split(r"<tr\b", page, flags=re.I)[1:]:
                if "/stats/players/" not in chunk:
                    continue
                cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.S | re.I)]
                cells = [c for c in cells if c]      # leading icon cell is blank
                if len(cells) < 4:
                    continue
                stats = _fftoday_stats(pos, [_num(c) for c in cells[2:]])
                if stats is None:
                    continue
                found = True
                out.append({
                    "name": cells[0],
                    "position": pos,
                    "pro_team": cells[1],
                    "stats": stats,
                    "league_points": scoring.score(pos, stats),
                    "source_points": _num(cells[-1]),
                })
            if not found:
                break
    return out


# --- FantasyFootballCalculator ADP ---------------------------------------------

def ffc_adp(
    season: int,
    teams: int = 10,
    fmt: str = "2qb",
    force_refresh: bool = False,
) -> dict:
    """FantasyFootballCalculator's ADP for the given format and team count.

    Returns {"meta": {...drafts counted, date range...}, "players": [...]}
    with players in ADP order, each carrying name, position, team, adp,
    times_drafted, high, low, stdev.
    """
    raw = _fetch(FFC_URL.format(fmt=fmt, teams=teams, season=season),
                 f"ffc_adp_{season}_{fmt}_{teams}.json", force_refresh)
    data = json.loads(raw)
    return {"meta": data.get("meta", {}), "players": data.get("players", [])}
