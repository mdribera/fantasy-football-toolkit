"""Exact scoring rules for the SuperFun Football League.

Why this exists rather than trusting a projection site's "PPR points" column:
this league's D/ST scoring includes a yards-allowed table on top of the usual
points-allowed table, which most standard-scoring projections ignore entirely.
Kicker scoring is also distance-tiered. Applying these rules to raw projected
stat lines gives numbers that mean something in this league.
"""

from __future__ import annotations

from typing import Mapping

# --- Offense -----------------------------------------------------------------

PASSING = {
    "passing_yards": 0.04,        # 1 pt / 25 yds
    "passing_tds": 4.0,
    "interceptions": -2.0,
    "passing_2pt": 2.0,
}

RUSHING = {
    "rushing_yards": 0.1,
    "rushing_tds": 6.0,
    "rushing_2pt": 2.0,
}

RECEIVING = {
    "receiving_yards": 0.1,
    "receptions": 1.0,            # full PPR
    "receiving_tds": 6.0,
    "receiving_2pt": 2.0,
}

MISC = {
    "fumbles_lost": -2.0,
    "fumble_recovery_td": 6.0,
    "kick_return_td": 6.0,
    "punt_return_td": 6.0,
}

# --- Kicking -----------------------------------------------------------------

KICKING = {
    "pat_made": 1.0,
    "fg_missed": -1.0,
    "fg_0_39": 3.0,
    "fg_40_49": 4.0,
    "fg_50_59": 5.0,
    "fg_60_plus": 5.0,            # no extra credit beyond 50+
}

# --- Team Defense / Special Teams --------------------------------------------

DST_EVENTS = {
    "sacks": 1.0,
    "interceptions": 2.0,
    "fumble_recoveries": 2.0,
    "safeties": 2.0,
    "blocked_kicks": 2.0,         # blocked punt, PAT or FG
    "defensive_tds": 6.0,         # INT / fumble return
    "return_tds": 6.0,            # kickoff / punt / blocked-kick return
    "two_pt_returns": 2.0,
    "one_pt_safeties": 1.0,
}

# (inclusive_low, inclusive_high, points). Note the deliberate gap at 18-27:
# the league defines no tier there, so it scores 0.
POINTS_ALLOWED_TIERS = [
    (0, 0, 5.0),
    (1, 6, 4.0),
    (7, 13, 3.0),
    (14, 17, 1.0),
    (18, 27, 0.0),
    (28, 34, -1.0),
    (35, 45, -3.0),
    (46, 10_000, -5.0),
]

# Same story at 300-349: no tier defined, so it scores 0.
YARDS_ALLOWED_TIERS = [
    (0, 99, 5.0),
    (100, 199, 3.0),
    (200, 299, 2.0),
    (300, 349, 0.0),
    (350, 399, -1.0),
    (400, 449, -3.0),
    (450, 499, -5.0),
    (500, 549, -6.0),
    (550, 100_000, -7.0),
]


def _tier_points(value: float, tiers) -> float:
    for low, high, pts in tiers:
        if low <= value <= high:
            return pts
    return 0.0


def _apply(stats: Mapping[str, float], table: Mapping[str, float]) -> float:
    return sum(stats.get(stat, 0.0) * weight for stat, weight in table.items())


def score_offense(stats: Mapping[str, float]) -> float:
    """Score a QB/RB/WR/TE stat line."""
    return (
        _apply(stats, PASSING)
        + _apply(stats, RUSHING)
        + _apply(stats, RECEIVING)
        + _apply(stats, MISC)
    )


def score_kicker(stats: Mapping[str, float]) -> float:
    return _apply(stats, KICKING)


def score_dst(stats: Mapping[str, float]) -> float:
    """Score a team defense. Expects `points_allowed` and `yards_allowed`
    as per-game values already summed appropriately by the caller."""
    total = _apply(stats, DST_EVENTS)
    total += _tier_points(stats.get("points_allowed", 0.0), POINTS_ALLOWED_TIERS)
    total += _tier_points(stats.get("yards_allowed", 0.0), YARDS_ALLOWED_TIERS)
    return total


def score(position: str, stats: Mapping[str, float]) -> float:
    """Dispatch on position."""
    if position in ("D/ST", "DST"):
        return score_dst(stats)
    if position == "K":
        return score_kicker(stats)
    return score_offense(stats)


# --- ESPN stat-ID bridge -----------------------------------------------------
# ESPN returns projections as a dict keyed by numeric stat ID. These are the
# IDs that map onto rules this league actually scores.
ESPN_STAT_IDS = {
    3: "passing_yards",
    4: "passing_tds",
    20: "interceptions",
    24: "rushing_yards",
    25: "rushing_tds",
    42: "receiving_yards",
    43: "receiving_tds",
    53: "receptions",
    68: "fumbles_lost",
    72: "passing_2pt",
    62: "rushing_2pt",
    63: "receiving_2pt",
}


def from_espn_stats(position: str, espn_stats: Mapping) -> float:
    """Score a raw ESPN projected stat dict under this league's rules."""
    translated = {
        name: float(espn_stats.get(stat_id, 0) or 0)
        for stat_id, name in ESPN_STAT_IDS.items()
    }
    return score(position, translated)
