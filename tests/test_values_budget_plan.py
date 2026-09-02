"""Unit tests for values.budget_plan() (T67).

Was previously untested even though it generates the budget table in
docs/auction-strategy.md; a live console panel (RosterPanel's plan-dollars
row) now depends on its exact shape, including what it returns for a
position with no priced pool at all -- QB/RB/WR/TE only in this fixture, so
K and D/ST exercise that path.
"""

from __future__ import annotations

from ff import config, values


def _player(name: str, position: str, value: int) -> values.Valuation:
    return values.Valuation(
        name=name, position=position, pro_team="", projected_points=0.0,
        replacement_points=0.0, vorp=0.0, value=value,
    )


def _pool() -> list[values.Valuation]:
    # 30 players per position, distinct descending values -- enough to give
    # every slot (ROSTER_TARGETS tops out at 5, at NUM_TEAMS=10) a real,
    # non-clamped market_rate_each entry. K and D/ST are left out entirely.
    pool = []
    for pos in ("QB", "RB", "WR", "TE"):
        pool.extend(_player(f"{pos}{i}", pos, 100 - i) for i in range(30))
    return pool


def test_budget_plan_keys_match_roster_targets_plus_totals():
    plan = values.budget_plan(_pool())
    assert set(plan.keys()) == set(config.ROSTER_TARGETS) | {"_total", "_cap"}


def test_budget_plan_priced_position_has_one_rate_per_slot():
    plan = values.budget_plan(_pool())
    for pos, slots in config.ROSTER_TARGETS.items():
        if pos in ("K", "D/ST"):
            continue
        entry = plan[pos]
        assert entry["slots"] == slots
        assert len(entry["market_rate_each"]) == slots
        assert entry["subtotal"] == sum(entry["market_rate_each"])


def test_budget_plan_position_with_no_pool_is_empty_not_missing():
    plan = values.budget_plan(_pool())
    for pos in ("K", "D/ST"):
        assert plan[pos]["market_rate_each"] == []
        assert plan[pos]["subtotal"] == 0


def test_budget_plan_total_and_cap():
    plan = values.budget_plan(_pool())
    assert plan["_total"] == sum(plan[pos]["subtotal"] for pos in config.ROSTER_TARGETS)
    assert plan["_cap"] == config.SALARY_CAP
