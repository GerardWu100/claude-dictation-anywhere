"""A stand-in for Claude Code, for tests: same keys, same screen words, no network, no microphone.

Started in a real pty by ClaudeVoiceSession. Reads FAKE_CLAUDE_OPTIONS (JSON):

    startup          "prompt" | "trust" | "trust_changed" | "menu" | "logged_out" | "exit"
                     | "unknown_option"
    transcript       text that arrives when processing ends
    processing_s     how long "Voice: processing…" stays up
    notice           error notice shown when processing ends
    sticky_notice    error notice shown from the start and never removed
    no_processing    skip the processing state, as a future version might
    redraw_forever   keep "processing" and keep redrawing, so it never finishes
    ignore_ctrl_g    Ctrl+G does nothing
    freeze_warmup    stick at "keep holding…" and ignore every key
    stop_reading     never read the keyboard, so the pty fills up
    ignore_term      survive SIGTERM
    spawn_helper     start a helper process in the same group; its pid goes to keylog + ".helper"
    voice_refused    every hold shows this notice at once instead of recording; each one
                     appends a line to keylog + ".connects", like a connection attempt
    logged_out       `auth status` reports that nobody is signed in

Every key received is appended to FAKE_CLAUDE_KEYLOG, so a test can prove what was
never pressed (Enter above all).
"""

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import tty

RULE = "─" * 100
HOLD_DETECT_SPACES = 3
RELEASE_GAP_S = 0.15


options = json.loads(os.environ.get("FAKE_CLAUDE_OPTIONS", "{}"))
keylog = open(os.environ["FAKE_CLAUDE_KEYLOG"], "ab", buffering=0)  # noqa: SIM115


def draw(lines: list[str]) -> None:
    sys.stdout.write("\x1b[2J\x1b[H" + "\r\n".join(lines))
    sys.stdout.flush()


def draw_prompt(text: str, footer: str = "0 tokens", notice: str = "") -> None:
    notice = notice or options.get("sticky_notice", "")
    wrapped = [text[i : i + 96] for i in range(0, len(text), 96)] or [""]
    box = [f"❯ {wrapped[0]}", *(f"  {row}" for row in wrapped[1:])]
    draw(["Claude Code", "", notice, RULE, *box, RULE, f"  mode  {footer}"])


def read_keys() -> bytes:
    data = os.read(0, 4096)
    keylog.write(data)
    return data


def trust_dialog(changed: bool) -> None:
    yes = "Sure, trust it" if changed else "Yes, I trust this folder"
    selected = 0
    while True:
        rows = ["No, exit", yes]
        draw(
            [RULE, " Quick safety check: Is this a project you created or trust?", ""]
            + [f" {'❯' if i == selected else ' '} {row}" for i, row in enumerate(rows)]
            + ["", " Enter to confirm · Esc to cancel"]
        )
        data = read_keys()
        if b"\x1b[B" in data:
            selected = 1
        if b"\r" in data:
            if selected == 0:
                sys.exit(3)
            return


def run_editor(prompt: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as file:
        file.write(prompt)
    subprocess.run([os.environ["EDITOR"], file.name], check=False)
    with open(file.name, encoding="utf-8") as result:
        text = result.read()
    os.unlink(file.name)
    return text


def main() -> None:
    if sys.argv[1:3] == ["auth", "status"]:
        print(json.dumps({"loggedIn": not options.get("logged_out")}))
        sys.exit(1 if options.get("logged_out") else 0)
    if "--safe-mode" not in sys.argv:
        print("error: required flags missing")
        sys.exit(2)
    tty.setraw(0)
    if options.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if options.get("spawn_helper"):
        helper = subprocess.Popen(["sleep", "60"])
        with open(os.environ["FAKE_CLAUDE_KEYLOG"] + ".helper", "w") as file:
            file.write(str(helper.pid))
    startup = options.get("startup", "prompt")
    if startup == "exit":
        sys.exit(1)
    if startup == "unknown_option":
        print("error: unknown option '--safe-mode'")
        sys.exit(1)
    if startup == "menu":
        draw(["Choose the text style", " ❯ ✔ Dark mode", "   Light mode"])
        while True:
            read_keys()
    if startup in ("trust", "trust_changed"):
        trust_dialog(startup == "trust_changed")
    prompt = ""
    if startup == "logged_out":
        draw_prompt("", notice="Not logged in · Please run /login")
    else:
        draw_prompt("")
    if options.get("stop_reading"):
        time.sleep(3600)

    spaces, last_space = 0, 0.0
    phase = None  # None, "warmup", "listening", "processing"
    processing_until = 0.0
    while True:
        ready, _, _ = select.select([0], [], [], 0.01)
        now = time.monotonic()
        if ready:
            data = read_keys()
            if not data:
                return
            if options.get("freeze_warmup") and phase == "warmup":
                continue
            for key in data:
                if key == 0x20:
                    spaces += 1
                    last_space = now
                    if phase is None and spaces >= HOLD_DETECT_SPACES:
                        if options.get("voice_refused"):
                            spaces = 0
                            with open(os.environ["FAKE_CLAUDE_KEYLOG"] + ".connects", "a") as file:
                                file.write("connect\n")
                            draw_prompt(prompt, notice=options["voice_refused"])
                            continue
                        phase = "warmup"
                        draw_prompt(prompt, "· keep holding…")
                    elif phase == "warmup" and spaces >= HOLD_DETECT_SPACES + 2:
                        if not options.get("freeze_warmup"):
                            phase = "listening"
                            draw_prompt(prompt, "listening…")
                elif key == 0x1B and phase:  # Esc discards the recording
                    phase, spaces = None, 0
                    draw_prompt(prompt)
                elif key == 0x07 and not options.get("ignore_ctrl_g"):
                    prompt = run_editor(prompt)
                    draw_prompt(prompt)
        if phase in ("warmup", "listening") and now - last_space > RELEASE_GAP_S:
            if phase == "warmup" and options.get("freeze_warmup"):
                continue
            spaces = 0
            if options.get("no_processing"):
                phase = None
                prompt += options.get("transcript", "")
                draw_prompt(prompt, notice=options.get("notice", ""))
            else:
                phase = "processing"
                processing_until = now + options.get("processing_s", 0.1)
                draw_prompt(prompt + options.get("interim", ""), "Voice: processing…")
        if phase == "processing":
            if options.get("redraw_forever"):
                draw_prompt(prompt + options.get("interim", ""), f"Voice: processing… {now}")
                time.sleep(0.05)
            elif now >= processing_until:
                phase = None
                prompt += options.get("transcript", "")
                draw_prompt(prompt, notice=options.get("notice", ""))


if __name__ == "__main__":
    main()
