# Claude Dictation Anywhere

Goal: hold a key anywhere on Linux or macOS, speak, and get Claude Code's own `/voice` transcript typed at the cursor and copied to the clipboard. Free on top of the user's Claude subscription.

Guiding principles:

- **Account safety first.** Only drive the real, unmodified `claude` program installed on the system, through its terminal UI. Never read its credentials, never call Anthropic endpoints directly, never bundle Claude Code. One dictation per human key press.
- **Never send a prompt.** The hidden session has no tools; the transcript is pulled out with Ctrl+G before anything is submitted.
- **Simple over clever.** Tray icon plus a localhost settings page; no native GUI toolkit (a Tk window was tried and rejected as ugly).
- **Be sparing with real dictation requests when testing.** Prefer startup-only checks; a real recording only when it proves something new.

Layout: `src/claude_dictation_anywhere/` (`claude_session` drives Claude Code, `hotkeys` reads the shortcut, `controller` is the state machine, `output` types or copies, `web_ui` + `web/index.html` are the settings page, `tray` the icon). Tests: `uv run python -m unittest discover -s tests`. Lint: `uv run ruff check src tests && uv run ruff format src tests`.
