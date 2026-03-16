#!/usr/bin/env python3
"""Telegram bot that monitors Claude Code tmux sessions."""

import asyncio
import html
import logging
import os
import time
from difflib import SequenceMatcher

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from tmux_claude_lib import (
    State, STATE_ICONS, MONITORED_PANES, MONITOR_WINDOWS,
    tmux_sessions, tmux_windows, tmux_capture, tmux_send,
    detect_state, _prev_captures, _prev_states, _idle_since,
    _normalize_line, _strip_status, diff_new_lines,
    discover_panes, _short_prefixes, refresh_monitored,
    get_claude_panes, resolve_pane, _fmt_ago,
)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
POLL_INTERVAL = 15  # seconds
ATTACH_INTERVAL = 5  # seconds between attached pane updates

# Attached pane target and previous normalized content (for diffing new output)
_attached: dict = {"target": None, "prev_norm": [], "scroll_offset": 200}
# Per-pane: normalized content snapshot at time of last detach
_last_seen: dict[str, list[str]] = {}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)


# ── auth decorator ────────────────────────────────────────────────────────

def auth(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != ALLOWED_CHAT_ID:
            await update.message.reply_text("Unauthorized.")
            return
        return await func(update, context)
    return wrapper


# ── telegram command handlers ─────────────────────────────────────────────

JUST_FINISHED_THRESHOLD = 30 * 60  # 30 minutes


async def _build_status(do_diff: bool = True) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build status text + keyboard. If do_diff, takes two snapshots with delay."""
    refresh_monitored()
    claude_targets = []
    for target in sorted(MONITORED_PANES):
        text = tmux_capture(target, lines=30)
        if text.strip() and "bypass permissions on" in text:
            claude_targets.append(target)
            _prev_captures[target] = text
    if do_diff:
        await asyncio.sleep(3)

    running = []
    plan = []
    just_done = []
    long_idle = []
    now = time.time()

    for idx, target in enumerate(claude_targets, 1):
        label = MONITORED_PANES[target]
        state = detect_state(target)
        entry = (idx, target, label, state)
        if state == State.RUNNING:
            running.append(entry)
        elif state == State.PLAN:
            plan.append(entry)
        elif state in (State.IDLE, State.ERROR):
            idle_ts = _idle_since.get(target)
            if not idle_ts:
                long_idle.append(entry)
            elif (now - idle_ts) > JUST_FINISHED_THRESHOLD:
                long_idle.append(entry)
            else:
                just_done.append(entry)
        else:
            long_idle.append(entry)

    if not claude_targets:
        return "No Claude Code panes open.", None

    buttons = []

    def add_group(header, entries):
        if not entries:
            return
        buttons.append([InlineKeyboardButton(f"── {header} ──", callback_data="noop")])
        for idx, target, label, state in entries:
            icon = STATE_ICONS[state]
            ago = ""
            idle_ts = _idle_since.get(target)
            if idle_ts and state != State.RUNNING:
                ago = f" {_fmt_ago(idle_ts)} ago"
            name = f"{icon}{idx} {label}{ago}"
            if state == State.PLAN:
                buttons.append([
                    InlineKeyboardButton(name, callback_data=f"plan:{target}:peek"),
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(name, callback_data=f"peek:{target}"),
                ])

    def add_idle_group(header, entries):
        if not entries:
            return
        buttons.append([InlineKeyboardButton(f"── {header} ──", callback_data="noop")])
        from collections import OrderedDict
        by_session: dict[str, list] = OrderedDict()
        for idx, target, label, state in entries:
            session = target.split(":")[0]
            by_session.setdefault(session, []).append((idx, target, label, state))
        for session, panes_in_session in by_session.items():
            short_label = MONITORED_PANES.get(panes_in_session[0][1], session).split()[0]
            row = []
            for idx, target, label, state in panes_in_session:
                win = target.split(":")[1]
                w = win.replace("dev", "d").replace("review", "r")
                row.append(InlineKeyboardButton(f"{idx}·{w}", callback_data=f"peek:{target}"))
            row.insert(0, InlineKeyboardButton(short_label, callback_data="noop"))
            buttons.append(row)

    add_group("\u2699\ufe0f Running", running)
    add_group("\U0001f4cb Plan", plan)
    add_group("\u2705 Done", just_done)
    add_idle_group("\U0001f4a4 Idle", long_idle)

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    return "Status:", keyboard


@auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = await _build_status(do_diff=True)
    await update.message.reply_text(text, reply_markup=keyboard)


@auth
async def cmd_peek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /peek <id or session:window>")
        return
    target = resolve_pane(args[0])
    if not target:
        await update.message.reply_text("Pane not found. Use /status to see IDs.")
        return
    output = tmux_capture(target, lines=30)
    if not output.strip():
        await update.message.reply_text(f"Empty output from {target}")
        return
    truncated = output[-3500:]
    await update.message.reply_text(
        f"<b>{html.escape(target)}</b>\n<pre>{html.escape(truncated)}</pre>",
        parse_mode="HTML",
    )


@auth
async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /send <id or session:window> text...")
        return
    target = resolve_pane(args[0])
    if not target:
        await update.message.reply_text("Pane not found. Use /status to see IDs.")
        return
    keys = " ".join(args[1:])
    ok = tmux_send(target, keys)
    await update.message.reply_text(
        f"{'Sent' if ok else 'Failed'}: {html.escape(keys)} \u2192 {html.escape(target)}"
    )


@auth
async def cmd_attach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /attach <id or session:window>\n/detach to stop")
        return
    target = resolve_pane(args[0])
    if not target:
        await update.message.reply_text("Pane not found. Use /status to see IDs.")
        return
    _attached["target"] = target
    _attached["scroll_offset"] = 200
    output = tmux_capture(target, lines=200)
    content = _strip_status(output.splitlines())
    cur_norm = [_normalize_line(l) for l in content]
    _attached["prev_norm"] = cur_norm

    # Find new content since last detach (or last /detach)
    last_seen = _last_seen.get(target)
    if last_seen and len(cur_norm) > len(last_seen) // 3:
        # Only diff if content wasn't wiped (e.g. by /clear or /compact in Claude)
        prev_set = set(last_seen)
        sm = SequenceMatcher(None, last_seen, cur_norm, autojunk=False)
        new_lines = []
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag in ("insert", "replace"):
                for idx in range(j1, j2):
                    if cur_norm[idx] not in prev_set:
                        new_lines.append(content[idx])
        if new_lines:
            text = "\n".join(new_lines).strip()
        else:
            text = "(no new output since last visit)"
    else:
        # First attach, or content was cleared/compacted — show all content
        text = "\n".join(content).strip()

    detach_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u274c Detach", callback_data=f"detach:{target}"),
        InlineKeyboardButton("\u2b06 Up", callback_data=f"up:{target}"),
    ]])
    header = f"\U0001f4ce Attached to <b>{html.escape(target)}</b>"
    await update.message.reply_text(header, parse_mode="HTML")
    if text and text != "(no new output since last visit)":
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)]
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            await update.message.reply_text(
                f"<pre>{html.escape(chunk)}</pre>",
                parse_mode="HTML",
                reply_markup=detach_kb if is_last else None,
            )
    else:
        await update.message.reply_text(
            text if text == "(no new output since last visit)" else "\U0001f4ce Streaming...",
            reply_markup=detach_kb,
        )


@auth
async def cmd_detach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _attached["target"]:
        target = _attached["target"]
        output = tmux_capture(target, lines=200)
        content = _strip_status(output.splitlines())
        _last_seen[target] = [_normalize_line(l) for l in content]
        _attached["target"] = None
        _attached["prev_norm"] = []
        await update.message.reply_text(f"Detached from {html.escape(target)}")
        status_text, status_kb = await _build_status(do_diff=False)
        await update.message.reply_text(status_text, reply_markup=status_kb)
    else:
        await update.message.reply_text("Not attached to any pane.")




@auth
async def cmd_up(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = _attached.get("target")
    if not target:
        await update.message.reply_text("Not attached. Use /attach <id> first.")
        return
    offset = _attached.get("scroll_offset", 200)
    new_offset = offset + 200
    # Capture deeper history
    output_old = tmux_capture(target, lines=new_offset)
    output_cur = tmux_capture(target, lines=offset)
    old_lines = _strip_status(output_old.splitlines())
    cur_lines = _strip_status(output_cur.splitlines())
    # The older chunk is everything in old that's not in cur
    older = old_lines[:len(old_lines) - len(cur_lines)] if len(old_lines) > len(cur_lines) else []
    _attached["scroll_offset"] = new_offset
    if not older:
        await update.message.reply_text("No more history available.")
        return
    text = "\n".join(older).strip()
    text = text[-3500:] if text else "(empty)"
    await update.message.reply_text(
        f"<b>\u2b06 {html.escape(target)} (older)</b>\n<pre>{html.escape(text)}</pre>",
        parse_mode="HTML",
    )


@auth
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    for session in tmux_sessions():
        windows = tmux_windows(session)
        lines.append(f"<b>{html.escape(session)}</b>: {', '.join(windows)}")
    text = "\n".join(lines) if lines else "No tmux sessions."
    await update.message.reply_text(text, parse_mode="HTML")


@auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/status \u2014 Claude Code panes with IDs\n"
        "/peek <id> \u2014 last 30 lines\n"
        "/attach <id> \u2014 live stream (shows new since last visit)\n"
        "/up \u2014 scroll up for older output\n"
        "/detach \u2014 stop live stream\n"
        "/send <id> text \u2014 send keystrokes\n"
        "/list \u2014 all tmux sessions/windows\n"
        "/refresh \u2014 re-discover panes"
    )


@auth
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    refresh_monitored()
    await update.message.reply_text(f"Monitoring {len(MONITORED_PANES)} panes.")


@auth
async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward unknown /commands (like /compact, /clear) to attached pane."""
    target = _attached.get("target")
    if not target:
        await update.message.reply_text(f"Unknown command: {update.message.text}")
        return
    # Forward the whole message (including /) to the pane
    return await _send_to_pane(update, target, update.message.text)


async def _send_to_pane(update: Update, target: str, keys: str):
    """Send text to a pane with busy detection and feedback."""
    pane_text = tmux_capture(target, lines=10)
    is_busy = "esc to inter" in pane_text
    ok = tmux_send(target, keys)
    label = MONITORED_PANES.get(target, target)
    if ok and is_busy:
        await update.message.reply_text(
            f"\u23f3 {html.escape(label)} (queued, Claude is busy)"
        )
    elif ok:
        await update.message.reply_text(f"> {html.escape(label)}")
    else:
        await update.message.reply_text(f"Failed: {html.escape(label)}")


@auth
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When attached, plain text messages are sent directly to the pane."""
    target = _attached.get("target")
    if not target:
        await update.message.reply_text("Not attached to any pane. Use /attach <id>")
        return
    return await _send_to_pane(update, target, update.message.text)


# ── polling loop ──────────────────────────────────────────────────────────

async def poll_panes(context: ContextTypes.DEFAULT_TYPE):
    global _prev_states
    refresh_monitored()
    # Build ID map (same order as /status)
    panes = get_claude_panes()
    id_map = {target: idx for idx, (target, _label, _state) in enumerate(panes, 1)}

    for target, label in MONITORED_PANES.items():
        state = detect_state(target)
        prev = _prev_states.get(target)
        _prev_states[target] = state

        if prev is None:
            _prev_states[target] = state
            continue  # first run, just record state — don't set idle time

        # Track when pane transitioned from RUNNING to non-running
        if state == State.RUNNING:
            _idle_since.pop(target, None)
        elif prev == State.RUNNING and target not in _idle_since:
            _idle_since[target] = time.time()

        pane_id = id_map.get(target, "?")

        # Notify on transitions
        if prev == State.RUNNING and state == State.IDLE:
            icon = STATE_ICONS[State.IDLE]
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("\U0001f440 Peek", callback_data=f"peek:{target}"),
                InlineKeyboardButton("\U0001f4ce Attach", callback_data=f"attach:{target}"),
            ]])
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"{icon} <b>[{pane_id}] {html.escape(label)}</b> finished!",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        elif prev == State.RUNNING and state == State.PLAN:
            icon = STATE_ICONS[State.PLAN]
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1. Clear+Bypass", callback_data=f"plan:{target}:1"),
                    InlineKeyboardButton("2. Bypass", callback_data=f"plan:{target}:2"),
                ],
                [
                    InlineKeyboardButton("3. Manual", callback_data=f"plan:{target}:3"),
                    InlineKeyboardButton("\U0001f440 Peek", callback_data=f"plan:{target}:peek"),
                ],
            ])
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"{icon} <b>[{pane_id}] {html.escape(label)}</b> plan ready",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        elif prev == State.RUNNING and state == State.ERROR:
            icon = STATE_ICONS[State.ERROR]
            tail = tmux_capture(target, lines=15).strip()[-500:]
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("\U0001f440 Peek", callback_data=f"peek:{target}"),
                InlineKeyboardButton("\U0001f4ce Attach", callback_data=f"attach:{target}"),
            ]])
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=(
                    f"{icon} <b>[{pane_id}] {html.escape(label)}</b> errored!\n"
                    f"<pre>{html.escape(tail)}</pre>"
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )


async def poll_attached(context: ContextTypes.DEFAULT_TYPE):
    """Send only genuinely new lines using diff. Works like a chat stream."""
    target = _attached.get("target")
    if not target:
        return
    output = tmux_capture(target, lines=200)
    raw_lines = output.splitlines()

    cur_content = _strip_status(raw_lines)
    cur_norm = [_normalize_line(l) for l in cur_content]
    prev_norm = _attached.get("prev_norm", [])

    _attached["prev_norm"] = cur_norm

    if not prev_norm:
        return  # first capture, baseline

    if cur_norm == prev_norm:
        return  # nothing changed (timer-only changes normalized away)

    # Use SequenceMatcher to find only inserted/new lines
    # Build set of prev normalized lines to filter out reflows
    # (same line moving position shows as delete+insert, not truly new)
    prev_norm_set = set(prev_norm)
    sm = SequenceMatcher(None, prev_norm, cur_norm, autojunk=False)
    new_lines = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            for idx in range(j1, j2):
                # Only include if this normalized line didn't exist anywhere in prev
                if cur_norm[idx] not in prev_norm_set:
                    new_lines.append(cur_content[idx])

    if not new_lines:
        return

    text = "\n".join(new_lines).strip()
    if not text:
        return

    text = text[-3500:]
    target = _attached.get("target")
    detach_kb = None
    if target:
        detach_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u274c Detach", callback_data=f"detach:{target}"),
            InlineKeyboardButton("\u2b06 Up", callback_data=f"up:{target}"),
        ]])
    await context.bot.send_message(
        chat_id=ALLOWED_CHAT_ID,
        text=f"<pre>{html.escape(text)}</pre>",
        parse_mode="HTML",
        reply_markup=detach_kb,
    )


# ── plan approval callback ───────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_CHAT_ID:
        await query.answer("Unauthorized.")
        return
    data = query.data
    log.info("Callback: %s", data)
    if data == "noop":
        await query.answer()
        return
    # Split only on first colon: "action:rest"
    action, _, rest = data.partition(":")

    # ── peek:<target> ──
    if action == "peek" and rest:
        target = rest
        label = MONITORED_PANES.get(target, target)
        output = tmux_capture(target, lines=100)
        truncated = output[-3500:]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"\U0001f4ce Attach {label}", callback_data=f"attach:{target}"),
        ]])
        await query.answer()
        await query.message.reply_text(
            f"<pre>{html.escape(truncated)}</pre>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    # ── attach:<target> ──
    if action == "attach" and rest:
        target = rest
        label = MONITORED_PANES.get(target, target)
        _attached["target"] = target
        _attached["scroll_offset"] = 200
        output = tmux_capture(target, lines=200)
        content = _strip_status(output.splitlines())
        cur_norm = [_normalize_line(l) for l in content]
        _attached["prev_norm"] = cur_norm

        last_seen = _last_seen.get(target)
        if last_seen and len(cur_norm) > len(last_seen) // 3:
            prev_set = set(last_seen)
            sm = SequenceMatcher(None, last_seen, cur_norm, autojunk=False)
            new_lines = []
            for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
                if tag in ("insert", "replace"):
                    for idx in range(j1, j2):
                        if cur_norm[idx] not in prev_set:
                            new_lines.append(content[idx])
            text = "\n".join(new_lines).strip() if new_lines else "(no new output since last visit)"
        else:
            text = "\n".join(content).strip()

        detach_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u274c Detach", callback_data=f"detach:{target}"),
            InlineKeyboardButton("\u2b06 Up", callback_data=f"up:{target}"),
        ]])
        await query.answer(f"Attached to {label}")
        await query.message.reply_text(
            f"\U0001f4ce Attached to <b>{html.escape(target)}</b>",
            parse_mode="HTML",
        )
        if text and text != "(no new output since last visit)":
            chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)]
            for i, chunk in enumerate(chunks):
                is_last = (i == len(chunks) - 1)
                await query.message.reply_text(
                    f"<pre>{html.escape(chunk)}</pre>",
                    parse_mode="HTML",
                    reply_markup=detach_kb if is_last else None,
                )
        else:
            await query.message.reply_text(
                text if text == "(no new output since last visit)" else "\U0001f4ce Streaming...",
                reply_markup=detach_kb,
            )
        return

    # ── plan:<target>:<choice> ──
    if action == "plan" and rest:
        # rest = "session:window:choice" — split on last colon
        target, _, choice = rest.rpartition(":")
        if not target or not choice:
            await query.answer("Bad plan data.")
            return
        label = MONITORED_PANES.get(target, target)

        if choice == "peek":
            output = tmux_capture(target, lines=100)
            truncated = output[-3500:]
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1·Clear+Bypass", callback_data=f"plan:{target}:1"),
                    InlineKeyboardButton("2·Bypass", callback_data=f"plan:{target}:2"),
                ],
                [
                    InlineKeyboardButton("3·Manual", callback_data=f"plan:{target}:3"),
                    InlineKeyboardButton("\U0001f4ce Attach", callback_data=f"attach:{target}"),
                ],
            ])
            await query.answer()
            await query.message.reply_text(
                f"<pre>{html.escape(truncated)}</pre>",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return

        ok = tmux_send(target, choice)
        if ok:
            labels = {"1": "Clear+Bypass", "2": "Bypass", "3": "Manual"}
            await query.answer(f"Sent option {choice} to {label}")
            await query.edit_message_text(
                f"\u2705 <b>{html.escape(label)}</b> — approved ({labels.get(choice, choice)})",
                parse_mode="HTML",
            )
        else:
            await query.answer(f"Failed to send to {label}")
        return

    # ── detach:<target> ──
    if action == "detach" and rest:
        target = rest
        label = MONITORED_PANES.get(target, target)
        if _attached.get("target") == target:
            output = tmux_capture(target, lines=200)
            content = _strip_status(output.splitlines())
            _last_seen[target] = [_normalize_line(l) for l in content]
            _attached["target"] = None
            _attached["prev_norm"] = []
        await query.answer(f"Detached from {label}")
        await query.edit_message_text(f"Detached from {html.escape(label)}")
        status_text, status_kb = await _build_status(do_diff=False)
        await query.message.reply_text(status_text, reply_markup=status_kb)
        return

    # ── up:<target> ──
    if action == "up" and rest:
        target = rest
        if _attached.get("target") != target:
            await query.answer("Not attached to this pane.")
            return
        offset = _attached.get("scroll_offset", 200)
        new_offset = offset + 200
        output_old = tmux_capture(target, lines=new_offset)
        output_cur = tmux_capture(target, lines=offset)
        old_lines = _strip_status(output_old.splitlines())
        cur_lines = _strip_status(output_cur.splitlines())
        older = old_lines[:len(old_lines) - len(cur_lines)] if len(old_lines) > len(cur_lines) else []
        _attached["scroll_offset"] = new_offset
        if not older:
            await query.answer("No more history.")
            return
        text = "\n".join(older).strip()[-3500:]
        detach_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u274c Detach", callback_data=f"detach:{target}"),
            InlineKeyboardButton("\u2b06 Up", callback_data=f"up:{target}"),
        ]])
        await query.answer()
        await query.message.reply_text(
            f"<pre>{html.escape(text)}</pre>",
            parse_mode="HTML",
            reply_markup=detach_kb,
        )
        return

    await query.answer("Unknown action.")


# ── main ──────────────────────────────────────────────────────────────────

def main():
    refresh_monitored()
    log.info("Monitoring %d panes", len(MONITORED_PANES))

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("peek", cmd_peek))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(CommandHandler("attach", cmd_attach))
    app.add_handler(CommandHandler("up", cmd_up))
    app.add_handler(CommandHandler("detach", cmd_detach))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    app.job_queue.run_repeating(poll_panes, interval=POLL_INTERVAL, first=5)
    app.job_queue.run_repeating(poll_attached, interval=ATTACH_INTERVAL, first=ATTACH_INTERVAL)

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
