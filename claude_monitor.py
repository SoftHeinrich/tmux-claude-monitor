#!/usr/bin/env python3
"""Claude Monitor TUI — terminal dashboard for Claude Code tmux sessions."""

import re
import subprocess
import time
from collections import OrderedDict

from rich.markup import escape as rich_escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
import textwrap

from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog, Static, TextArea



from tmux_claude_lib import (
    State, STATE_ICONS, MONITORED_PANES,
    tmux_sessions, tmux_windows, tmux_capture, tmux_send,
    detect_state, has_completion_summary, _prev_states, _idle_since, _confirmed_running, _pinned_waiting, _save_pinned,
    _normalize_line, _strip_status, _extract_context_pct, diff_new_lines,
    refresh_monitored, _fmt_ago, MONITOR_WINDOWS, _short_prefixes,
    ensure_monitor_windows,
)


# ── Data types ────────────────────────────────────────────────────────────

class PaneInfo:
    __slots__ = ("target", "label", "state", "idx", "context_pct", "task_name")

    def __init__(self, target: str, label: str, state: State, idx: int,
                 context_pct: str | None = None, task_name: str | None = None):
        self.target = target
        self.label = label
        self.state = state
        self.idx = idx
        self.context_pct = context_pct  # e.g. "42" for 42%
        self.task_name = task_name      # Claude's current task from pane title


class SessionInfo:
    __slots__ = ("name", "short", "sid")

    def __init__(self, name: str, short: str, sid: str):
        self.name = name    # full session name
        self.short = short  # short prefix
        self.sid = sid      # e.g. "01", "02", ...


class SafeListView(ListView):
    """ListView that ignores click races during sidebar rebuilds."""

    def _on_list_item__child_clicked(self, event) -> None:
        event._no_default_action = True
        event.stop()
        self.focus()
        try:
            self.index = self._nodes.index(event.item)
            self.post_message(self.Selected(self, event.item, self.index))
        except ValueError:
            pass


class SendInput(TextArea):
    """TextArea that submits on Enter instead of inserting newline."""

    async def _on_key(self, event) -> None:
        if event.key == "tab":
            # Never switch focus; do shell tab-completion for SHELL panes
            pane = self.app.selected_pane
            if pane and pane.state == State.SHELL:
                self._shell_tab_complete(pane.target)
            event.prevent_default()
            event.stop()
            return
        if event.key == "enter":
            text = self.text.strip()
            if text:
                self.clear()
                self.app._handle_command(text)
            else:
                # Empty enter → forward to tmux pane (confirm prompt selection)
                self.clear()
                pane = self.app.selected_pane
                if pane:
                    subprocess.run(["tmux", "send-keys", "-t", pane.target, "Enter"], capture_output=True)
            event.prevent_default()
            event.stop()
        elif event.key == "ctrl+c":
            if self.app.selected_pane:
                subprocess.run(["tmux", "send-keys", "-t", self.app.selected_pane.target, "C-c"], capture_output=True)
                rlog = self.app.query_one("#output-view", RichLog)
                rlog.write("[bold cyan]> Ctrl+C[/bold cyan]")
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            if self.app.selected_pane:
                subprocess.run(["tmux", "send-keys", "-t", self.app.selected_pane.target, "Escape"], capture_output=True)
                rlog = self.app.query_one("#output-view", RichLog)
                rlog.write("[bold cyan]> ESC[/bold cyan]")
            event.prevent_default()
            event.stop()
        elif event.key in ("up", "down"):
            # Forward arrow keys to tmux pane for navigating Claude prompts
            pane = self.app.selected_pane
            if pane and not self.text:
                key = "Up" if event.key == "up" else "Down"
                subprocess.run(["tmux", "send-keys", "-t", pane.target, key], capture_output=True)
                event.prevent_default()
                event.stop()
                return
            await super()._on_key(event)
        else:
            # Let TextArea handle all other keys (printable chars, backspace, etc.)
            await super()._on_key(event)

    def _shell_tab_complete(self, target: str) -> None:
        """Use the remote shell's tab completion by typing into tmux and reading back."""
        current = self.text
        # Clear the remote line, type our text, send Tab
        subprocess.run(["tmux", "send-keys", "-t", target, "C-u"], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "C-k"], capture_output=True)
        if current:
            subprocess.run(["tmux", "send-keys", "-t", target, "-l", current], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "Tab"], capture_output=True)
        time.sleep(0.15)
        # Read back what the shell completed — grab the last line with content
        text = tmux_capture(target, lines=5)
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            return
        last_line = lines[-1]
        # Strip common shell prompts to get just the command text
        # Match patterns like "user@host:~$ ", "$ ", "% ", "❯ ", "> "
        m = re.search(r'(?:[\$%❯>#])\s*(.*)', last_line)
        completed = m.group(1) if m else last_line.strip()
        if completed and completed != current:
            self.clear()
            self.insert(completed)
        # Clean up the remote line so it doesn't execute
        subprocess.run(["tmux", "send-keys", "-t", target, "C-u"], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "C-k"], capture_output=True)

    async def _on_paste(self, event) -> None:
        """Handle terminal paste — strip newlines, prevent TextArea's default."""
        event._no_default_action = True
        if self.read_only:
            return
        text = event.text.replace("\n", " ").replace("\r", "")
        self.insert(text)


# ── CSS ───────────────────────────────────────────────────────────────────

APP_CSS = """
#main {
    height: 1fr;
}
#pane-list {
    overflow-y: auto;
}
#sidebar:focus-within {
    border-right: solid $accent-lighten-2;
}
.group-header {
    color: $text-muted;
    padding: 0 1;
    text-style: bold;
}
.pane-item {
    padding: 0 1;
    height: 1;
}
.pane-item-2row {
    padding: 0 1;
    height: auto;
}
ListItem.auto-height {
    height: auto;
}
Static.pane-item-2row {
    padding: 0 1;
    height: auto;
}
.pane-item.selected, .pane-item-2row.selected, Static.pane-item-2row.selected {
    background: $accent;
    color: $text;
    text-style: bold;
}
#sidebar {
    width: 28;
    border-right: solid $accent;
}
#pane-list {
    width: 1fr;
}
#theme-toggle {
    width: 100%;
    min-width: 0;
    height: 1;
    margin: 0;
    padding: 0 1;
    border: none;
    text-style: dim;
    dock: bottom;
}
#output-panel {
    width: 1fr;
}
#output-view {
    height: 1fr;
    border: none;
    scrollbar-size: 1 1;
}
#send-bar {
    height: auto;
    max-height: 8;
    padding: 0 1;
}
#send-bar Label {
    height: 1;
    color: $accent;
}
#send-input {
    height: auto;
    min-height: 1;
    max-height: 6;
    background: $boost;
    border: none;
}
"""

# ── App ───────────────────────────────────────────────────────────────────

class ClaudeMonitorApp(App):
    CSS = APP_CSS
    TITLE = "Claude Monitor"
    BINDINGS = [
        Binding("ctrl+q", "real_quit", "Quit", priority=True),
        Binding("ctrl+f", "toggle_fullscreen", "Fullscreen", priority=True),
    ]
    ENABLE_COMMAND_PALETTE = False

    def action_quit(self) -> None:
        """Only quit via our q command, not Ctrl+C."""
        pass

    def action_real_quit(self) -> None:
        self.exit()

    def action_toggle_fullscreen(self) -> None:
        sidebar = self.query_one("#sidebar", Vertical)
        sidebar.display = not sidebar.display

    def __init__(self):
        super().__init__()
        self.panes: list[PaneInfo] = []
        self.sessions: list[SessionInfo] = []
        self.selected_pane: PaneInfo | None = None
        self._prev_output_norm: list[str] = []
        self._full_content: list[str] = []
        self._capture_depth = 200
        self._expanded_session: str | None = None
        self._task_name_cache: dict[str, str] = {}  # target → last known task name
        self._context_warned: dict[str, int] = {}   # target → last warned threshold

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield SafeListView(id="pane-list")
                yield Button("☀ Light", id="theme-toggle", variant="default")
            with Vertical(id="output-panel"):
                yield RichLog(id="output-view", wrap=True, markup=True)
                with Vertical(id="send-bar"):
                    yield Label("", id="send-label")
                    yield SendInput(id="send-input")

    def on_mount(self) -> None:
        self._do_refresh()
        self.set_interval(5, self._poll_states)
        self.set_interval(3, self._poll_output)
        # Always focus input
        self.query_one("#send-input", SendInput).focus()

    # ── Pane discovery ────────────────────────────────────────────────────

    def _discover_all_panes(self) -> list[PaneInfo]:
        refresh_monitored()
        all_sessions = tmux_sessions()
        short = _short_prefixes(all_sessions)

        # Build session list with IDs: 01, 02, ..., 09, 010, 011, ...
        self.sessions = []
        for i, session in enumerate(sorted(all_sessions)):
            sid = f"0{i + 1}"
            self.sessions.append(SessionInfo(session, short[session], sid))

        # Get pane commands and titles
        pane_cmds, pane_titles = self._get_pane_info()

        # Recreate any killed monitor windows
        for si in self.sessions:
            recreated = ensure_monitor_windows(si.name)
            if recreated:
                rlog = self.query_one("#output-view", RichLog)
                for w in recreated:
                    rlog.write(f"[bold yellow]⚠ Recreated missing window: {si.name}:{w}[/bold yellow]")

        panes = []
        idx = 0
        for si in self.sessions:
            windows = tmux_windows(si.name)
            for win in ["dir"] + MONITOR_WINDOWS:
                if win in windows:
                    idx += 1
                    target = f"{si.name}:{win}"
                    label = f"{si.short} {win}"
                    title = pane_titles.get(target, "")
                    cmd = pane_cmds.get(target, "")
                    is_claude = cmd in ("node", "claude") or (title and ord(title[0]) > 127)
                    text = tmux_capture(target, lines=50)
                    raw_lines = text.splitlines()
                    context_pct = _extract_context_pct(raw_lines) if is_claude else None
                    if is_claude:
                        state = detect_state(target)
                        # detect_state may return SHELL if capture is empty/short;
                        # but we know it's Claude, so treat as IDLE
                        if state == State.SHELL:
                            state = State.IDLE
                    else:
                        is_shell_cmd = cmd in ("bash", "zsh", "fish", "sh", "tmux", "")
                        if not is_shell_cmd:
                            state = State.BUSY
                        else:
                            state = State.SHELL
                    _prev_states[target] = state
                    # Extract task name from title: "✳ Claude Code" → None, "⠐ Fix Bug" → "Fix Bug"
                    task_name = None
                    if is_claude and title and ord(title[0]) > 127:
                        name_part = title[1:].strip()
                        if name_part and name_part != "Claude Code":
                            task_name = name_part
                    # Cache task names so they persist across spinner changes
                    if task_name:
                        self._task_name_cache[target] = task_name
                    elif target in self._task_name_cache and is_claude:
                        task_name = self._task_name_cache[target]
                    panes.append(PaneInfo(target, label, state, idx, context_pct, task_name))
        return panes

    @staticmethod
    def _get_pane_info() -> tuple[dict[str, str], dict[str, str]]:
        """Get current command and title for each pane via tmux."""
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{session_name}:#{window_name}\t#{pane_current_command}\t#{pane_title}"],
            capture_output=True, text=True,
        )
        cmds = {}
        titles = {}
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    cmds[parts[0]] = parts[1]
                if len(parts) >= 3:
                    titles[parts[0]] = parts[2]
        return cmds, titles

    def _do_refresh(self) -> None:
        self.panes = self._discover_all_panes()
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        lv = self.query_one("#pane-list", SafeListView)
        lv.clear()

        # Time header
        now_str = time.strftime("%H:%M:%S")
        lv.append(ListItem(Label(f" {now_str}", classes="group-header")))

        # Build session ID lookup
        sess_id: dict[str, str] = {}  # session_name -> "01", "02", ...
        for si in self.sessions:
            sess_id[si.name] = si.sid

        groups: dict[str, list[PaneInfo]] = {
            "running": [], "waiting": [], "busy": [], "idle": [], "archive": [],
        }
        now = time.time()
        for p in self.panes:
            if p.state == State.RUNNING:
                groups["running"].append(p)
            elif p.state == State.BUSY:
                groups["busy"].append(p)
            elif p.state == State.PLAN:
                groups["waiting"].append(p)
            elif p.state in (State.IDLE, State.ERROR):
                if p.target in _pinned_waiting:
                    groups["waiting"].append(p)
                elif (idle_ts := _idle_since.get(p.target)) and (now - idle_ts) < 7200:
                    groups["waiting"].append(p)
                else:
                    groups["idle"].append(p)
            elif p.state == State.SHELL:
                if p.target in _pinned_waiting:
                    groups["waiting"].append(p)
                else:
                    groups["idle"].append(p)
            else:
                groups["idle"].append(p)

        # Move sessions where ALL panes are idle into archive
        active_sessions: set[str] = set()
        for g in ("running", "waiting", "busy"):
            for p in groups[g]:
                active_sessions.add(p.target.split(":")[0])
        archived: list[PaneInfo] = []
        remaining_idle: list[PaneInfo] = []
        # Find which session has the selected pane
        selected_sess = self.selected_pane.target.split(":")[0] if self.selected_pane else None
        for p in groups["idle"]:
            sess = p.target.split(":")[0]
            # Keep out of archive if session has active panes or selected pane
            if sess not in active_sessions and sess != selected_sess:
                archived.append(p)
            else:
                remaining_idle.append(p)
        groups["idle"] = remaining_idle
        groups["archive"] = archived

        def _by_session(items: list[PaneInfo]) -> dict[str, list[PaneInfo]]:
            d: dict[str, list[PaneInfo]] = OrderedDict()
            for p in items:
                sess = p.target.split(":")[0]
                d.setdefault(sess, []).append(p)
            return d

        def _add_panes_expanded(panes_in: list[PaneInfo], short: str, show_ago: bool = False, two_row: bool = False):
            for p in panes_in:
                win = p.target.split(":")[1]
                w = win.replace("dev", "d").replace("review", "r")
                suffix = ""
                if p.target in _pinned_waiting:
                    suffix += " \U0001f4cc"
                if p.state == State.PLAN:
                    suffix += " \U0001f4cb"
                if show_ago:
                    idle_ts = _idle_since.get(p.target)
                    if idle_ts:
                        suffix += f" ({_fmt_ago(idle_ts)})"
                if p.context_pct is not None:
                    ctx = int(p.context_pct)
                    if ctx <= 25:
                        suffix += f" \u26a0{ctx}%"
                    elif ctx <= 50:
                        suffix += f" {ctx}%"
                is_sel = self.selected_pane and self.selected_pane.target == p.target
                display_name = p.task_name
                if two_row and display_name:
                    line1 = f"  {p.idx} {short} {w}{suffix}"
                    # Wrap task name to fit panel (28 width - 4 indent - 2 padding)
                    wrapped = textwrap.fill(display_name, width=22, initial_indent="    ", subsequent_indent="    ")
                    full_text = f"{line1}\n{wrapped}"
                    cls = "pane-item-2row selected" if is_sel else "pane-item-2row"
                    item = ListItem(Static(full_text, classes=cls), name=p.target, classes="auto-height")
                    lv.append(item)
                else:
                    if display_name:
                        suffix += f" {display_name}"
                    text = f"  {p.idx} {short} {w}{suffix}"
                    cls = "pane-item selected" if is_sel else "pane-item"
                    lv.append(ListItem(Label(text, classes=cls), name=p.target))

        def add_group(icon: str, title: str, items: list[PaneInfo], show_ago: bool = False, compact: bool = False, two_row: bool = False):
            if not items:
                return
            lv.append(ListItem(Label(f"{icon} {title}", classes="group-header")))
            for sess, panes_in in _by_session(items).items():
                short = panes_in[0].label.split()[0]
                sid = sess_id.get(sess, "?")
                expanded = not compact or sess == self._expanded_session
                if self.selected_pane and any(p.target == self.selected_pane.target for p in panes_in):
                    expanded = True

                if expanded:
                    if compact:
                        arrow = "\u25bc"  # ▼
                        has_sel = self.selected_pane and any(
                            p.target == self.selected_pane.target for p in panes_in)
                        cls = "pane-item selected" if has_sel else "pane-item"
                        lv.append(ListItem(
                            Label(f" {arrow} {sid} {short}", classes=cls),
                            name=f"toggle:{sess}"))
                    _add_panes_expanded(panes_in, short, show_ago, two_row=two_row)
                else:
                    arrow = "\u25b6"  # ▶
                    parts = []
                    for p in panes_in:
                        win = p.target.split(":")[1]
                        w = win.replace("dev", "d").replace("review", "r")
                        parts.append(f"{p.idx}·{w}")
                    text = f" {arrow} {sid} {short}: {' '.join(parts)}"
                    lv.append(ListItem(Label(text, classes="pane-item"), name=f"toggle:{sess}"))

        add_group("\u2699\ufe0f", "Running", groups["running"], two_row=True)
        add_group("\u2705", "Waiting", groups["waiting"], show_ago=True, two_row=True)
        add_group("\U0001f528", "Busy", groups["busy"])
        add_group("\U0001f4a4", "Idle", groups["idle"], compact=True, two_row=True)

        # Archive: compact list, expandable inline
        if groups["archive"]:
            archive_sessions = _by_session(groups["archive"])
            lv.append(ListItem(Label(f"\U0001f4e6 Archive ({len(archive_sessions)})", classes="group-header")))
            for sess, panes_in in archive_sessions.items():
                sid = sess_id.get(sess, "?")
                short = panes_in[0].label.split()[0]
                if sess == self._expanded_session:
                    lv.append(ListItem(
                        Label(f" \u25bc {sid} {sess}", classes="pane-item"),
                        name=f"toggle:{sess}"))
                    _add_panes_expanded(panes_in, short)
                else:
                    lv.append(ListItem(
                        Label(f"  {sid}\u00b7{sess}", classes="pane-item"),
                        name=f"toggle:{sess}"))

    # ── Polling ───────────────────────────────────────────────────────────

    def _poll_states(self) -> None:
        # Snapshot previous states before discovery overwrites them
        old_states = dict(_prev_states)
        new_panes = self._discover_all_panes()
        for p in new_panes:
            prev_state = old_states.get(p.target)
            if p.state == State.RUNNING:
                _idle_since.pop(p.target, None)
            elif prev_state == State.RUNNING and p.target not in _idle_since:
                # Only mark as "recently idle" if it was genuinely running
                # (had "esc to interrupt"), not just diff-detected
                if p.target in _confirmed_running:
                    _idle_since[p.target] = time.time()
                    _confirmed_running.discard(p.target)
            elif p.state in (State.IDLE, State.PLAN, State.ERROR) and \
                    prev_state is None and \
                    p.target not in _idle_since and has_completion_summary(p.target):
                # First poll for this pane (after monitor start): pane shows a
                # spinner summary (e.g. "✻ Sautéed for 1m 14s") but we never
                # observed it running — seed idle_since so it appears in the
                # "waiting" group instead of "idle".
                _idle_since[p.target] = time.time()
        self.panes = new_panes
        self._check_context_warnings(new_panes)
        self._rebuild_list()

    def _check_context_warnings(self, panes: list[PaneInfo]) -> None:
        """Warn in the output view when a pane's context drops below thresholds."""
        rlog = self.query_one("#output-view", RichLog)
        for p in panes:
            if p.context_pct is None:
                continue
            ctx = int(p.context_pct)
            prev_threshold = self._context_warned.get(p.target, 100)
            # Warn at 25% and 10% thresholds
            if ctx <= 10 and prev_threshold > 10:
                rlog.write(f"[bold red]⚠ {p.label}: context critically low ({ctx}%) — consider /compact or /clear[/bold red]")
                self._context_warned[p.target] = 10
            elif ctx <= 25 and prev_threshold > 25:
                rlog.write(f"[bold yellow]⚠ {p.label}: context low ({ctx}%)[/bold yellow]")
                self._context_warned[p.target] = 25
            elif ctx > 25:
                # Reset warnings when context recovers (e.g. after /clear)
                self._context_warned.pop(p.target, None)

    def _poll_output(self) -> None:
        if not self.selected_pane:
            return
        target = self.selected_pane.target
        output = tmux_capture(target, lines=200)
        raw_lines = output.splitlines()
        cur_content = _strip_status(raw_lines)
        cur_norm = [_normalize_line(l) for l in cur_content]
        prev_norm = self._prev_output_norm

        self._prev_output_norm = cur_norm
        if not prev_norm or cur_norm == prev_norm:
            return

        new_lines = diff_new_lines(prev_norm, cur_norm, cur_content)
        if not new_lines:
            return

        self._full_content.extend(new_lines)
        rlog = self.query_one("#output-view", RichLog)
        for line in new_lines:
            rlog.write(rich_escape(line))

    # ── Actions ───────────────────────────────────────────────────────────

    def _select_pane(self, pane: PaneInfo) -> None:
        self.selected_pane = pane
        self._capture_depth = 2000
        rlog = self.query_one("#output-view", RichLog)
        rlog.clear()

        output = tmux_capture(pane.target, lines=self._capture_depth)
        content = _strip_status(output.splitlines())
        self._full_content = list(content)
        self._prev_output_norm = [_normalize_line(l) for l in content]

        rlog.write(f"[bold]--- {rich_escape(pane.label)} ({rich_escape(pane.target)}) ---[/bold]")
        for line in content:
            rlog.write(rich_escape(line))

        self.query_one("#send-label", Label).update(f"[bold]{pane.label}[/bold]:")
        self._rebuild_list()
        self.query_one("#send-input", SendInput).focus()

    def _deselect(self) -> None:
        self.selected_pane = None
        self._prev_output_norm = []
        self._full_content = []
        rlog = self.query_one("#output-view", RichLog)
        rlog.clear()
        rlog.write("[dim]Select a pane to view output[/dim]")
        self.query_one("#send-label", Label).update("")
        self._rebuild_list()
        self.query_one("#send-input", SendInput).focus()

    def _load_more_history(self) -> None:
        if not self.selected_pane:
            return
        old_depth = self._capture_depth
        self._capture_depth += 500
        target = self.selected_pane.target
        output = tmux_capture(target, lines=self._capture_depth)
        all_content = _strip_status(output.splitlines())

        output_old = tmux_capture(target, lines=old_depth)
        old_content = _strip_status(output_old.splitlines())

        if len(all_content) <= len(old_content):
            return

        older = all_content[:len(all_content) - len(old_content)]
        self._full_content = older + self._full_content
        rlog = self.query_one("#output-view", RichLog)
        rlog.clear()
        rlog.write(f"[bold]--- {rich_escape(self.selected_pane.label)} ({rich_escape(self.selected_pane.target)}) ---[/bold]")
        for line in self._full_content:
            rlog.write(rich_escape(line))
        rlog.scroll_home(animate=False)

    def on_rich_log_scroll_up(self) -> None:
        rlog = self.query_one("#output-view", RichLog)
        if rlog.scroll_offset.y <= 0 and self.selected_pane:
            self._load_more_history()

    def on_click(self, event) -> None:
        """Always keep input focused."""
        self.set_timer(0.05, lambda: self.query_one("#send-input", SendInput).focus())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = event.item.name
        if not name:
            return
        if name.startswith("toggle:"):
            sess = name[7:]
            if self._expanded_session == sess:
                self._expanded_session = None
            else:
                self._expanded_session = sess
            self._rebuild_list()
            self.query_one("#send-input", SendInput).focus()
            return
        for p in self.panes:
            if p.target == name:
                self._select_pane(p)
                return

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "theme-toggle":
            is_dark = self.current_theme.dark if self.current_theme else True
            self.theme = "textual-light" if is_dark else "textual-dark"
            btn = self.query_one("#theme-toggle", Button)
            # After toggle: was dark → now light (offer dark), was light → now dark (offer light)
            btn.label = "☾ Dark" if is_dark else "☀ Light"
            self.query_one("#send-input", SendInput).focus()

    def _handle_command(self, text: str) -> None:
        # Commands
        if text == "q":
            if self.selected_pane:
                self._deselect()
            else:
                self.exit()
            return
        if text == "r":
            self._do_refresh()
            return

        # 0-prefixed → session ID (expand/collapse)
        if text.startswith("0") and len(text) >= 2:
            for si in self.sessions:
                if si.sid == text:
                    if self._expanded_session == si.name:
                        self._expanded_session = None
                    else:
                        self._expanded_session = si.name
                    self._rebuild_list()
                    return
            return

        # Plain number → attach to pane
        if text.isdigit():
            n = int(text)
            for p in self.panes:
                if p.idx == n:
                    self._select_pane(p)
                    return
            return

        # "tbd" → pin selected pane as waiting (never idle)
        if text == "tbd" and self.selected_pane:
            target = self.selected_pane.target
            _pinned_waiting.add(target)
            _save_pinned()
            _idle_since[target] = time.time()
            rlog = self.query_one("#output-view", RichLog)
            rlog.write("[bold yellow]> pinned as waiting[/bold yellow]")
            self._rebuild_list()
            return
        if text == "untbd" and self.selected_pane:
            target = self.selected_pane.target
            _pinned_waiting.discard(target)
            _save_pinned()
            rlog = self.query_one("#output-view", RichLog)
            rlog.write("[bold yellow]> unpinned[/bold yellow]")
            self._rebuild_list()
            return

        # "b"/"bg" → send Ctrl+B Ctrl+B to background Claude in selected pane
        if text in ("b", "bg") and self.selected_pane:
            target = self.selected_pane.target
            subprocess.run(["tmux", "send-keys", "-t", target, "C-b"], capture_output=True)
            subprocess.run(["tmux", "send-keys", "-t", target, "C-b"], capture_output=True)
            rlog = self.query_one("#output-view", RichLog)
            rlog.write("[bold cyan]> background (Ctrl+B Ctrl+B)[/bold cyan]")
            return

        # Text → send to selected pane
        if not self.selected_pane:
            return
        pane = self.selected_pane
        rlog = self.query_one("#output-view", RichLog)

        # Plan mode: use arrows + empty Enter to navigate menus.
        # "y"/"yes" → confirm current selection, "n"/"no" → move to No option and confirm.
        # Anything else → type as literal text (for free-form input after "Other").
        if pane.state == State.PLAN:
            target = pane.target
            if text.lower() in ("yes", "y"):
                subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True)
                rlog.write(f"[bold green]> yes[/bold green]")
                self._full_content.append(f"> yes")
            elif text.lower() in ("no", "n"):
                # Navigate down to find a "No" option, then confirm
                subprocess.run(["tmux", "send-keys", "-t", target, "Down"], capture_output=True)
                subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True)
                rlog.write(f"[bold yellow]> no[/bold yellow]")
                self._full_content.append(f"> no")
            else:
                # Free-form text: type it literally and press Enter
                subprocess.run(["tmux", "send-keys", "-t", target, "-l", text], capture_output=True)
                subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True)
                rlog.write(f"[bold yellow]> {text}[/bold yellow]")
                self._full_content.append(f"> {text}")
            return

        ok = tmux_send(pane.target, text)
        if ok:
            rlog.write(f"[bold cyan]> {text}[/bold cyan]")
            self._full_content.append(f"> {text}")
        else:
            rlog.write(f"[bold red]Failed to send: {text}[/bold red]")



if __name__ == "__main__":
    app = ClaudeMonitorApp()
    app.run()
