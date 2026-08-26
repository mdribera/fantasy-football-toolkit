#!/usr/bin/env python3
"""Validate the valuation pipeline without needing ESPN credentials.

Builds a synthetic projection curve shaped like a real NFL season, prices it
under this league's 2QB rules, then re-prices the identical player pool under
1QB rules. The difference between the two is the entire thesis of this project,
so it is worth being able to demonstrate on demand.

The projections here are synthetic and exist only to exercise the math. Real
values come from scripts/build_values.py.
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table

from ff import config, scoring, values

console = Console()

# (position, count, top-end points, decay) -- shaped to resemble real positional
# scoring curves in full PPR.
CURVES = [
    ("QB", 40, 400, 0.985),
    ("RB", 70, 330, 0.975),
    ("WR", 90, 320, 0.982),
    ("TE", 30, 260, 0.955),
    ("K", 20, 150, 0.990),
    ("D/ST", 20, 140, 0.975),
]


def synthetic_pool() -> list[dict]:
    pool = []
    for position, count, top, decay in CURVES:
        for i in range(count):
            pool.append(
                {
                    "name": f"{position}{i + 1}",
                    "position": position,
                    "pro_team": "SYN",
                    "league_points": round(top * (decay ** i), 1),
                }
            )
    return pool


def price_with_replacement(pool, qb_replacement: int):
    original = config.REPLACEMENT.QB
    object.__setattr__(config.REPLACEMENT, "QB", qb_replacement)
    try:
        return values.compute_values(pool)
    finally:
        object.__setattr__(config.REPLACEMENT, "QB", original)


def top_n(vals, position, n=5):
    return [v for v in vals if v.position == position][:n]


def main() -> int:
    console.print("[bold]Scoring engine[/bold]")
    qb = scoring.score_offense(
        {"passing_yards": 4500, "passing_tds": 35, "interceptions": 10,
         "rushing_yards": 300, "rushing_tds": 3}
    )
    wr = scoring.score_offense(
        {"receptions": 90, "receiving_yards": 1200, "receiving_tds": 8}
    )
    dst = scoring.score_dst(
        {"sacks": 3, "interceptions": 1, "points_allowed": 17, "yards_allowed": 280}
    )
    console.print(f"  4500/35/10 QB + 300 rush yds : {qb:.1f} pts")
    console.print(f"  90 rec / 1200 yds / 8 TD WR  : {wr:.1f} pts")
    console.print(f"  3 sack, 1 INT, 17 PA, 280 YA : {dst:.1f} pts (one game)\n")

    pool = synthetic_pool()

    two_qb = price_with_replacement(pool, config.REPLACEMENT.QB)   # 30
    one_qb = price_with_replacement(pool, 14)                      # 10 tm x ~1.4

    console.print(f"[bold]Same player pool, priced two ways[/bold] "
                  f"(${config.TOTAL_LEAGUE_BUDGET} league-wide, "
                  f"${config.BIDDABLE_SURPLUS} biddable)\n")

    table = Table()
    table.add_column("Player")
    table.add_column("Pos", justify="center")
    table.add_column("Proj", justify="right")
    table.add_column("2QB value", justify="right", style="bold green")
    table.add_column("1QB value", justify="right", style="dim")
    table.add_column("Delta", justify="right")

    one_lookup = {v.name: v.value for v in one_qb}
    shown = (top_n(two_qb, "QB", 4) + top_n(two_qb, "RB", 3)
             + top_n(two_qb, "WR", 3) + top_n(two_qb, "TE", 2))
    for v in shown:
        other = one_lookup.get(v.name, 0)
        delta = v.value - other
        style = "green" if delta > 0 else "red" if delta < 0 else "dim"
        table.add_row(v.name, v.position, f"{v.projected_points:.0f}",
                      f"${v.value}", f"${other}", f"[{style}]{delta:+d}[/{style}]")
    console.print(table)

    def spend(vals, position):
        drafted = sorted([v for v in vals if v.position == position],
                         key=lambda v: v.value, reverse=True)
        return sum(v.value for v in drafted[:config.NUM_TEAMS * 3]) if position == "QB" \
            else sum(v.value for v in drafted[:config.NUM_TEAMS * 4])

    console.print("\n[bold]League-wide spend by position[/bold]")
    share = Table()
    share.add_column("Pos")
    share.add_column("2QB", justify="right")
    share.add_column("1QB", justify="right")
    for position in ("QB", "RB", "WR", "TE"):
        share.add_row(position, f"${spend(two_qb, position)}", f"${spend(one_qb, position)}")
    console.print(share)

    # The claim being tested is about budget conservation, not any one price:
    # the league spends the same $2,000 either way, so money that flows into
    # quarterbacks has to flow out of the skill positions. Absolute prices here
    # depend on the synthetic decay constants above and mean nothing; the
    # direction and rough magnitude of the shift is the real result.
    qb_shift = spend(two_qb, "QB") / max(spend(one_qb, "QB"), 1)
    skill_shift = (
        (spend(two_qb, "RB") + spend(two_qb, "WR"))
        / max(spend(one_qb, "RB") + spend(one_qb, "WR"), 1)
    )
    console.print(f"\nQB spend multiple, 2QB vs 1QB : [bold]{qb_shift:.2f}x[/bold]")
    console.print(f"RB+WR spend multiple           : [bold]{skill_shift:.2f}x[/bold]")

    assert qb_shift > 2.0, "2QB format must pull far more budget into QB"
    assert skill_shift < 0.95, "that budget must come out of RB/WR"
    console.print("[green]Budget conservation confirmed: "
                  "QB dollars come directly out of the RB/WR market.[/green]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
