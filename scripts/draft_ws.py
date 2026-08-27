#!/usr/bin/env python3
"""Raw capture tool for ESPN's live draft-room websocket.

`mDraftDetail` (see draft_sync.py) never updates during a live auction --
the draft room actually holds a websocket to a separate host,
`fantasydraft.espn.com`, captured from the browser's own Network tab on
2026-08-26. This script's only job is to connect read-only and log every
frame verbatim to a JSONL fixture, so a parser can be built against a
recording instead of a live draft:

    scripts/draft_ws.py --record OUT.jsonl

No parsing happens on `--record` on purpose -- it captures raw frames.
`--replay` feeds a recorded (or hand-captured) fixture through
`ff.draft_ws.parse_frame` and a scratch `DraftState`, the same
fixture-rehearsal pattern `draft_sync.py --replay` already uses.

    scripts/draft_ws.py --replay data/ws-live-test.jsonl

`--from-har` extracts and redacts the fantasydraft.espn.com websocket
messages out of a Chrome DevTools HAR export (Network tab -> right-click ->
"Save all as HAR with content"), in both directions, into the same fixture
shape --replay understands:

    scripts/draft_ws.py --from-har data/ws-capture.har --out data/ws-from-har.jsonl

By default `--record` sends nothing at all. `--ping` is the one exception:
it sends the browser's confirmed `PING PING%20<epochMs>` keepalive every
~15s (format and cadence from a HAR capture) and nothing else, so a
connection can survive past ESPN's ~65s silence timeout for tests that need
a long-lived, still-otherwise-silent connection.

Token: the host tolerates only one live connection per token -- reusing an
active session's token disconnects the other holder ("Duplicate Connection"),
and per Mark this is per-account, not per-token (a second browser hits the
same wall). So there is no session id to mint: `--record` connects using the
exact JOIN URL Mark captures by hand from his own browser's DevTools (Network
tab -> WS filter -> the JOIN request's full URL) and pastes into
`data/join-url.txt` (or a path given with `--join-url-file`). leagueId and
teamId are parsed back out of that URL for the console log and the Referer
header; the token itself is used verbatim, never reconstructed.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table
import websocket

from ff import config, draft_state, draft_sync, draft_ws
from ff.draft_ws import redact_token, parse_join_url

console = Console()
PING_INTERVAL_S = 15  # confirmed cadence from a HAR capture, see docs/notes/ws-protocol.md
VALUES_PATH = Path(__file__).resolve().parents[1] / "data" / "values.json"
SCRATCH_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "cache" / "draft-ws-state-replay.json"
DEFAULT_JOIN_URL_FILE = Path(__file__).resolve().parents[1] / "data" / "join-url.txt"


def load_join_url(path: Path) -> str:
    if not path.exists():
        raise SystemExit(
            f"No join URL at {path}. Open the draft room, copy the JOIN request's full URL "
            "from DevTools (Network tab -> WS filter), and paste it into that file."
        )
    url = path.read_text().strip()
    if not url:
        raise SystemExit(f"{path} is empty.")
    return url


def cmd_record(out_path: Path, join_url: str, cred: config.EspnCredentials, duration: int | None,
                send_ping: bool) -> int:
    if not cred.has_private_auth:
        console.print("[red]ESPN_SWID / ESPN_S2 required.[/red]")
        return 1

    try:
        league_id, team_id = parse_join_url(join_url)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print(f"Connecting to league {league_id} as team {team_id} (from pasted join URL).")
    ping_note = f", sending PING every {PING_INTERVAL_S}s" if send_ping else ", sending nothing"
    console.print(f"Logging every frame to {out_path}{ping_note}. Ctrl-C to stop.")

    fh = out_path.open("a")
    start = time.monotonic()
    stop_ping = threading.Event()

    def _log(direction: str, msg: str) -> None:
        fh.write(json.dumps({"ts": time.time(), "dir": direction, "msg": redact_token(msg)}) + "\n")
        fh.flush()

    def on_message(ws, message):
        _log("receive", message)
        console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] recv {message[:120]}")

    def _ping_loop(ws):
        while not stop_ping.wait(PING_INTERVAL_S):
            # Every sent frame in the HAR capture is newline-terminated, PING
            # included -- omitting it means the server never sends a PONG back
            # (confirmed live: 0 PONGs across two tests that omitted it).
            frame = f"PING PING%20{int(time.time() * 1000)}\n"
            try:
                ws.send(frame)
            except Exception as exc:
                console.print(f"[red]ping send failed:[/red] {exc}")
                return
            _log("send", frame)
            console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] sent {frame}")

    def on_open(ws):
        console.print("[green]Connected.[/green]")
        if send_ping:
            threading.Thread(target=_ping_loop, args=(ws,), daemon=True).start()

    def on_error(ws, error):
        console.print(f"[red]error:[/red] {error}")

    def on_close(ws, code, msg):
        stop_ping.set()
        console.print(f"[yellow]closed:[/yellow] {code} {msg}")

    ws = websocket.WebSocketApp(
        join_url,
        cookie=f"espn_s2={cred.espn_s2}; SWID={cred.swid}",
        header=[
            "Origin: https://fantasy.espn.com",
            f"Referer: https://fantasy.espn.com/football/draft?leagueId={league_id}",
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
            timer = threading.Timer(duration, ws.close)
            timer.start()
        ws.run_forever()
    except KeyboardInterrupt:
        console.print("Stopped.")
    finally:
        stop_ping.set()
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


def cmd_from_har(har_path: Path, out_path: Path) -> int:
    har = json.loads(har_path.read_text())
    ws_entries = [e for e in har["log"]["entries"] if "_webSocketMessages" in e]
    draft_entries = [e for e in ws_entries if "fantasydraft.espn.com" in e["request"]["url"]]
    if not draft_entries:
        console.print("[red]No fantasydraft.espn.com websocket connection found in this HAR.[/red]")
        return 1
    if len(draft_entries) > 1:
        console.print(f"[yellow]{len(draft_entries)} fantasydraft.espn.com connections found "
                       "in this HAR; using the first.[/yellow]")

    frames = draft_entries[0]["_webSocketMessages"]
    sent = sum(1 for f in frames if f.get("type") == "send")
    with out_path.open("w") as fh:
        for frame in frames:
            direction = "send" if frame.get("type") == "send" else "receive"
            fh.write(json.dumps({
                "ts": frame["time"],
                "dir": direction,
                "msg": redact_token(frame["data"]),
            }) + "\n")
    console.print(f"Extracted {len(frames)} frames ({sent} sent, {len(frames) - sent} received) "
                  f"to {out_path}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", metavar="PATH", help="append raw frames to a JSONL fixture")
    group.add_argument("--replay", metavar="PATH", help="replay a JSONL fixture through the parser")
    group.add_argument("--from-har", metavar="PATH", help="extract a fixture from a DevTools HAR export")
    parser.add_argument("--out", type=Path, help="output path for --from-har")
    parser.add_argument("--join-url-file", type=Path, default=DEFAULT_JOIN_URL_FILE,
                        help=f"file holding the pasted JOIN URL (default: {DEFAULT_JOIN_URL_FILE})")
    parser.add_argument("--duration", type=int, default=None,
                        help="stop after N seconds for --record (default: run until Ctrl-C)")
    parser.add_argument("--ping", action="store_true",
                        help="send the browser's PING keepalive every ~15s for --record "
                             "(default: send nothing at all)")
    args = parser.parse_args()

    if args.replay:
        return cmd_replay(Path(args.replay))

    if args.from_har:
        if not args.out:
            parser.error("--from-har requires --out")
        return cmd_from_har(Path(args.from_har), args.out)

    cred = config.EspnCredentials()
    join_url = load_join_url(args.join_url_file)
    return cmd_record(Path(args.record), join_url, cred, args.duration, args.ping)


if __name__ == "__main__":
    raise SystemExit(main())
