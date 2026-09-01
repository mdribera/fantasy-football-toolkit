"""System-audio playback for the live auction console -- draft night's
distinct cues for a player being nominated, a closing clock worth acting
on, and my turn to nominate. Textual has no built-in audio, so this shells
out to whichever system player is on PATH, discovered once at
construction time, and degrades silently -- no player found, no audio
device, a mid-draft failure -- rather than ever raising into the event
loop."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SOUNDS_DIR = Path(__file__).resolve().parents[2] / "assets" / "sounds"

EVENTS = ("nominated", "five", "my-turn")

# First one found on PATH wins. Args are appended after the player binary;
# "{path}" is substituted with the WAV file's path.
PLAYERS: dict[str, list[str]] = {
    "paplay": ["{path}"],
    "aplay": ["-q", "{path}"],
    "ffplay": ["-nodisp", "-autoexit", "-loglevel", "quiet", "{path}"],
    "afplay": ["{path}"],
}


class SoundPlayer:
    """Discovers a system player once, then fires WAVs by event name.
    `enabled` is the live mute toggle; a missing player, a missing WAV, or
    a failed subprocess spawn is silent, not an error."""

    def __init__(self) -> None:
        self.enabled = True
        self._player: str | None = None
        self._args: list[str] = []
        for name, args in PLAYERS.items():
            found = shutil.which(name)
            if found:
                self._player = found
                self._args = args
                break

    def play(self, event: str) -> bool:
        """Fires the WAV for `event`. Returns whether a sound actually
        played, so a caller can fall back to something else (the terminal
        bell) when it didn't."""
        if not self.enabled or self._player is None:
            return False
        path = SOUNDS_DIR / f"{event}.wav"
        if not path.exists():
            return False
        args = [arg.format(path=str(path)) for arg in self._args]
        try:
            subprocess.Popen([self._player, *args],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        return True
