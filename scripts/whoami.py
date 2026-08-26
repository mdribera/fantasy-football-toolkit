#!/usr/bin/env python3
"""Verify the ESPN connection and print league/team identity.

Run this first after filling in .env. It confirms credentials work and tells
you your ESPN_TEAM_ID.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table

from ff import config, espn

console = Console()


def main() -> int:
    cred = config.EspnCredentials()
    if not cred.is_configured:
        console.print("[red]ESPN_LEAGUE_ID not set.[/red] Copy .env.example to .env first.")
        return 1

    auth = "private (cookies present)" if cred.has_private_auth else "public read-only"
    console.print(f"Connecting to league [bold]{cred.league_id}[/bold] "
                  f"season {config.SEASON} -- {auth}")

    try:
        league = espn.connect(cred)
    except Exception as exc:
        console.print(f"[red]Connection failed:[/red] {exc}")
        console.print("\nIf this is a private-league error, refresh your SWID and "
                      "espn_s2 cookies -- espn_s2 rotates periodically.")
        return 1

    console.print(f"\n[green]Connected:[/green] {league.settings.name}")

    table = Table(title="Teams")
    table.add_column("ID", justify="right")
    table.add_column("Team")
    table.add_column("Owner")
    table.add_column("Roster", justify="right")

    for team in league.teams:
        owners = getattr(team, "owners", None) or []
        owner = ""
        if owners and isinstance(owners[0], dict):
            owner = f"{owners[0].get('firstName','')} {owners[0].get('lastName','')}".strip()
        table.add_row(str(team.team_id), team.team_name, owner, str(len(team.roster)))

    console.print(table)
    console.print("\nSet [bold]ESPN_TEAM_ID[/bold] in .env to your row's ID.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
