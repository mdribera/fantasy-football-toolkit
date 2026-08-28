#!/usr/bin/env python3
"""T7: sensitivity of the auction board to config.REPLACEMENT's QB baseline.

`config.REPLACEMENT.QB = 30` presumes all 10 teams roster three
quarterbacks. If the league carries only two, replacement moves to roughly
QB22, and ESPN's own QB board falls off a cliff around QB26 -- see
docs/league-analysis.md. This reprices the cached player pool at QB 22, 26,
and 30 (config.REPLACEMENT itself is left untouched) and reports the swing:
the top-15 QB board at each baseline, QB's share of the league's $2,000, Josh
Allen's price and his gap to the rest of the QB board, and the knock-on move
at RB and WR that comes from the same $1,840 surplus being redistributed.

Analysis only -- writes nothing under data/, and reads the cached player pool
(data/cache/player_pool.json) rather than pulling from ESPN. Run
`build_values.py --cache` or `--plan` at least once first if that cache is
missing.
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table

from ff import config, espn, values

console = Console()
QB_BASELINES = (22, 26, 30)


def load_pool() -> list[dict]:
    pool = espn.cache_read("player_pool")
    if not pool:
        console.print(
            "[red]No cached player pool at data/cache/player_pool.json.[/red] "
            "Run scripts/build_values.py once first (with or without --cache) "
            "to populate it.")
        raise SystemExit(1)
    return pool


def reprice(pool: list[dict], qb_baseline: int) -> list[values.Valuation]:
    replacement = replace(config.REPLACEMENT, QB=qb_baseline)
    return values.compute_values(pool, replacement=replacement)


def top_by_position(priced: list[values.Valuation], position: str, limit: int
                    ) -> list[values.Valuation]:
    return sorted((v for v in priced if v.position == position),
                  key=lambda v: v.value, reverse=True)[:limit]


def render_board_by_baseline(title: str, rows_by_baseline: dict[int, list[values.Valuation]]
                             ) -> Table:
    """One row per player named on the highest (current, QB30) baseline's
    board, with that player's price at every baseline side by side --
    the fastest way to see who moves and by how much."""
    table = Table(title=title)
    table.add_column("Player")
    for qb in QB_BASELINES:
        table.add_column(f"QB{qb}", justify="right")

    anchor = rows_by_baseline[QB_BASELINES[-1]]
    for i, anchor_v in enumerate(anchor):
        row = [f"{i + 1}. {anchor_v.name}"]
        for qb in QB_BASELINES:
            match = next((v for v in rows_by_baseline[qb] if v.name == anchor_v.name), None)
            row.append(f"${match.value}" if match else "-")
        table.add_row(*row)
    return table


def main() -> int:
    pool = load_pool()
    priced = {qb: reprice(pool, qb) for qb in QB_BASELINES}

    console.print(render_board_by_baseline(
        "Top-15 QB price by replacement baseline",
        {qb: top_by_position(priced[qb], "QB", 15) for qb in QB_BASELINES}))

    summary = Table(title="QB share of the league budget, and Josh Allen's price")
    summary.add_column("Baseline")
    summary.add_column("Top-30 QB $", justify="right")
    summary.add_column("Share of $2,000", justify="right")
    summary.add_column("Josh Allen", justify="right")
    summary.add_column("Best of the rest", justify="right")
    summary.add_column("Allen's gap", justify="right")

    for qb in QB_BASELINES:
        qb_vals = top_by_position(priced[qb], "QB", 30)  # 10 teams x 3 rostered
        top30_dollars = sum(v.value for v in qb_vals)
        allen = next((v for v in qb_vals if v.name == "Josh Allen"), None)
        rest = next((v for v in qb_vals if v.name != "Josh Allen"), None)
        gap = f"+{allen.value - rest.value}" if allen and rest else "-"
        summary.add_row(
            f"QB{qb}",
            f"${top30_dollars}",
            f"{top30_dollars / config.TOTAL_LEAGUE_BUDGET:.1%}",
            f"${allen.value}" if allen else "-",
            f"${rest.value} ({rest.name})" if rest else "-",
            gap,
        )
    console.print(summary)

    for pos in ("RB", "WR"):
        console.print(render_board_by_baseline(
            f"Top-5 {pos} price by replacement baseline (the knock-on move)",
            {qb: top_by_position(priced[qb], pos, 5) for qb in QB_BASELINES}))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
