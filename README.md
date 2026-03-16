# tmux-claude-monitor

A terminal dashboard (TUI) and Telegram bot for monitoring multiple [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions running in tmux.

Built for setups where you run several Claude Code instances in parallel across tmux windows and need to keep track of which ones are running, waiting for input, or finished.

## Components

| File | Description |
|---|---|
| `claude_monitor.py` | TUI dashboard built with [Textual](https://github.com/Textualize/textual) |
| `tmux_claude_bot.py` | Telegram bot for remote monitoring and control |
| `tmux_claude_lib.py` | Shared library: tmux helpers, state detection, diff utilities |

## How it works

The monitor watches tmux panes for Claude Code sessions by:

1. **Discovering panes** — scans tmux sessions for windows named `dev1`, `review1`, `dev2`, `review2`, `dev3`, `review3`
2. **Detecting state** — analyzes captured pane output to classify each session:
   - **Running** — "esc to interrupt" visible, or spinner/background activity in status bar
   - **Plan** — Claude is asking a yes/no question or waiting for plan approval
   - **Error** — prompt visible with error indicators
   - **Idle** — prompt visible, nothing happening
   - **Busy** — non-Claude shell command running
   - **Shell** — plain shell prompt (no Claude)
3. **Diffing output** — uses `SequenceMatcher` with line normalization to show only genuinely new output, filtering out timer ticks and spinner changes
4. **Tracking context** — extracts "Context left: XX%" from Claude's status bar, warns at 25% and 10%

## TUI Dashboard

```
python claude_monitor.py
```

The sidebar groups panes by state:

- **Running** — active Claude sessions
- **Waiting** — plan prompts or recently finished (confirmed running before)
- **Busy** — shell commands in progress
- **Idle** — mixed active/idle sessions
- **Archive** — sessions where all panes are idle, shown as compact chips

### Keyboard commands

| Key | Action |
|---|---|
| `1`-`99` | Select pane by index |
| `0XX` | Expand/collapse session (e.g. `01`, `02`) |
| `q` | Deselect pane, or quit |
| `r` | Refresh pane list |
| `y` / `yes` | Approve plan prompt |
| `n` / `no` | Reject plan prompt |
| `b` / `bg` | Background Claude (sends Ctrl+B Ctrl+B) |
| `tbd` | Pin selected pane as "waiting" (survives restarts) |
| `untbd` | Unpin |
| Arrow Up/Down | Navigate Claude menu options (when input is empty) |
| Enter | Confirm selection in tmux pane (when input is empty) |
| Tab | Shell tab-completion (SHELL panes only) |
| Ctrl+C | Send interrupt to pane |
| Escape | Send ESC to pane |
| Ctrl+F | Toggle fullscreen (hide sidebar) |
| Ctrl+Q | Quit |

Any other text typed into the input bar is sent directly to the selected pane.

## Telegram Bot

```
python tmux_claude_bot.py
```

Requires a `.env` file:

```
TELEGRAM_BOT_TOKEN=your-bot-token
ALLOWED_CHAT_ID=your-chat-id
```

### Bot commands

| Command | Description |
|---|---|
| `/status` | Overview of all Claude panes with inline buttons |
| `/peek <id>` | Last 100 lines of a pane |
| `/attach <id>` | Live stream — shows new output since last visit, polls every 5s |
| `/up` | Scroll up for older output while attached |
| `/detach` | Stop live stream |
| `/send <id> text` | Send keystrokes to a pane |
| `/list` | All tmux sessions and windows |
| `/refresh` | Re-discover panes |

The bot also:
- Notifies you when a session finishes, errors, or enters plan mode
- Provides inline buttons for quick plan approval (Clear+Bypass / Bypass / Manual)
- Forwards unknown `/commands` to the attached pane (e.g. `/compact`, `/clear`)
- Accepts plain text messages as input to the attached pane

## Requirements

- Python 3.12+
- tmux
- `pip install textual python-telegram-bot python-dotenv`

## tmux window naming convention

The monitor expects Claude Code sessions to run in tmux windows named `dev1`, `review1`, `dev2`, `review2`, `dev3`, `review3`. A `dir` window per session is also tracked if present. Sessions where at least one monitor window exists will have missing windows auto-recreated.

## License

MIT
