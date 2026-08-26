#!/usr/bin/env python3
"""Stand-alone tool for the ESPN draft-detail feed: check it's alive, record a
fixture from a real draft, or replay a fixture through the import path.

    scripts/draft_sync.py --once             # is the live feed reachable right now
    scripts/draft_sync.py --record OUT.jsonl # poll a live draft, save every snapshot
    scripts/draft_sync.py --replay IN.jsonl  # feed a fixture through import, reconcile

`--replay` is the rehearsal step from HANDOFF.md: it exercises the exact same
DraftFeed / PlayerResolver / DraftState.record_pick path that scripts/auction.py
uses live, with no ESPN account or timing involved.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table

from ff import config, draft_state, draft_sync

console = Console()
VALUES_PATH = Path(__file__).resolve().parents[1] / "data" / "values.json"


def cmd_once(cred: config.EspnCredentials) -> int:
    try:
        detail = draft_sync.fetch_picks(cred)
    except draft_sync.DraftFeedError as exc:
        console.print(f"[red]Feed unreachable:[/red] {exc}")
        return 1
    picks = detail.get("picks", [])
    done = draft_sync.completed(picks)
    console.print(f"inProgress={detail.get('inProgress')} drafted={detail.get('drafted')} "
                  f"total_slots={len(picks)} completed={len(done)}")
    if done:
        resolver = draft_sync.PlayerResolver(VALUES_PATH)
        for p in draft_sync.to_resolved(done[-5:], resolver):
            console.print(f"  {p.player} ${p.price} -> {p.team}")
    return 0


def cmd_record(out_path: Path, interval: int, cred: config.EspnCredentials) -> int:
    console.print(f"Recording live draft snapshots (league {cred.league_id}) "
                  f"to {out_path} every {interval}s. Ctrl-C to stop.")
    with out_path.open("a") as fh:
        try:
            while True:
                try:
                    detail = draft_sync.fetch_picks(cred)
                    fh.write(json.dumps(detail) + "\n")
                    fh.flush()
                    done = len(draft_sync.completed(detail.get("picks", [])))
                    console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] {done} picks completed")
                except draft_sync.DraftFeedError as exc:
                    console.print(f"[yellow]poll failed: {exc}[/yellow]")
                time.sleep(interval)
        except KeyboardInterrupt:
            console.print("Stopped.")
    return 0


def _run_replay(in_path: Path, feed: draft_sync.DraftFeed, state: draft_state.DraftState) -> tuple[int, int]:
    imported = duplicates = 0
    for snapshot in draft_sync.replay_snapshots(in_path):
        for pick in feed.poll_snapshot(snapshot):
            recorded = state.record_pick(pick.player, pick.position, pick.price,
                                          pick.team, pick.espn_pick_id)
            imported += 1 if recorded else 0
            duplicates += 0 if recorded else 1
    return imported, duplicates


SCRATCH_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "cache" / "draft-state-replay.json"


def cmd_replay(in_path: Path) -> int:
    resolver = draft_sync.PlayerResolver(VALUES_PATH)
    # A dedicated scratch path, never the live data/draft-state.json -- a
    # rehearsal must never be able to clobber real draft-day state.
    state = draft_state.DraftState(my_team="ME", state_path=SCRATCH_STATE_PATH)
    feed = draft_sync.DraftFeed(source=lambda: {"picks": []}, resolver=resolver)

    imported, duplicates = _run_replay(in_path, feed, state)
    console.print(f"Imported {imported} picks, {duplicates} duplicates suppressed.")

    # Replaying the identical file again should add nothing: proves the
    # espn_pick_id dedup holds across a full pass, the same guarantee that
    # protects the live poller from double-recording a pick it sees twice.
    reimported, _ = _run_replay(in_path, feed, state)
    console.print(f"Second pass imported {reimported} new picks (0 expected).")

    table = Table(title="Reconciliation")
    table.add_column("Team")
    table.add_column("Spent", justify="right")
    table.add_column("Spots filled", justify="right")
    for team in state.all_teams():
        table.add_row(team, f"${state.spent_by(team)}",
                      str(config.ROSTER_SIZE - state.spots_left(team)))
    console.print(table)

    all_full = all(state.spots_left(t) == 0 for t in state.all_teams())
    all_capped = all(state.spent_by(t) <= config.SALARY_CAP for t in state.all_teams())
    console.print(f"All teams at {config.ROSTER_SIZE} spots: {all_full}")
    console.print(f"No team over the ${config.SALARY_CAP} cap: {all_capped}")
    return 0 if all_capped else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="poll the live feed once and report")
    group.add_argument("--record", metavar="PATH", help="append live snapshots to a JSONL fixture")
    group.add_argument("--replay", metavar="PATH", help="replay a JSONL fixture through import")
    parser.add_argument("--interval", type=int, default=10, help="seconds between polls for --record")
    parser.add_argument("--league-id", help="override ESPN_LEAGUE_ID (e.g. a practice draft)")
    parser.add_argument("--team-id", help="override ESPN_TEAM_ID")
    args = parser.parse_args()

    cred = config.EspnCredentials()
    if args.league_id:
        cred.league_id = args.league_id
    if args.team_id:
        cred.team_id = args.team_id

    if args.once:
        return cmd_once(cred)
    if args.record:
        return cmd_record(Path(args.record), args.interval, cred)
    return cmd_replay(Path(args.replay))


if __name__ == "__main__":
    raise SystemExit(main())
