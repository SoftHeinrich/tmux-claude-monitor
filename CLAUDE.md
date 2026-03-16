# Claude Monitor

TUI dashboard for monitoring Claude Code tmux sessions, built with Python/Textual.

## Project Structure

- `claude_monitor.py` — Main TUI app (Textual). Sidebar shows panes grouped by state, main panel shows selected pane output with live diffing.
- `tmux_claude_bot.py` — Telegram bot for remote monitoring of the same tmux sessions.
- `tmux_claude_lib.py` — Shared library: tmux helpers, state detection, diff utilities.

## Key Concepts

- **Pane states**: RUNNING, IDLE, ERROR, SHELL, BUSY, PLAN. Detected via tmux capture content analysis.
- **MONITOR_WINDOWS**: `dev1`, `review1`, `dev2`, `review2`, `dev3`, `review3` — tmux windows to monitor per session.
- **State detection**: "esc to interrupt" (case-insensitive) → RUNNING, spinner/background patterns with prompt → RUNNING, prompt indicators (❯/⏵) → IDLE/PLAN/ERROR, diff-based fallback for no-prompt cases.
- **Confirmed running**: Panes with "esc to interrupt" or spinner/background patterns are treated as genuinely running (for idle-since tracking). Only diff-based RUNNING detection does not trigger "waiting" status.
- **Context tracking**: Extracts "Context left: XX%" from Claude status bar, warns at 25% and 10%.
- **Diff-based output**: Uses `SequenceMatcher` with line normalization to show only genuinely new output.
- **Auto-recovery**: Missing monitor windows are auto-recreated if a session has at least one existing monitor window.

## Sidebar Groups

- **Running** — panes with active Claude (esc to interrupt visible)
- **Waiting** — PLAN state or recently finished running (genuinely confirmed)
- **Busy** — non-Claude commands running in shell
- **Idle** — sessions with mix of active/idle panes
- **Archive** — sessions where ALL panes are idle, shown as compact chips

## Commands (TUI input bar)

- Number → select pane by index
- `0XX` → expand/collapse session
- `q` → deselect pane or quit
- `r` → refresh
- `tbd` → pin selected pane as "waiting" (never idle); only `untbd` removes the pin.
- `b` / `bg` → background Claude (sends Ctrl+B Ctrl+B)
- `y` / `yes` → confirm current PLAN prompt
- `n` / `no` → reject current PLAN prompt
- Arrow Up/Down (empty input) → navigate Claude menu options
- Enter (empty input) → confirm selection in tmux pane
- Tab → shell tab-completion (SHELL panes only)
- Ctrl+C → send interrupt to pane
- Escape → send ESC to pane

## Style

- Python 3.12+, type hints where useful
- No external deps beyond `textual` and `python-telegram-bot`
- Keep code concise; avoid over-abstraction
- Use `rich_escape()` for all raw tmux content written to RichLog (markup=True)
