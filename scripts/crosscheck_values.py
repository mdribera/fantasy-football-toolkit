#!/usr/bin/env python3
"""Cross-check data/values.json against outside projection sources.

Reprices three independent projection sets under this league's exact scoring
and the same value model that built the sheet, then lines them up against
ESPN's numbers player by player:

  - Rotowire, via Sleeper's public projections endpoint
  - CBS Sports season projections
  - FFToday season projections

plus FantasyFootballCalculator's 10-team 2QB ADP as a market-order read.
Because every source is scored with scoring.py and priced with
values.compute_values, a dollar gap between a source and the sheet is a
projection disagreement, not a format artifact.

Reads data/values.json (run build_values.py first) and writes a snapshot of
the merged comparison to data/cache/crosscheck_<date>.json. Never touches
values.json itself: the sheet stays ESPN-derived, and this is the audit.

    .venv/bin/python scripts/crosscheck_values.py             # outliers + summaries
    .venv/bin/python scripts/crosscheck_values.py --refresh   # refetch every source
    .venv/bin/python scripts/crosscheck_values.py -p QB -n 30 # one position, top 30
    .venv/bin/python scripts/crosscheck_values.py --player "kyler murray"
"""

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests
from rich.console import Console
from rich.table import Table

from ff import config, projections, scoring, sleeper, values
from ff.projections import normalize_name as norm

console = Console()
ROOT = Path(__file__).resolve().parents[1]
SHEET = ROOT / "data" / "values.json"
SOURCES = ("rotowire", "cbs", "fftoday")


def load_sheet() -> list[dict]:
    if not SHEET.exists():
        console.print("[red]data/values.json is missing.[/red] Run scripts/build_values.py first.")
        raise SystemExit(1)
    return json.loads(SHEET.read_text())


def source_pools(season: int, sheet: list[dict], refresh: bool) -> dict[str, list[dict]]:
    """Each outside source as a pool ready for values.compute_values.

    Positions a source does not cover (K, D/ST) are filled from the sheet's
    own ESPN rows so the surplus split sees the same roster shape ESPN did.
    """
    def rotowire() -> list[dict]:
        return [
            {"name": r["name"], "position": r["position"], "pro_team": r["pro_team"] or "",
             "league_points": scoring.score_offense(r["stats"])}
            for r in sleeper.season_projections(season, force_refresh=refresh)
            if r["name"] and r["position"]
        ]

    fetchers = {
        "rotowire": rotowire,
        "cbs": lambda: projections.cbs_projections(season, force_refresh=refresh),
        "fftoday": lambda: projections.fftoday_projections(season, force_refresh=refresh),
    }
    pools: dict[str, list[dict]] = {}
    for label, fetch in fetchers.items():
        try:
            pools[label] = fetch()
        except requests.RequestException as exc:
            # One source being down the morning of the draft should cost
            # that column, not the whole audit.
            console.print(f"[yellow]{label}: unavailable, skipping ({exc})[/yellow]")
    if not pools:
        console.print("[red]No outside source could be fetched.[/red]")
        raise SystemExit(1)

    for label, pool in pools.items():
        covered = {r["position"] for r in pool}
        pool.extend(
            {"name": v["name"], "position": v["position"], "pro_team": v["pro_team"],
             "league_points": v["projected_points"]}
            for v in sheet if v["position"] not in covered
        )
        console.print(f"{label}: {len(pool)} rows (ESPN fills {sorted(set(config.ROSTER_TARGETS) - covered)})")
    return pools


def reprice(pool: list[dict]) -> dict[tuple[str, str], values.Valuation]:
    return {(norm(v.name), v.position): v for v in values.compute_values(pool)}


def merge(sheet: list[dict], priced: dict[str, dict], adp: dict) -> list[dict]:
    """One row per sheet player with every source's projection and price."""
    adp_rank = {norm(p["name"]): (i + 1, p["adp"]) for i, p in enumerate(adp["players"])}
    ranked = sorted(sheet, key=lambda v: (-v["value"], -v["vorp"]))
    rows = []
    for rank, v in enumerate(ranked, 1):
        key = (norm(v["name"]), v["position"])
        row = {
            "rank": rank, "name": v["name"], "position": v["position"], "pro_team": v["pro_team"],
            "espn_points": round(v["projected_points"], 1), "espn_value": v["value"],
        }
        outside = []
        for label in SOURCES:
            hit = priced[label].get(key)
            row[f"{label}_points"] = hit and round(hit.projected_points, 1)
            row[f"{label}_value"] = hit and hit.value
            if hit is not None and hit.projected_points > 0:
                outside.append(hit.value)
        row["outside_mean"] = round(statistics.mean(outside), 1) if outside else None
        row["outside_n"] = len(outside)
        row["delta"] = round(row["outside_mean"] - v["value"], 1) if outside else None
        row["adp_2qb_rank"], row["adp_2qb"] = adp_rank.get(norm(v["name"]), (None, None))
        rows.append(row)
    return rows


def _money(x) -> str:
    return "-" if x is None else f"${x}"


def render_rows(rows: list[dict], title: str) -> None:
    table = Table(title=title)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Player")
    table.add_column("Pos", justify="center")
    table.add_column("ESPN pts", justify="right")
    table.add_column("ESPN", justify="right", style="bold")
    table.add_column("RW", justify="right")
    table.add_column("CBS", justify="right")
    table.add_column("FFT", justify="right")
    table.add_column("Mean", justify="right")
    table.add_column("Delta", justify="right")
    table.add_column("2QB ADP", justify="right", style="dim")
    for r in rows:
        delta = r["delta"]
        style = "" if delta is None else ("green" if delta > 0 else "red")
        table.add_row(
            str(r["rank"]), r["name"], r["position"], f"{r['espn_points']:.0f}",
            _money(r["espn_value"]), _money(r["rotowire_value"]), _money(r["cbs_value"]),
            _money(r["fftoday_value"]),
            "-" if r["outside_mean"] is None else f"${r['outside_mean']:.0f}",
            "-" if delta is None else f"[{style}]{delta:+.0f}[/{style}]",
            "-" if r["adp_2qb_rank"] is None else f"#{r['adp_2qb_rank']}",
        )
    console.print(table)


def render_summaries(sheet: list[dict], pools: dict[str, list[dict]],
                     priced: dict[str, dict]) -> None:
    def share(vals: list, pos: str) -> str:
        top = sorted(vals, key=lambda v: -v.value)[: config.TOTAL_ROSTER_SPOTS]
        total = sum(v.value for v in top) or 1
        return f"{100 * sum(v.value for v in top if v.position == pos) / total:.1f}%"

    def qb_band(vals: list) -> tuple[str, str]:
        qbs = sorted((v for v in vals if v.position == "QB"), key=lambda v: -v.projected_points)
        pts = [v.projected_points for v in qbs]
        return f"{pts[1] - pts[14]:.1f}", f"{pts[0] - pts[1]:.1f}"

    espn_vals = [values.Valuation(**v) for v in sheet]
    table = Table(title="Where each source puts the money (top-160 dollars)")
    table.add_column("Source")
    for pos in ("QB", "RB", "WR", "TE"):
        table.add_column(pos, justify="right")
    table.add_column("QB repl", justify="right")
    table.add_column("QB2-15 spread", justify="right")
    table.add_column("Allen - QB2", justify="right")
    for label, vals in [("ESPN", espn_vals)] + [(k, list(p.values())) for k, p in priced.items()]:
        pool = sheet if label == "ESPN" else pools[label]
        repl = (values.replacement_points(pool, "QB") if label != "ESPN"
                else next(v["replacement_points"] for v in sheet if v["position"] == "QB"))
        spread, premium = qb_band(vals)
        table.add_row(label, *(share(vals, p) for p in ("QB", "RB", "WR", "TE")),
                      f"{repl:.0f}", spread, premium)
    console.print(table)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="refetch every source")
    parser.add_argument("--position", "-p", help="show every sheet player at one position")
    parser.add_argument("--limit", "-n", type=int, default=40)
    parser.add_argument("--player", help="show one player (substring match)")
    parser.add_argument("--threshold", type=int, default=6,
                        help="outlier table: |outside mean - ESPN| in dollars")
    args = parser.parse_args()

    sheet = load_sheet()
    pools = source_pools(config.SEASON, sheet, args.refresh)
    priced = {label: reprice(pool) for label, pool in pools.items()}
    adp = projections.ffc_adp(config.SEASON, teams=config.NUM_TEAMS, force_refresh=args.refresh)
    meta = adp["meta"]
    console.print(f"FFC 2QB ADP: {meta.get('total_drafts')} drafts, "
                  f"{meta.get('start_date')} to {meta.get('end_date')}\n")

    rows = merge(sheet, priced, adp)
    out = ROOT / "data" / "cache" / f"crosscheck_{date.today().isoformat()}.json"
    out.write_text(json.dumps(rows, indent=1))

    if args.player:
        needle = args.player.lower()
        render_rows([r for r in rows if needle in r["name"].lower()], f"'{args.player}' across sources")
        return 0
    if args.position:
        pos = "D/ST" if args.position.upper() in ("DST", "D/ST") else args.position.upper()
        render_rows([r for r in rows if r["position"] == pos][: args.limit],
                    f"{pos} -- sheet order, every source")
        return 0

    render_summaries(sheet, pools, priced)
    outliers = [
        r for r in rows
        if r["delta"] is not None and r["outside_n"] >= 2
        and abs(r["delta"]) >= args.threshold
        and r["position"] not in ("K", "D/ST")
    ]
    outliers.sort(key=lambda r: r["delta"])
    render_rows(outliers[: args.limit],
                f"ESPN outliers: outside mean differs by ${args.threshold}+ "
                f"(2+ sources; K and D/ST excluded)")
    console.print(f"Wrote {len(rows)} merged rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
