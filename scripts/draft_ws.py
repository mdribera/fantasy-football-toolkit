#!/usr/bin/env python3
"""Raw capture tool for ESPN's live draft-room websocket.

`mDraftDetail` (see draft_sync.py) never updates during a live auction --
the draft room actually holds a websocket to a separate host,
`fantasydraft.espn.com`, captured from the browser's own Network tab on
2026-08-26. This script's only job is to connect read-only and log every
frame verbatim to a JSONL fixture, so a parser can be built against a
recording instead of a live draft:

    scripts/draft_ws.py --record OUT.jsonl --league-id N --team-id N

No parsing happens on `--record` on purpose -- it captures raw frames.
`--replay` feeds a recorded (or hand-captured) fixture through
`ff.draft_ws.parse_frame` and a scratch `DraftState`, the same
fixture-rehearsal pattern `draft_sync.py --replay` already uses.

    scripts/draft_ws.py --replay data/ws-live-test.jsonl

Session id: ESPN's own room tab used a token whose trailing field looks like
a per-connection session id, and the host appears to only tolerate one live
connection per token -- reusing an active session id disconnects the other
holder ("Duplicate Connection"). This script always mints its own random
session id rather than reusing one from `.env` or a captured URL, so running
it alongside someone's real draft-room tab does not kick them off. That
assumption still needs to be verified against a real practice draft before
this is trusted on Sep 2 -- see HANDOFF.md item 2.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table
import websocket

from ff import config, draft_state, draft_sync, draft_ws

console = Console()
VALUES_PATH = Path(__file__).resolve().parents[1] / "data" / "values.json"
SCRATCH_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "cache" / "draft-ws-state-replay.json"

# TOKEN <gameId>:<leagueId>:<teamId>:<swid>:<sessionId> -- SWID and sessionId are
# per-account auth material and must never land in a recording on disk.
_TOKEN_RE = re.compile(r"^(TOKEN \d+:\d+:\d+:)\{[0-9A-Fa-f-]+\}:(\d+)")


def redact_token(msg: str) -> str:
    return _TOKEN_RE.sub(lambda m: m.group(1) + "{REDACTED-SWID}:REDACTED-SESSION", msg)


GAME_ID = "1"
GAME_CODE = "KONA"  # ESPN's internal codename for this product; fixed in the captured URL


def _join_url(cred: config.EspnCredentials, session_id: int) -> str:
    token = f"{GAME_ID}:{cred.league_id}:{cred.team_id}:{cred.swid}:{session_id}"
    nocache = random.randint(100000, 999999)
    return (
        f"wss://fantasydraft.espn.com/game-{GAME_ID}/league-{cred.league_id}/JOIN"
        f"?1={GAME_ID}&2={cred.league_id}&3={cred.team_id}&4={cred.swid}&5={token}"
        f"&6=false&7=false&8={GAME_CODE}&nocache={nocache}"
    )


def cmd_record(out_path: Path, cred: config.EspnCredentials, duration: int | None) -> int:
    if not cred.has_private_auth:
        console.print("[red]ESPN_SWID / ESPN_S2 required.[/red]")
        return 1

    session_id = random.randint(100_000_000, 999_999_999)
    url = _join_url(cred, session_id)
    console.print(f"Connecting to league {cred.league_id} as team {cred.team_id}, "
                  f"session {session_id} (own -- not Mark's live session).")
    console.print(f"Logging every frame to {out_path}. Ctrl-C to stop.")

    fh = out_path.open("a")
    start = time.monotonic()

    def _log(msg: str) -> None:
        fh.write(json.dumps({"ts": time.time(), "msg": redact_token(msg)}) + "\n")
        fh.flush()

    def on_message(ws, message):
        _log(message)
        console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] recv {message[:120]}")

    def on_open(ws):
        console.print("[green]Connected.[/green]")

    def on_error(ws, error):
        console.print(f"[red]error:[/red] {error}")

    def on_close(ws, code, msg):
        console.print(f"[yellow]closed:[/yellow] {code} {msg}")

    ws = websocket.WebSocketApp(
        url,
        cookie=f"espn_s2={cred.espn_s2}; SWID={cred.swid}",
        header=[
            "Origin: https://fantasy.espn.com",
            f"Referer: https://fantasy.espn.com/football/draft?leagueId={cred.league_id}",
            "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        ],
        on_message=on_message,
        on_open=on_open,
        on_error=on_error,
        on_close=on_close,
    )

    try:
        if duration:
            import threading
            timer = threading.Timer(duration, ws.close)
            timer.start()
        ws.run_forever()
    except KeyboardInterrupt:
        console.print("Stopped.")
    finally:
        fh.close()
    console.print(f"Ran {time.monotonic() - start:.0f}s. Frames saved to {out_path}.")
    return 0


def _replay_pass(path: Path, resolver: draft_sync.PlayerResolver,
                  state: draft_state.DraftState) -> tuple[int, int]:
    imported = duplicates = 0
    for msg in draft_ws.iter_frames(path):
        event = draft_ws.parse_frame(msg)
        if isinstance(event, draft_ws.WsError):
            console.print(f"[yellow]unparsed frame:[/yellow] {event.raw!r} ({event.reason})")
            continue
        if not isinstance(event, draft_ws.Sold):
            continue
        # Each player sells at most once, so the ESPN playerId is a natural
        # stable dedupe key -- there's no per-slot pick id on this protocol
        # the way mDraftDetail has one.
        name, position = resolver.resolve(event.player_id)
        team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
        recorded = state.record_pick(name, position, event.price, team,
                                      espn_pick_id=event.player_id)
        imported += 1 if recorded else 0
        duplicates += 0 if recorded else 1
    return imported, duplicates


def cmd_replay(in_path: Path) -> int:
    resolver = draft_sync.PlayerResolver(VALUES_PATH)
    # A dedicated scratch path, never the live data/draft-state.json -- a
    # rehearsal must never be able to clobber real draft-day state.
    state = draft_state.DraftState(my_team="ME", state_path=SCRATCH_STATE_PATH)

    imported, duplicates = _replay_pass(in_path, resolver, state)
    console.print(f"Imported {imported} picks, {duplicates} duplicates suppressed.")

    # Replaying the identical file again should add nothing: proves the
    # playerId dedup holds across a full pass, the same guarantee that
    # protects the live poller from double-recording a sale it sees twice.
    reimported, _ = _replay_pass(in_path, resolver, state)
    console.print(f"Second pass imported {reimported} new picks (0 expected).")

    table = Table(title="Reconciliation")
    table.add_column("Team")
    table.add_column("Spent", justify="right")
    for team in state.all_teams():
        if state.spent_by(team):
            table.add_row(team, f"${state.spent_by(team)}")
    console.print(table)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", metavar="PATH", help="append raw frames to a JSONL fixture")
    group.add_argument("--replay", metavar="PATH", help="replay a JSONL fixture through the parser")
    parser.add_argument("--league-id", help="override ESPN_LEAGUE_ID (e.g. a practice draft)")
    parser.add_argument("--team-id", help="override ESPN_TEAM_ID")
    parser.add_argument("--duration", type=int, default=None,
                        help="stop after N seconds for --record (default: run until Ctrl-C)")
    args = parser.parse_args()

    if args.replay:
        return cmd_replay(Path(args.replay))

    cred = config.EspnCredentials()
    if args.league_id:
        cred.league_id = args.league_id
    if args.team_id:
        cred.team_id = args.team_id

    return cmd_record(Path(args.record), cred, args.duration)


if __name__ == "__main__":
    raise SystemExit(main())
