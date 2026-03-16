"""Shared tmux helpers for Claude Code monitoring."""

import json
import re
import subprocess
import time
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path


# Sessions and windows to monitor.
MONITOR_WINDOWS = ["dev1", "review1", "dev2", "review2", "dev3", "review3"]

MONITORED_PANES: dict[str, str] = {}


class State(Enum):
    RUNNING = "running"
    IDLE = "idle"
    ERROR = "error"
    SHELL = "shell"
    BUSY = "busy"       # shell command running (not Claude)
    PLAN = "plan"
    UNKNOWN = "unknown"


STATE_ICONS = {
    State.RUNNING: "\u2699\ufe0f",   # ⚙️
    State.IDLE: "\u2705",             # ✅
    State.ERROR: "\u274c",            # ❌
    State.SHELL: "\U0001f41a",        # 🐚
    State.BUSY: "\U0001f528",         # 🔨
    State.PLAN: "\U0001f4cb",         # 📋
    State.UNKNOWN: "\u2753",          # ❓
}

# Previous captures for diff-based running detection
_prev_captures: dict[str, str] = {}
# Previous state per pane for transition detection
_prev_states: dict[str, State] = {}
# Timestamp when pane entered IDLE/PLAN/ERROR state
_idle_since: dict[str, float] = {}
# Panes confirmed running via "esc to inter[rupt]" (not just diff-based)
_confirmed_running: set[str] = set()
# Regex for spinner summary lines that appear when Claude finishes a task
# e.g. "✻ Sautéed for 1m 14s", "✻ Brewed for 3m 2s"
_COMPLETION_RE = re.compile(r"^\s*[✻✶✽✢·*]\s+\S+.*\bfor\s+\d+[ms]")
# Panes manually pinned as "waiting" (never grouped as idle)
_PINNED_FILE = Path(__file__).parent / ".pinned_waiting.json"

def _load_pinned() -> set[str]:
    try:
        return set(json.loads(_PINNED_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def _save_pinned() -> None:
    _PINNED_FILE.write_text(json.dumps(sorted(_pinned_waiting)))

_pinned_waiting: set[str] = _load_pinned()


# ── tmux helpers ──────────────────────────────────────────────────────────

def tmux_sessions() -> list[str]:
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    return r.stdout.strip().splitlines() if r.returncode == 0 else []


def tmux_windows(session: str) -> list[str]:
    r = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True, text=True,
    )
    return r.stdout.strip().splitlines() if r.returncode == 0 else []


def tmux_capture(target: str, lines: int = 80) -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else ""


def tmux_send(target: str, keys: str) -> bool:
    text = tmux_capture(target, lines=10)
    is_busy = "esc to inter" in text.lower()

    if not is_busy:
        subprocess.run(["tmux", "send-keys", "-t", target, "C-u"], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "C-k"], capture_output=True)
    r1 = subprocess.run(
        ["tmux", "send-keys", "-t", target, "-l", keys],
        capture_output=True, text=True,
    )
    r2 = subprocess.run(
        ["tmux", "send-keys", "-t", target, "Enter"],
        capture_output=True, text=True,
    )
    return r1.returncode == 0 and r2.returncode == 0


def ensure_monitor_windows(session: str) -> list[str]:
    """Recreate any missing MONITOR_WINDOWS in a session. Returns list of recreated window names."""
    windows = tmux_windows(session)
    has_any = any(w in windows for w in MONITOR_WINDOWS)
    if not has_any:
        return []
    recreated = []
    for win in MONITOR_WINDOWS:
        if win not in windows:
            subprocess.run(
                ["tmux", "new-window", "-t", session, "-n", win, "-d"],
                capture_output=True,
            )
            recreated.append(win)
    return recreated


# ── normalization ─────────────────────────────────────────────────────────

# Spinner chars used by Claude Code: ✻ ✶ ✽ ✢ · *
_SPINNER = r"[✻✶✽✢·*]"
_SPINNER_RE = re.compile(rf"^\s*{_SPINNER}\s+")


def _normalize_line(line: str) -> str:
    """Strip volatile parts (timers, spinners) so timer-only changes are ignored."""
    if _SPINNER_RE.match(line):
        m = re.match(rf"(\s*){_SPINNER}\s+(\S+)", line)
        if m:
            return f"{m.group(1)}SPINNER {m.group(2)}"
    s = re.sub(r"^\s*[●⎿]\s*", "  ", line)
    s = re.sub(r"Context left[^%]*%", "Context left", s)
    s = re.sub(r"·\s+\d+\s+\w+(\s*\+\d+\s*-\d+)?", "·", s)
    s = re.sub(r"↓\s+[\d.]+k?\s+tokens", "", s)
    s = re.sub(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", "", s)
    s = re.sub(r"^\s+", "  ", s)
    return s


def _extract_context_pct(lines: list[str]) -> str | None:
    """Extract 'Context left: XX%' from the status area at the bottom."""
    for line in lines[-8:]:
        m = re.search(r"Context left[:\s]*(\d+)%", line)
        if m:
            return m.group(1)
    return None


def _strip_status(lines: list[str]) -> list[str]:
    """Strip the Claude Code status/prompt area from the bottom of capture."""
    cut = len(lines)
    for i in range(len(lines) - 1, max(len(lines) - 8, -1), -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if stripped.startswith("❯") or stripped.startswith("⏵") or \
           set(stripped.replace(" ", "")).issubset(set("─━")):
            cut = i
    return lines[:cut]


# ── state detection ───────────────────────────────────────────────────────

def detect_state(target: str, update_capture: bool = True) -> State:
    """Detect pane state using diff-based approach."""
    text = tmux_capture(target, lines=50)
    if not text.strip():
        return State.SHELL

    # Check bottom lines for status indicators
    tail = "\n".join(text.splitlines()[-8:])

    # Check for prompt FIRST — when Claude finishes, old "esc to interrupt" text
    # may still be within the last 8 lines above the new ❯ prompt.
    has_prompt = "❯" in tail or "⏵" in tail

    # Direct running indicator — only when no prompt is visible.
    # If there's a prompt, any "esc to interrupt" in the tail is stale output
    # above the prompt line, not the active status bar.
    if not has_prompt and "esc to i" in tail.lower():
        _confirmed_running.add(target)
        if update_capture:
            _prev_captures[target] = text
        return State.RUNNING

    if has_prompt and any(s in tail for s in (
        "Would you like to proceed?",
        "Yes, clear context and bypass",
        "Do you want to",
        "ExitPlanMode",
        "plan mode",
        "Plan mode",
        "Yes, and",
        "No, and",
        "Yes / No",
    )):
        if update_capture:
            _prev_captures[target] = text
        return State.PLAN

    # Detect yes/no style prompts (multiple choice questions from Claude)
    if has_prompt and re.search(r"(?:Yes|No|Approve)[,\s]", tail):
        if re.search(r"[>\?]\s*$", tail, re.MULTILINE):
            if update_capture:
                _prev_captures[target] = text
            return State.PLAN

    if has_prompt and re.search(r"(ERROR|Error|panic|Traceback|FAILED)", tail):
        if update_capture:
            _prev_captures[target] = text
        return State.ERROR

    # Prompt visible but Claude still actively working → RUNNING
    # Check the status bar (last 2 lines, below ❯) for live activity indicators.
    # Do NOT match "background tasks" in output above ❯ — that's stale text.
    if has_prompt:
        status_bar = "\n".join(text.splitlines()[-2:])
        if re.search(
            r"\d+\s+bash(?:es)?\s+·\s+↓|local agents?"
            r"|↓\s+[\d.]+k?\s+tokens"
            r"|/btw to ask",
            status_bar,
        ):
            _confirmed_running.add(target)
            if update_capture:
                _prev_captures[target] = text
            return State.RUNNING

    # Idle prompt visible (❯ at bottom) — not running
    if has_prompt:
        if update_capture:
            _prev_captures[target] = text
        return State.IDLE

    # Normalize for comparison: strip status bar noise
    text_norm = "\n".join(_normalize_line(l) for l in _strip_status(text.splitlines()))
    prev_cap = _prev_captures.get(target)
    if update_capture:
        _prev_captures[target] = text_norm

    if prev_cap is not None and text_norm != prev_cap:
        return State.RUNNING

    return State.IDLE


def has_completion_summary(target: str) -> bool:
    """Check if the last real output before the prompt is a spinner summary.

    Returns True only when the completion line (e.g. '✻ Sautéed for 1m 14s')
    is the final substantive content before the prompt area — nothing but blank
    lines and separators between it and ❯.  This avoids false positives from
    old summaries that have scrolled up.
    """
    text = tmux_capture(target, lines=50)
    lines = text.splitlines()
    # Find the ❯ prompt line (not the ⏵ status bar) scanning from bottom
    prompt_idx = None
    for i in range(len(lines) - 1, max(len(lines) - 10, -1), -1):
        stripped = lines[i].strip()
        if stripped.startswith("❯"):
            prompt_idx = i
            break
    if prompt_idx is None:
        return False
    # Walk upward from prompt, skipping blanks and separator lines
    _SEP = set("─━ ")
    for i in range(prompt_idx - 1, max(prompt_idx - 6, -1), -1):
        s = lines[i].strip()
        if not s or set(s).issubset(_SEP):
            continue
        return bool(_COMPLETION_RE.match(lines[i]))
    return False


# ── pane discovery ────────────────────────────────────────────────────────

def _short_prefixes(names: list[str]) -> dict[str, str]:
    """Compute shortest unique prefix for each name (min 3 chars)."""
    prefixes = {}
    for name in names:
        for length in range(3, len(name) + 1):
            prefix = name[:length]
            if sum(1 for n in names if n[:length] == prefix) == 1:
                prefixes[name] = prefix
                break
        else:
            prefixes[name] = name
    return prefixes


def discover_panes() -> dict[str, str]:
    """Build MONITORED_PANES from live tmux sessions."""
    sessions = tmux_sessions()
    short = _short_prefixes(sessions)
    panes = {}
    for session in sessions:
        windows = tmux_windows(session)
        for win in MONITOR_WINDOWS:
            if win in windows:
                key = f"{session}:{win}"
                panes[key] = f"{short[session]} {win}"
    return panes


def refresh_monitored():
    global MONITORED_PANES
    MONITORED_PANES.update(discover_panes())


def get_claude_panes() -> list[tuple[str, str, State]]:
    """Return (target, label, state) for panes with Claude Code open."""
    refresh_monitored()
    results = []
    for target, label in sorted(MONITORED_PANES.items()):
        text = tmux_capture(target, lines=30)
        if text.strip() and "bypass permissions on" in text:
            state = _prev_states.get(target, State.IDLE)
            results.append((target, label, state))
    return results


def resolve_pane(arg: str) -> str | None:
    """Resolve a pane by ID number or session:window name."""
    panes = get_claude_panes()
    try:
        idx = int(arg)
        if 1 <= idx <= len(panes):
            return panes[idx - 1][0]
        return None
    except ValueError:
        pass
    target = arg
    if ":" not in target:
        target += ":dev1"
    if target in MONITORED_PANES:
        return target
    return None


def _fmt_ago(ts: float) -> str:
    """Format a timestamp as 'Xm ago' or 'Xh ago'."""
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta}s"
    elif delta < 3600:
        return f"{delta // 60}m"
    else:
        h = delta // 3600
        m = (delta % 3600) // 60
        return f"{h}h{m}m" if m else f"{h}h"


def diff_new_lines(prev_norm: list[str], cur_norm: list[str], cur_content: list[str]) -> list[str]:
    """Return genuinely new lines between two normalized captures."""
    prev_norm_set = set(prev_norm)
    sm = SequenceMatcher(None, prev_norm, cur_norm, autojunk=False)
    new_lines = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            for idx in range(j1, j2):
                if cur_norm[idx] not in prev_norm_set:
                    new_lines.append(cur_content[idx])
    return new_lines
