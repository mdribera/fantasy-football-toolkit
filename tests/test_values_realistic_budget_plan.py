"""Unit tests for values.realistic_budget_plan().

RosterPanel's plan-dollars row uses this instead of the raw budget_plan():
K and D/ST are always $1 on draft day (docs/auction-strategy.md), not the
model's market rate, and the freed-up dollars go back into QB, WR and RB.
"""

from __future__ import annotations

from ff import config, values


def _player(name: str, position: str, value: int) -> values.Valuation:
    return values.Valuation(
        name=name, position=position, pro_team="", projected_points=0.0,
        replacement_points=0.0, vorp=0.0, value=value,
    )


def _pool() -> list[values.Valuation]:
    # QB/RB/WR/TE priced like test_values_budget_plan's fixture. K and D/ST
    # are seeded so their one-slot market rate lands at $3 and $6 -- the
    # exact figures docs/auction-strategy.md quotes -- so the $7 freed by
    # flattening them to $1 each is easy to check by hand.
    pool = []
    for pos in ("QB", "RB", "WR", "TE"):
        pool.extend(_player(f"{pos}{i}", pos, 100 - i) for i in range(30))
    pool.extend(_player(f"K{i}", "K", 8 - i) for i in range(30))
    pool.extend(_player(f"D/ST{i}", "D/ST", 11 - i) for i in range(30))
    return pool


def test_realistic_budget_plan_prices_k_and_dst_at_one_dollar():
    plan = values.realistic_budget_plan(_pool())
    assert plan["K"]["subtotal"] == config.MIN_BID
    assert plan["D/ST"]["subtotal"] == config.MIN_BID


def test_realistic_budget_plan_redistributes_freed_dollars_evenly():
    before = values.budget_plan(_pool())
    after = values.realistic_budget_plan(_pool())
    freed = (before["K"]["subtotal"] - config.MIN_BID) + (before["D/ST"]["subtotal"] - config.MIN_BID)
    assert freed == 7  # matches docs/auction-strategy.md's worked example

    gains = {pos: after[pos]["subtotal"] - before[pos]["subtotal"] for pos in ("QB", "WR", "RB")}
    assert sum(gains.values()) == freed
    assert max(gains.values()) - min(gains.values()) <= 1


def test_realistic_budget_plan_leaves_te_untouched():
    before = values.budget_plan(_pool())
    after = values.realistic_budget_plan(_pool())
    assert after["TE"]["subtotal"] == before["TE"]["subtotal"]


def test_realistic_budget_plan_total_matches_adjusted_subtotals():
    plan = values.realistic_budget_plan(_pool())
    assert plan["_total"] == sum(plan[pos]["subtotal"] for pos in config.ROSTER_TARGETS)
