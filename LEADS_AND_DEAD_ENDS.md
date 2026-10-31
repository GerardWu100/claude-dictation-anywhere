# Leads and dead ends

## Decisions

- 2026-10-25 Drive the real Claude Code CLI in a pty instead of calling its voice WebSocket with the token from `~/.claude/.credentials.json`. Anthropic's consumer terms (Feb 2026) allow subscription OAuth only inside Claude Code and Claude.ai; a homemade client risks the account.
- 2026-10-25 Settings and history live on a localhost browser page, not a native window. A Tk window was built and rejected by the user as ugly (Tk 9 on GNOME: tiny fonts, huge checkboxes); the browser page is simpler.
- 2026-10-25 The hidden session runs in its own empty folder (`<data dir>/dictation`), outside any git repo, because Claude Code feeds the folder name and git branch to dictation as word hints. The app answers the first-run "trust this folder" prompt itself; the session has no tools, so that is safe.
- 2026-10-31 Silence other audio during recording by muting the default output, not by pausing players (players live in different browsers/apps; MPRIS pause was unreliable for the user) and not by setting volume 0 (mute leaves the level untouched). Mute after the start sound ends, unmute at release rather than after transcription.

## Gotchas
- 2026-10-25 macOS AppKit crashes if a controller worker updates the tray menu directly. Dispatch tray refresh, visibility, and stop to the main event loop; Linux permits the existing worker updates.

- 2026-10-25 A hidden `claude` started from inside another Claude Code session inherits `CLAUDE_*` / `ANTHROPIC_*` variables and shows "Not logged in". Strip them.
- 2026-10-25 `claude --bare` disables OAuth, so `/voice` is unavailable; use `--safe-mode`. `--safe-mode` plus voice settings passed through `--settings` does record. `--no-session-persistence` only works with `--print`.
- 2026-10-25 "Not logged in" appears a few seconds after the prompt draws, so a startup check misses it; detect leftover typed spaces after a hold instead.
- 2026-10-25 With escape codes stripped, Claude Code's screen often has no spaces between words (they are cursor moves), and its renderer redraws only changed cells: the recording footer reads "listning…". Match markers with whitespace removed, and never rely on the word "listening".
- 2026-10-25 Voice warm-up is short: "keep holding…" at 0.04 s, recording at about 0.13 s after the first repeated space.
- 2026-10-25 Keychron mice: the sniper button sends KEY_F8 through the receiver's keyboard interface ("Keychron  Keychron Link  Keyboard", double spaces), and receivers appear as several devices. ydotoold's virtual device also reports side buttons; ignore it or typed text echoes back.
- 2026-10-25 pystray on GNOME needs PyGObject (builds from PyPI on Arch) and the AppIndicator extension. In the built-in browser pane, `document.hidden` is true, so the page must fetch once regardless of visibility.
- 2026-10-25 A GitHub wiki's git repo (`<repo>.wiki.git`) does not exist until someone saves the first page in the web UI; `gh repo edit --enable-wiki` alone is not enough. The wiki repo is the only copy of the wiki pages; edit them there.
- 2026-10-26 After rewriting commits to drop an author, GitHub's repo-page Contributors sidebar stays stale even when the contributors API is already clean. Switching the default branch away and back to the same name does not refresh it; renaming the default branch (`master` → `main`) did, at once. The global git identity is AIkry; this repo sets a local Gerard Wu / GerardWu100 identity.

## Failed approaches

- 2026-10-25 Codex dictation. *disproved*. Codex CLI 0.164 has no dictation code. The ChatGPT/Codex desktop app's global dictation is app-scoped on Linux and cannot paste there. Its `chatgpt.com/backend-api/transcribe` endpoint answers scripts with a 403 bot-check page; do not try to get around it.
- 2026-10-25 pi (pi-mono) as a lighter Codex front end. *disproved*: no built-in dictation, and its ChatGPT login covers chat models only.
- 2026-10-25 Shrinking the hidden Claude Code with Bun memory options (`BUN_OPTIONS=--smol`, `BUN_JSC_forceRAMSize`). *disproved*: RSS stayed at about 250 MB and PSS (shared memory split fairly) at about 194 MB. About 141 MB is its own working memory; about 108 MB is program code the kernel can drop and reload under memory pressure. Only quitting the session saves more.

## Untried ideas

- Start at login (Linux `~/.config/autostart` entry, macOS LaunchAgent) as an opt-in setting: removes the manual start; about an hour.
- Remember the focused window at press time (macOS: frontmost app via AppKit) and refuse to type if it changed: prevents text landing in the wrong window; not possible on GNOME Wayland without a Shell extension.
