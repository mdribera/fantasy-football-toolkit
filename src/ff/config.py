"""League constants for the SuperFun Football League.

Everything here is derived from league-settings.md. This is the single source
of truth for format assumptions; the scoring and valuation modules read from it
rather than hardcoding numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

LEAGUE_NAME = "SuperFun Football League"
SEASON = int(os.getenv("ESPN_SEASON", "2026"))
NUM_TEAMS = 10
SALARY_CAP = 200
FAAB_BUDGET = 100

# Draft: Sep 2, 2026 @ 12:00 PM PDT
DRAFT_DATE = "2026-09-02T12:00:00-07:00"

# Starting lineup. Note QB=2 is a true 2QB requirement, not superflex: the FLEX
# slot accepts RB/WR/TE only, so a QB injury cannot be papered over with a RB.
STARTERS = {
    "QB": 2,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "FLEX": 1,
    "D/ST": 1,
    "K": 1,
}
FLEX_ELIGIBLE = ("RB", "WR", "TE")

# A realistic full bench, not just the starting lineup -- the number a team
# should actually target rostering by the end of the draft. QB=3 is the
# league's most important roster rule and the one STARTERS alone can't
# express: two starters plus a bye/injury hedge, since the in-season QB
# waiver wire is empty (see league-analysis.md). The rest is bench depth
# beyond the starting requirement.
ROSTER_TARGETS = {
    "QB": 3,
    "RB": 4,
    "WR": 5,
    "TE": 2,
    "D/ST": 1,
    "K": 1,
}

# Per-team positional caps enforced by ESPN.
POSITION_MAX = {
    "QB": 4,
    "RB": 8,
    "WR": 8,
    "TE": 3,
    "D/ST": 3,
    "K": 3,
}

ROSTER_SIZE = 16          # 10 starters + 6 bench
BENCH_SLOTS = 6
IR_SLOTS = 1              # does not count against the active roster

TOTAL_ROSTER_SPOTS = NUM_TEAMS * ROSTER_SIZE          # 160
TOTAL_LEAGUE_BUDGET = NUM_TEAMS * SALARY_CAP          # $2000

# Every drafted player costs at least $1, so only the remainder is actually
# available to bid up talent. This is the pool that VORP gets priced against.
MIN_BID = 1
BIDDABLE_SURPLUS = TOTAL_LEAGUE_BUDGET - (TOTAL_ROSTER_SPOTS * MIN_BID)  # $1840


@dataclass(frozen=True)
class ReplacementLevel:
    """Positional rank treated as freely available (i.e. worth ~$1).

    Value above this baseline is what teams actually bid on. The QB number is
    the whole story of this league: 10 teams x 2 starters = 20 QBs started every
    week, and rosters carry a third for byes, so roughly 30 of the ~32 startable
    NFL quarterbacks are gone. Replacement QB is therefore a genuine backup,
    not the solid QB12-ish starter a 1QB league falls back on.
    """

    QB: int = 30      # 10 teams x ~3 rostered
    RB: int = 40      # 20 starters + ~4 flex + bench depth
    WR: int = 45      # 20 starters + ~5 flex + bench depth
    TE: int = 12      # 10 starters + ~1 flex + ~1 bench
    K: int = 10       # exactly one per team, no reason to roster two
    DST: int = 10

    def rank_for(self, position: str) -> int:
        key = "DST" if position in ("D/ST", "DST") else position
        return getattr(self, key)


REPLACEMENT = ReplacementLevel()

# Weekly league-wide demand, used for scarcity reporting.
WEEKLY_STARTER_DEMAND = {
    "QB": STARTERS["QB"] * NUM_TEAMS,     # 20
    "RB": STARTERS["RB"] * NUM_TEAMS,     # 20
    "WR": STARTERS["WR"] * NUM_TEAMS,     # 20
    "TE": STARTERS["TE"] * NUM_TEAMS,     # 10
    "D/ST": NUM_TEAMS,
    "K": NUM_TEAMS,
}

# Season shape
REGULAR_SEASON_WEEKS = 14
PLAYOFF_TEAMS = 4
CHAMPIONSHIP_WEEKS = 2      # championship round spans two weeks
TRADE_DEADLINE = "2026-12-04T00:00:00-08:00"

DIVISIONS = {
    "East": [
        "Love The Drake",
        "Nabers think im selling dope",
        "Team 5",
        "Team 7",
        "Team 11",
    ],
    "West": [
        "Aubrey's Revenge",
        "Team 4",
        "The QB's Knees",
        "Rhodric's Rowdy Team",
        "Team 10",
    ],
}

# ESPN team id -> console label. IDs come from the league's mTeam view and skip
# 9 (the ten teams are 1-8, 10, 11). Labels are hand-picked because ESPN's own
# abbreviations include emoji for three teams and aren't usable as text labels.
MY_TEAM_ID = 6

TEAMS = {
    1: "DRAKE",   # Love The Drake
    2: "AUBREY",  # Aubrey's Revenge
    3: "LEWE",    # Nabers think im selling dope
    4: "CCT",     # Team 4
    5: "FWD",     # Team 5
    6: "ME",      # The QB's Knees
    7: "HH",      # Team 7
    8: "RRT",     # Rhodric's Rowdy Team
    10: "PITTS",  # Team 10
    11: "SLAY",   # Team 11
}


@dataclass
class EspnCredentials:
    league_id: str = field(default_factory=lambda: os.getenv("ESPN_LEAGUE_ID", ""))
    swid: str = field(default_factory=lambda: os.getenv("ESPN_SWID", ""))
    espn_s2: str = field(default_factory=lambda: os.getenv("ESPN_S2", ""))
    team_id: str = field(default_factory=lambda: os.getenv("ESPN_TEAM_ID", ""))

    @property
    def is_configured(self) -> bool:
        return bool(self.league_id)

    @property
    def has_private_auth(self) -> bool:
        return bool(self.swid and self.espn_s2)


def max_bid(budget_remaining: int, roster_spots_open: int) -> int:
    """Most you can bid while still affording $1 for every other open slot."""
    return max(0, budget_remaining - (roster_spots_open - 1) * MIN_BID)
