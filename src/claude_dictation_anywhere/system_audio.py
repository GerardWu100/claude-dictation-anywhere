"""Mute the computer's speakers while recording, so music or a podcast doesn't leak into dictation.

Mutes the default output device instead of setting the volume to 0, so the
volume level is never touched. Only this app's own change is undone: an output
that was already muted stays muted, and if the default output changes while
recording, the device this app muted is the one unmuted.

    Linux:  wpctl (PipeWire), else pactl (PulseAudio or pipewire-pulse)
    macOS:  CoreAudio names the default device; osascript mutes it, and CoreAudio
            unmutes it if it is no longer the default

A device is a string with its backend, e.g. "wpctl:57", "pactl:alsa_output.pci",
"coreaudio:82". The record file in the runtime folder holds {"device": ...} and
exists from just before muting until unmuting has worked, so a copy killed
mid-recording unmutes on its next start. Writing it first is safe because the
device was checked to be unmuted just before: undoing a mute that never landed
changes nothing.

Limit: if you press mute yourself while recording, that is not noticed; the
device still unmutes at release.
"""

import ctypes
import json
import logging
import re
import shutil
import subprocess
import threading

from .settings import IS_MAC, runtime_dir, write_private_file

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 3
RECORD_FILE_NAME = "output_muted_by_app.json"
WPCTL, PACTL, COREAUDIO = "wpctl", "pactl", "coreaudio"

# CoreAudio constants (AudioHardware.h): a property is named by a selector, scope, and element.
AUDIO_SYSTEM_OBJECT = 1
AUDIO_ELEMENT_MAIN = 0
SELECTOR_DEFAULT_OUTPUT = int.from_bytes(b"dOut", "big")
SELECTOR_MUTE = int.from_bytes(b"mute", "big")
SCOPE_GLOBAL = int.from_bytes(b"glob", "big")
SCOPE_OUTPUT = int.from_bytes(b"outp", "big")
CORE_AUDIO_LIBRARY = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"

_lock = threading.Lock()  # one mute or restore at a time, so a late mute cannot follow a restore


class _AudioPropertyAddress(ctypes.Structure):
    """CoreAudio's AudioObjectPropertyAddress."""

    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


def _run(args: list[str]) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=COMMAND_TIMEOUT_S
    ).stdout


def _mac_default_output_id() -> int:
    """AudioDeviceID of the default output. Raises OSError if CoreAudio refuses."""
    library = ctypes.CDLL(CORE_AUDIO_LIBRARY)
    address = _AudioPropertyAddress(SELECTOR_DEFAULT_OUTPUT, SCOPE_GLOBAL, AUDIO_ELEMENT_MAIN)
    device = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(device))
    status = library.AudioObjectGetPropertyData(
        AUDIO_SYSTEM_OBJECT,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(device),
    )
    if status != 0 or device.value == 0:
        raise OSError(f"CoreAudio status {status}")
    return device.value


def _mac_set_device_muted(device_id: int, muted: bool) -> None:
    """Mute a device that need not be the default. Raises CalledProcessError if it refuses."""
    library = ctypes.CDLL(CORE_AUDIO_LIBRARY)
    address = _AudioPropertyAddress(SELECTOR_MUTE, SCOPE_OUTPUT, AUDIO_ELEMENT_MAIN)
    value = ctypes.c_uint32(1 if muted else 0)
    status = library.AudioObjectSetPropertyData(
        device_id, ctypes.byref(address), 0, None, ctypes.sizeof(value), ctypes.byref(value)
    )
    if status != 0:  # the device has gone, or has no mute control
        raise subprocess.CalledProcessError(status, "AudioObjectSetPropertyData")


def default_output_device() -> str | None:
    """The default output with its backend, or None if no tool can name it."""
    if IS_MAC:
        return f"{COREAUDIO}:{_mac_default_output_id()}"
    if shutil.which(WPCTL):
        # First line: "id 57, type PipeWire:Interface:Node"
        match = re.match(r"id (\d+)", _run([WPCTL, "inspect", "@DEFAULT_AUDIO_SINK@"]))
        return f"{WPCTL}:{match.group(1)}" if match else None
    if shutil.which(PACTL):
        name = _run([PACTL, "get-default-sink"]).strip()
        return f"{PACTL}:{name}" if name else None
    return None


def device_muted(device: str) -> bool:
    """True if ``device`` (the current default) is muted."""
    backend, _, name = device.partition(":")
    if backend == COREAUDIO:
        return _run(["osascript", "-e", "output muted of (get volume settings)"]).strip() == "true"
    if backend == WPCTL:
        # "Volume: 0.82" or "Volume: 0.82 [MUTED]"
        return "[MUTED]" in _run([WPCTL, "get-volume", name])
    # "Mute: yes" / "Mute: no"
    return "yes" in _run([PACTL, "get-sink-mute", name])


def set_device_muted(device: str, muted: bool) -> None:
    """Mute or unmute exactly ``device``.

    Raises
    ------
    subprocess.CalledProcessError
        If the device refuses, usually because it has gone.
    OSError, subprocess.TimeoutExpired
        If the tool could not run or answer; whether it took effect is unknown.
    """
    backend, _, name = device.partition(":")
    if backend == COREAUDIO:
        if int(name) == _mac_default_output_id():
            _run(["osascript", "-e", f"set volume output muted {str(muted).lower()}"])
        else:  # osascript only reaches the default device
            _mac_set_device_muted(int(name), muted)
    elif backend == WPCTL:
        _run([WPCTL, "set-mute", name, "1" if muted else "0"])
    else:
        _run([PACTL, "set-sink-mute", name, "1" if muted else "0"])


def _record_path():
    return runtime_dir() / RECORD_FILE_NAME


def _recorded_device() -> str | None:
    """The device this app muted; a damaged record authorises nothing and is removed."""
    path = _record_path()
    try:
        record = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        record = None
    device = record.get("device") if isinstance(record, dict) else None
    backend = device.partition(":")[0] if isinstance(device, str) else ""
    if backend in (WPCTL, PACTL) or (backend == COREAUDIO and device.partition(":")[2].isdigit()):
        return device
    log.warning("removing a damaged mute record")
    path.unlink(missing_ok=True)
    return None


def mute_output() -> None:
    """Mute the output if it is playing; never raises (dictation matters more than silence)."""
    with _lock:
        try:
            device = default_output_device()
            if device is None:
                log.warning("could not tell which output is the default, so it stays unmuted")
                return
            if device_muted(device):
                return
            write_private_file(_record_path(), json.dumps({"device": device}))
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("could not mute the speakers: %s", type(error).__name__)
            return
        try:
            set_device_muted(device, True)
        except (OSError, subprocess.TimeoutExpired) as error:
            # Unknown whether it landed: keep the record, and unmuting later is still correct.
            log.warning("could not mute the speakers: %s", type(error).__name__)
        except subprocess.CalledProcessError:
            log.warning("the output refused to mute")
            _forget_record()


def _forget_record() -> None:
    try:
        _record_path().unlink(missing_ok=True)
    except OSError as error:
        log.warning("could not remove the mute record: %s", error.strerror)


def restore_output() -> None:
    """Undo this app's mute, also after a crash; never raises, and safe to repeat."""
    with _lock:
        try:
            device = _recorded_device()
            if device is None:
                return
            set_device_muted(device, False)
        except subprocess.CalledProcessError:
            log.warning("the output this app muted has gone; nothing to unmute")
        except (OSError, subprocess.TimeoutExpired) as error:
            log.warning("could not unmute the speakers: %s", type(error).__name__)
            return  # kept, so the next restore tries again
        _forget_record()
