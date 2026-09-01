"""Tests for src/ff/sounds.py -- the console's system-audio playback.
Player discovery and subprocess spawning are both mocked; this never
actually plays a sound, only checks the wiring that would."""

from __future__ import annotations

import subprocess

from ff import sounds


def test_prefers_paplay_when_multiple_players_are_on_path(monkeypatch):
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("paplay", "aplay") else None)
    player = sounds.SoundPlayer()
    assert player._player == "/usr/bin/paplay"
    assert player._args == ["{path}"]


def test_falls_back_to_the_next_player_in_order(monkeypatch):
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda name: "/usr/bin/aplay" if name == "aplay" else None)
    player = sounds.SoundPlayer()
    assert player._player == "/usr/bin/aplay"
    assert player._args == ["-q", "{path}"]


def test_no_player_found_disables_playback_silently(monkeypatch):
    monkeypatch.setattr(sounds.shutil, "which", lambda name: None)
    player = sounds.SoundPlayer()
    assert player._player is None
    assert player.play("nominated") is False


def test_play_builds_the_expected_argv(monkeypatch):
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda name: "/usr/bin/aplay" if name == "aplay" else None)
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()

    monkeypatch.setattr(sounds.subprocess, "Popen", fake_popen)
    player = sounds.SoundPlayer()
    assert player.play("nominated") is True
    (argv, kwargs), = calls
    assert argv == ["/usr/bin/aplay", "-q", str(sounds.SOUNDS_DIR / "nominated.wav")]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_play_is_silent_when_muted(monkeypatch):
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda name: "/usr/bin/paplay" if name == "paplay" else None)
    calls = []
    monkeypatch.setattr(sounds.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    player = sounds.SoundPlayer()
    player.enabled = False
    assert player.play("nominated") is False
    assert calls == []


def test_play_is_silent_for_an_unknown_event(monkeypatch):
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda name: "/usr/bin/paplay" if name == "paplay" else None)
    player = sounds.SoundPlayer()
    assert player.play("does-not-exist") is False


def test_play_degrades_silently_when_the_player_fails_to_spawn(monkeypatch):
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda name: "/usr/bin/paplay" if name == "paplay" else None)

    def fake_popen(*a, **k):
        raise OSError("no such device")

    monkeypatch.setattr(sounds.subprocess, "Popen", fake_popen)
    player = sounds.SoundPlayer()
    assert player.play("nominated") is False


def test_every_declared_event_has_a_committed_wav_file():
    for event in sounds.EVENTS:
        assert (sounds.SOUNDS_DIR / f"{event}.wav").exists()
