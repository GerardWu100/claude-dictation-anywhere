# Claude Dictation Anywhere

Goal: hold a key anywhere on Linux or macOS, speak, and get Claude Code's own `/voice` transcript typed at the cursor and copied to the clipboard. Free on top of the user's Claude subscription.

Guiding principles:

- **Account safety first.** Only drive the real, unmodified `claude` program installed on the system, through its terminal UI. Never read its credentials, never call Anthropic endpoints directly, never bundle Claude Code. One dictation per human key press.
- **Never send a prompt.** The hidden session has no tools; the transcript is pulled out with Ctrl+G before anything is submitted.
- **Simple over clever.** Tray icon plus a localhost settings page; no native GUI toolkit (a Tk window was tried and rejected as ugly).
- **Be sparing with real dictation requests when testing.** Prefer startup-only checks; a real recording only when it proves something new.

Layout: `src/claude_dictation_anywhere/` (`claude_session` drives Claude Code, `terminal_screen` reads its screen, `hotkeys` reads the shortcut, `mac_events` is the macOS event tap, typing and permission checks, `controller` is the state machine, `output` types or copies, `web_ui` + `web/index.html` are the settings page, `tray` the menu, `icon` draws it). Tests: `uv run python -m unittest discover -s tests`; they use `tests/fake_claude.py` in a real pty and screens recorded from real Claude Code in `tests/fixtures/`, so ordinary test runs need no login, microphone, or network. `CDA_LIVE_LINUX=1 uv run python -m unittest tests.test_live_linux` adds real kernel input, ydotool, PipeWire, tray, and the whole app on a Linux desktop, still with the fake Claude Code; quit the real app and do not type meanwhile. Lint: `uv run ruff check src tests && uv run ruff format src tests`.

## Public project: privacy

Strangers can see this project: its published output, and for public repositories every file and commit. Never write the owner's email, local paths (such as `/Users/<user>`), account numbers or IDs, holdings or trades, names, code or data from private repositories, server addresses, or other people's details. This also applies to anything copied in from other projects. The owner's name is allowed only where it is already public, such as the website byline. When unsure, leave it out and ask.
