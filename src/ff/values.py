"""Auction value model for a 10-team, $200, 2QB, full-PPR league.

Downloaded auction value sheets are built for 1QB leagues and are actively
misleading here. The reason is budget conservation: the league spends exactly
$2,000 no matter what. In a 1QB league roughly 6% of that goes to quarterbacks;
in a 2QB league it is 25-30%. That extra ~$400 does not appear from nowhere --
it is drained out of the RB and WR markets. So QBs are underpriced by stock
sheets and RB/WR are overpriced, and both errors compound.

The model:

  1. Project season points for every player under this league's scoring.
  2. Set a replacement baseline per position from real roster demand.
  3. VORP = projected points - replacement points (floored at zero).
  4. Every one of the 160 roster spots costs at least $1, leaving $1,840 of
     surplus to bid with. Distribute that surplus in proportion to VORP.
  5. value = $1 + VORP * (surplus per VORP point).

The output is internally consistent by construction: summed across the players
who will actually be drafted, it totals the league's real $2,000.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from . import config


@dataclass
class Valuation:
    name: str
    position: str
    pro_team: str
    projected_points: float
    replacement_points: float
    vorp: float
    value: int
    tier: int = 0
    espn_id: int | None = None

    @property
    def value_str(self) -> str:
        return f"${self.value}"


def _positional_pool(players: Sequence, position: str) -> list:
    return sorted(
        [p for p in players if _pos(p) == position],
        key=lambda p: _points(p),
        reverse=True,
    )


def _pos(player) -> str:
    pos = player.get("position") if isinstance(player, dict) else player.position
    return "D/ST" if pos in ("DST", "D/ST", "TEAM") else pos


def _points(player) -> float:
    """Projected season points under league scoring.

    Prefers ESPN's league-context projection; `league_points` is only a
    fallback for sources where we scored a raw stat line ourselves.
    """
    if isinstance(player, dict):
        return float(player.get("projected_points") or player.get("league_points") or 0.0)
    return float(
        getattr(player, "projected_points", 0.0)
        or getattr(player, "league_points", 0.0)
        or 0.0
    )


def _name(player) -> str:
    return player["name"] if isinstance(player, dict) else player.name


def _team(player) -> str:
    if isinstance(player, dict):
        return player.get("pro_team", "")
    return getattr(player, "pro_team", "")


def _espn_id(player) -> int | None:
    if isinstance(player, dict):
        return player.get("espn_id")
    return getattr(player, "espn_id", None)


def replacement_points(players: Sequence, position: str) -> float:
    """Projected points of the player at this position's replacement rank."""
    pool = _positional_pool(players, position)
    rank = config.REPLACEMENT.rank_for(position)
    if not pool:
        return 0.0
    index = min(rank - 1, len(pool) - 1)
    return _points(pool[index])


def compute_values(
    players: Sequence,
    positions: Iterable[str] = ("QB", "RB", "WR", "TE", "K", "D/ST"),
    budget_surplus: int = config.BIDDABLE_SURPLUS,
) -> list[Valuation]:
    """Price the draftable pool. Returns valuations sorted by value desc."""
    positions = list(positions)

    baselines = {pos: replacement_points(players, pos) for pos in positions}

    raw: list[Valuation] = []
    for player in players:
        pos = _pos(player)
        if pos not in baselines:
            continue
        pts = _points(player)
        vorp = max(0.0, pts - baselines[pos])
        raw.append(
            Valuation(
                name=_name(player),
                position=pos,
                pro_team=_team(player),
                projected_points=pts,
                replacement_points=baselines[pos],
                vorp=vorp,
                value=config.MIN_BID,
                espn_id=_espn_id(player),
            )
        )

    # Only the players who will actually be drafted compete for the surplus.
    # Taking the top N by VORP, where N is the number of roster spots, keeps the
    # dollars-in equal to dollars-out.
    raw.sort(key=lambda v: v.vorp, reverse=True)
    draftable = raw[: config.TOTAL_ROSTER_SPOTS]
    total_vorp = sum(v.vorp for v in draftable)

    if total_vorp <= 0:
        return raw

    dollars_per_vorp = budget_surplus / total_vorp

    priced = []
    for v in raw:
        in_pool = v in draftable
        value = config.MIN_BID + (v.vorp * dollars_per_vorp if in_pool else 0.0)
        priced.append(replace(v, value=max(config.MIN_BID, round(value))))

    priced.sort(key=lambda v: (v.value, v.vorp), reverse=True)
    return assign_tiers(priced)


def assign_tiers(valuations: list[Valuation], gap_ratio: float = 0.12) -> list[Valuation]:
    """Break each position into tiers where projected points drop off.

    Tiers matter more than exact prices in an auction: they tell you when it is
    safe to lose a bidding war because an equivalent player is still available,
    and when it is not.
    """
    by_position: dict[str, list[Valuation]] = {}
    for v in valuations:
        by_position.setdefault(v.position, []).append(v)

    for pos, group in by_position.items():
        group.sort(key=lambda v: v.projected_points, reverse=True)
        if not group:
            continue
        tier = 1
        top = group[0].projected_points or 1.0
        for i, v in enumerate(group):
            if i > 0:
                drop = group[i - 1].projected_points - v.projected_points
                if drop / top > gap_ratio / 4:
                    tier += 1
            v.tier = tier
    return valuations


def budget_plan(valuations: list[Valuation]) -> dict:
    """What a single team's $200 should look like, given the priced market."""
    plan: dict[str, dict] = {}
    for pos, slots in (
        ("QB", 3),   # 2 starters + a bye/injury hedge; the wire will be empty
        ("RB", 4),
        ("WR", 5),
        ("TE", 2),
        ("K", 1),
        ("D/ST", 1),
    ):
        pool = sorted(
            [v for v in valuations if v.position == pos],
            key=lambda v: v.value,
            reverse=True,
        )
        # Market rate for the Nth-best player a team would realistically land.
        per_team_share = [
            pool[min(i * config.NUM_TEAMS + config.NUM_TEAMS // 2, len(pool) - 1)].value
            for i in range(slots)
            if pool
        ]
        plan[pos] = {
            "slots": slots,
            "market_rate_each": per_team_share,
            "subtotal": sum(per_team_share),
        }
    total = sum(p["subtotal"] for p in plan.values())
    plan["_total"] = total
    plan["_cap"] = config.SALARY_CAP
    return plan
