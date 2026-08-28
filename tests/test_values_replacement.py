"""Unit test for the `replacement` override threaded through values.py (T7).

Only covers the override itself -- scripts/replacement_sensitivity.py is the
actual sensitivity analysis, run against the real cached player pool.
"""

from __future__ import annotations

from dataclasses import replace

from ff import config, values


def _player(name: str, position: str, points: float) -> dict:
    return {"name": name, "position": position, "pro_team": "",
            "projected_points": points}


def test_replacement_override_does_not_touch_the_live_config():
    pool = [_player(f"QB{i}", "QB", 300 - i * 5) for i in range(40)]
    values.compute_values(pool, positions=("QB",),
                          replacement=replace(config.REPLACEMENT, QB=10))
    assert config.REPLACEMENT.QB == 26   # untouched


def test_a_lower_qb_baseline_raises_qb_replacement_points():
    """Baseline QB10 sets replacement to the 10th-best QB's points, which is
    higher up the depth chart (and thus a higher point total) than the
    default baseline's 30th-best -- raising the floor for everyone above it
    and shrinking their VORP."""
    pool = [_player(f"QB{i}", "QB", 300 - i * 5) for i in range(40)]
    low_baseline = replace(config.REPLACEMENT, QB=10)
    high_baseline = replace(config.REPLACEMENT, QB=30)

    low_vals = {v.name: v for v in values.compute_values(pool, positions=("QB",),
                                                         replacement=low_baseline)}
    high_vals = {v.name: v for v in values.compute_values(pool, positions=("QB",),
                                                          replacement=high_baseline)}

    assert low_vals["QB0"].replacement_points > high_vals["QB0"].replacement_points
    assert low_vals["QB0"].vorp < high_vals["QB0"].vorp
