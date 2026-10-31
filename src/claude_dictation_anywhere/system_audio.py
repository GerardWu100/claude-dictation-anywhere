"""Mute the computer's speakers while recording, so music or a podcast doesn't leak into dictation.

Mutes the default output device instead of setting the volume to 0, so the
volume level is never touched. Only unmutes what this app muted: if the output
was already muted, it stays muted.

    Linux:  wpctl (PipeWire), else pactl (PulseAudio or pipewire-pulse)
    macOS:  osascript "set volume output muted"

A marker file in the runtime folder records "the app muted the output", so a
copy killed mid-recording unmutes on its next start.
"""

import logging
import shutil
import subprocess

from .settings import IS_MAC, runtime_dir

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 3


def _run(args: list[str]) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=COMMAND_TIMEOUT_S
    ).stdout


def output_muted() -> bool:
    """True if the default output device is muted. Raises OSError/CalledProcessError if unknown."""
    if IS_MAC:
        return _run(["osascript", "-e", "output muted of (get volume settings)"]).strip() == "true"
    if shutil.which("wpctl"):
        # "Volume: 0.82" or "Volume: 0.82 [MUTED]"
        return "[MUTED]" in _run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
    # "Mute: yes" / "Mute: no"
    return "yes" in _run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])


def set_output_muted(muted: bool) -> None:
    if IS_MAC:
        _run(["osascript", "-e", f"set volume output muted {str(muted).lower()}"])
    elif shutil.which("wpctl"):
        _run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if muted else "0"])
    else:
        _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"])


def _marker():
    return runtime_dir() / "output_muted_by_app"


def mute_output() -> None:
    """Mute the output if it is playing; never raises (dictation matters more than silence)."""
    try:
        if output_muted():
            return
        _marker().touch()
        set_output_muted(True)
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("could not mute the speakers: %s", error)


def restore_output() -> None:
    """Unmute only if this app muted it (also after a crash); never raises."""
    marker = _marker()
    if not marker.exists():
        return
    try:
        set_output_muted(False)
        marker.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("could not unmute the speakers: %s", error)
