#!/usr/bin/env python3
"""Raw capture tool for ESPN's live draft-room websocket.

`mDraftDetail` (see draft_sync.py) never updates during a live auction --
the draft room actually holds a websocket to a separate host,
`fantasydraft.espn.com`, captured from the browser's own Network tab on
2026-08-26. This script's only job is to connect read-only and log every
frame verbatim to a JSONL fixture, so a parser can be built against a
recording instead of a live draft:

    scripts/draft_ws.py --record OUT.jsonl --league-id N --team-id N

No parsing happens here on purpose. `--record` captures raw frames; a later
module turns `sold`/`bid` frames into ResolvedPick the same way draft_sync.py
turns mDraftDetail rows into ResolvedPick.

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
import websocket

from ff import config

console = Console()

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

    def _log(direction: str, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = redact_token(raw)
        fh.write(json.dumps({"ts": time.time(), "dir": direction, "payload": payload}) + "\n")
        fh.flush()

    def on_message(ws, message):
        _log("recv", message)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", metavar="PATH", required=True,
                        help="append raw frames to a JSONL fixture")
    parser.add_argument("--league-id", help="override ESPN_LEAGUE_ID (e.g. a practice draft)")
    parser.add_argument("--team-id", help="override ESPN_TEAM_ID")
    parser.add_argument("--duration", type=int, default=None,
                        help="stop after N seconds (default: run until Ctrl-C)")
    args = parser.parse_args()

    cred = config.EspnCredentials()
    if args.league_id:
        cred.league_id = args.league_id
    if args.team_id:
        cred.team_id = args.team_id

    return cmd_record(Path(args.record), cred, args.duration)


if __name__ == "__main__":
    raise SystemExit(main())
