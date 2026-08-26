#!/usr/bin/env python3
"""Pull projections from ESPN and build the auction value board.

Writes data/values.json and prints a tiered board. Re-run any time before the
draft; ESPN updates projections through the preseason.
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table

from ff import config, espn, values

console = Console()
OUT = Path(__file__).resolve().parents[1] / "data" / "values.json"


def load_pool(use_cache: bool) -> list[dict]:
    if use_cache:
        cached = espn.cache_read("player_pool")
        if cached:
            console.print(f"Using cached pool ({len(cached)} players).")
            return cached

    league = espn.connect()
    console.print("Pulling player pool from ESPN...")
    pool = [asdict(p) for p in espn.full_player_pool(league, fa_size=600)]
    espn.cache_write("player_pool", pool)
    console.print(f"Pulled {len(pool)} players.")
    return pool


def render(vals: list[values.Valuation], position: str | None, limit: int) -> None:
    rows = [v for v in vals if position is None or v.position == position]
    rows = rows[:limit]

    table = Table(title=f"Auction values -- {position or 'all positions'} "
                        f"({config.NUM_TEAMS}tm, ${config.SALARY_CAP}, 2QB, PPR)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Player")
    table.add_column("Pos", justify="center")
    table.add_column("Tm", justify="center", style="dim")
    table.add_column("Tier", justify="center")
    table.add_column("Proj", justify="right")
    table.add_column("VORP", justify="right")
    table.add_column("Value", justify="right", style="bold green")

    for i, v in enumerate(rows, 1):
        table.add_row(
            str(i), v.name, v.position, v.pro_team, str(v.tier),
            f"{v.projected_points:.0f}", f"{v.vorp:.0f}", f"${v.value}",
        )
    console.print(table)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--position", "-p", help="filter to one position")
    parser.add_argument("--limit", "-n", type=int, default=40)
    parser.add_argument("--cache", action="store_true", help="reuse the cached pull")
    parser.add_argument("--plan", action="store_true", help="show a $200 budget plan")
    args = parser.parse_args()

    pool = load_pool(use_cache=args.cache)
    vals = values.compute_values(pool)

    OUT.write_text(json.dumps([asdict(v) for v in vals], indent=2))
    console.print(f"Wrote {len(vals)} valuations to {OUT}\n")

    render(vals, args.position, args.limit)

    if args.plan:
        plan = values.budget_plan(vals)
        table = Table(title="Market-rate budget plan")
        table.add_column("Pos")
        table.add_column("Slots", justify="right")
        table.add_column("Going rate each")
        table.add_column("Subtotal", justify="right")
        for pos in ("QB", "RB", "WR", "TE", "K", "D/ST"):
            row = plan[pos]
            table.add_row(
                pos, str(row["slots"]),
                ", ".join(f"${x}" for x in row["market_rate_each"]),
                f"${row['subtotal']}",
            )
        table.add_row("[bold]Total", "", "", f"[bold]${plan['_total']} of ${plan['_cap']}")
        console.print(table)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
