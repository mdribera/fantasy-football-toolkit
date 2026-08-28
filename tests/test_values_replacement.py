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
    QB30 baseline's 30th-best -- raising the floor for everyone above it
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


def test_replacement_rank_guard_passes_with_current_config():
    """The guard allows the current config, which has rank sum 137 <= 160."""
    pool = [
        _player(f"QB{i}", "QB", 300 - i * 5) for i in range(40)
    ] + [
        _player(f"RB{i}", "RB", 300 - i * 5) for i in range(50)
    ] + [
        _player(f"WR{i}", "WR", 300 - i * 5) for i in range(50)
    ] + [
        _player(f"TE{i}", "TE", 200 - i * 5) for i in range(20)
    ] + [
        _player(f"K{i}", "K", 100 - i * 5) for i in range(15)
    ] + [
        _player(f"D{i}", "D/ST", 100 - i * 5) for i in range(15)
    ]

    # Should not raise with the current config
    values.compute_values(pool, replacement=config.REPLACEMENT)


def test_replacement_rank_guard_raises_on_over_cap_config():
    """The guard raises ValueError when rank sum exceeds TOTAL_ROSTER_SPOTS."""
    pool = [
        _player(f"QB{i}", "QB", 300 - i * 5) for i in range(40)
    ] + [
        _player(f"RB{i}", "RB", 300 - i * 5) for i in range(50)
    ] + [
        _player(f"WR{i}", "WR", 300 - i * 5) for i in range(50)
    ] + [
        _player(f"TE{i}", "TE", 200 - i * 5) for i in range(20)
    ] + [
        _player(f"K{i}", "K", 100 - i * 5) for i in range(15)
    ] + [
        _player(f"D{i}", "D/ST", 100 - i * 5) for i in range(15)
    ]

    # Create a config with QB rank pushed high enough to exceed the cap.
    # Current sum is 137, cap is 160, so we need rank sum > 160.
    # If we set QB=50 (instead of 26), the new sum would be:
    # 50 - 1 + 39 + 44 + 11 + 9 + 9 = 161, which exceeds 160.
    over_cap = replace(config.REPLACEMENT, QB=50)

    try:
        values.compute_values(pool, replacement=over_cap)
        assert False, "Expected ValueError to be raised"
    except ValueError as e:
        assert "Replacement rank sum" in str(e)
        assert "exceeds TOTAL_ROSTER_SPOTS" in str(e)
        assert "161" in str(e)
        assert "160" in str(e)
