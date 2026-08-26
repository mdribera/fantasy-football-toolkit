#!/usr/bin/env python3
"""Live auction console. Run this in a terminal beside the ESPN draft room.

Commands (type at the > prompt):
  <player> <price> <team>   record a purchase        e.g. "Josh Allen 62 ME"
  me                        my roster, budget, max bid, unfilled slots
  best [POS] [n]            best remaining by value
  need                      best remaining at positions I still must fill
  teams                     every team's budget and roster count
  market                    inflation: is the room paying over or under sheet
  undo                      remove the last recorded purchase
  quit

State persists to data/draft-state.json, so a crashed terminal loses nothing.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table

from ff import config, draft_state, values

console = Console()
VALUES_PATH = Path(__file__).resolve().parents[1] / "data" / "values.json"


def load_values() -> list[values.Valuation]:
    if not VALUES_PATH.exists():
        console.print("[red]No data/values.json.[/red] Run scripts/build_values.py first.")
        raise SystemExit(1)
    return [values.Valuation(**row) for row in json.loads(VALUES_PATH.read_text())]


def show_me(state: draft_state.DraftState, vals: list[values.Valuation]) -> None:
    me = state.my_team
    table = Table(title=f"{me} -- ${state.budget_left(me)} left, "
                        f"{state.spots_left(me)} spots, max bid ${state.max_bid(me)}")
    table.add_column("Player")
    table.add_column("Pos", justify="center")
    table.add_column("Paid", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("Edge", justify="right")

    lookup = {v.name: v.value for v in vals}
    for p in state.purchases:
        if p.team != me:
            continue
        value = lookup.get(p.player)
        edge = f"{value - p.price:+d}" if value is not None else "-"
        style = "green" if value and value > p.price else "red" if value else "dim"
        table.add_row(p.player, p.position, f"${p.price}",
                      f"${value}" if value else "-", f"[{style}]{edge}[/{style}]")
    console.print(table)

    needs = state.needs(me)
    unfilled = {k: v for k, v in needs.items() if v > 0}
    if unfilled:
        console.print("Still need: " + ", ".join(f"{k} x{v}" for k, v in unfilled.items()))
    else:
        console.print("[green]All starting slots filled.[/green]")


def show_best(state, vals, position=None, limit=15) -> None:
    taken = state.taken()
    pool = [v for v in vals if v.name not in taken]
    if position:
        pool = [v for v in pool if v.position == position.upper()]
    pool = sorted(pool, key=lambda v: v.value, reverse=True)[:limit]

    inflation = state.inflation(vals)
    table = Table(title=f"Best available{' -- ' + position.upper() if position else ''} "
                        f"(market x{inflation:.2f})")
    table.add_column("Player")
    table.add_column("Pos", justify="center")
    table.add_column("Tier", justify="center")
    table.add_column("Proj", justify="right")
    table.add_column("Sheet", justify="right")
    table.add_column("Adjusted", justify="right", style="bold")

    for v in pool:
        adjusted = max(1, round(v.value * inflation))
        table.add_row(v.name, v.position, str(v.tier),
                      f"{v.projected_points:.0f}", f"${v.value}", f"${adjusted}")
    console.print(table)


def show_teams(state: draft_state.DraftState) -> None:
    table = Table(title="League budgets")
    table.add_column("Team")
    table.add_column("Spent", justify="right")
    table.add_column("Left", justify="right")
    table.add_column("Spots", justify="right")
    table.add_column("Max bid", justify="right")
    for team in state.all_teams():
        style = "bold" if team == state.my_team else ""
        table.add_row(f"[{style}]{team}[/{style}]" if style else team,
                      f"${state.spent_by(team)}", f"${state.budget_left(team)}",
                      str(state.spots_left(team)), f"${state.max_bid(team)}")
    console.print(table)


def main() -> int:
    vals = load_values()
    state = draft_state.DraftState.load()
    lookup = {v.name.lower(): v for v in vals}

    console.print(f"[bold]Auction console[/bold] -- {config.LEAGUE_NAME}, "
                  f"${config.SALARY_CAP} cap, {config.ROSTER_SIZE} spots. "
                  f"{len(state.purchases)} purchases loaded. Type 'quit' to exit.\n")

    while True:
        try:
            line = console.input("[bold cyan]>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        cmd = line.split()
        head = cmd[0].lower()

        if head in ("quit", "exit", "q"):
            break
        if head == "me":
            show_me(state, vals)
        elif head == "teams":
            show_teams(state)
        elif head == "market":
            table = Table(title=f"Market vs sheet -- overall x{state.inflation(vals):.2f}")
            table.add_column("Pos")
            table.add_column("Paying", justify="right")
            table.add_column("Read")
            for pos, rate in sorted(state.inflation_by_position(vals).items(),
                                    key=lambda kv: kv[1], reverse=True):
                if rate > 1.1:
                    read = "[red]over sheet -- let these go[/red]"
                elif rate < 0.9:
                    read = "[green]under sheet -- buy here[/green]"
                else:
                    read = "[dim]at sheet[/dim]"
                table.add_row(pos, f"x{rate:.2f}", read)
            console.print(table)
            console.print(f"Other teams still hold [bold]"
                          f"${state.dollars_remaining_in_room()}[/bold] combined.")
        elif head == "undo":
            removed = state.undo()
            console.print(f"Removed: {removed}" if removed else "Nothing to undo.")
        elif head == "need":
            for pos, count in state.needs(state.my_team).items():
                if count > 0:
                    show_best(state, vals, pos, limit=6)
        elif head == "best":
            pos = cmd[1] if len(cmd) > 1 and not cmd[1].isdigit() else None
            n = next((int(c) for c in cmd[1:] if c.isdigit()), 15)
            show_best(state, vals, pos, n)
        elif len(cmd) >= 3 and cmd[-2].lstrip("$").isdigit():
            team = cmd[-1]
            price = int(cmd[-2].lstrip("$"))
            name = " ".join(cmd[:-2])
            match = lookup.get(name.lower())
            if not match:
                candidates = [v for k, v in lookup.items() if name.lower() in k]
                if len(candidates) == 1:
                    match = candidates[0]
                elif candidates:
                    console.print("Ambiguous: " + ", ".join(c.name for c in candidates[:8]))
                    continue
            position = match.position if match else "?"
            canonical = draft_state.normalize_team(team)
            if canonical not in state.all_teams() and len(state.purchases) > 3:
                console.print(f"[yellow]New team label '{canonical}'.[/yellow] "
                              "Typo? 'undo' reverses it.")
            state.record(match.name if match else name, position, price, team)
            note = ""
            if match:
                delta = match.value - price
                note = f" (sheet ${match.value}, {delta:+d})"
            console.print(f"Recorded: {name} ${price} -> {team}{note}")
        else:
            console.print("[yellow]Unrecognized.[/yellow] "
                          "Use: '<player> <price> <team>' or me/best/need/teams/market/undo/quit")

    state.save()
    console.print("Saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
