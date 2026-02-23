"""Factory Control Bot — main entry point with all Telegram handlers."""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import auth, config, factory, state, voice

log = logging.getLogger(__name__)

# Conversation states for /new project wizard
(
    ST_ENGINE_SELECT,
    ST_NAME_INPUT,
    ST_REQUIREMENTS_INPUT,
    ST_TRANSLATION_REVIEW,
    ST_CONFIRM,
) = range(5)

# Active log monitors: {(project, engine): LogMonitor}
_monitors: dict[tuple[str, str], factory.LogMonitor] = {}

# Persistent reply keyboard
REPLY_KB = ReplyKeyboardMarkup(
    [["New Project", "Projects"], ["Settings", "Health"]],
    resize_keyboard=True,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return await auth.auth_check(update, context)


def _engines_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    """Build engine multi-select inline keyboard."""
    buttons = []
    for key, eng in factory.ENGINES.items():
        check = "V " if key in selected else "  "
        buttons.append([InlineKeyboardButton(
            f"{check}{eng['name']}", callback_data=f"eng:{key}"
        )])
    buttons.append([InlineKeyboardButton("Confirm", callback_data="eng:confirm")])
    return InlineKeyboardMarkup(buttons)


# ─── /start and main menu ────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    text = (
        "Black Box Factory v2\n\n"
        "Commands:\n"
        "/new - Create a new project\n"
        "/projects - List projects\n"
        "/settings - Bot settings\n"
        "/engines - Engine status\n"
        "/health - System health\n"
        "/admin - User management\n"
        "/help - Show help"
    )
    await update.message.reply_text(text, reply_markup=REPLY_KB)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    text = (
        "Factory Bot Commands\n\n"
        "/new - Start project creation wizard\n"
        "/projects - List all projects\n"
        "/factory <name> - Start factory run for a project\n"
        "/status <name> - Show active run status\n"
        "/stop <name> - Stop a running factory\n"
        "/logs <name> - View last 50 lines of factory log\n"
        "/deploy <name> - Deploy a completed project\n"
        "/settings - Configure STT/TTS/engines\n"
        "/engines - Check installed engine versions\n"
        "/health - System health (CPU, RAM, disk)\n"
        "/admin add|remove|list - Manage users (admin)\n"
        "/cancel - Cancel current wizard"
    )
    await update.message.reply_text(text)


# ─── /new — Project creation wizard ──────────────────────────────────────────

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return ConversationHandler.END
    context.user_data["selected_engines"] = set()
    context.user_data["voice_segments"] = []
    await update.message.reply_text(
        "Select engines for this project (tap to toggle, then Confirm):",
        reply_markup=_engines_keyboard(set()),
    )
    return ST_ENGINE_SELECT


async def engine_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "eng:confirm":
        selected = context.user_data.get("selected_engines", set())
        if not selected:
            await query.edit_message_text(
                "Select at least one engine:",
                reply_markup=_engines_keyboard(selected),
            )
            return ST_ENGINE_SELECT
        engines_text = ", ".join(
            factory.ENGINES[e]["name"] for e in selected
        )
        await query.edit_message_text(
            f"Engines: {engines_text}\n\nNow enter a project name (lowercase, hyphens only):"
        )
        return ST_NAME_INPUT

    engine = data.split(":", 1)[1]
    selected = context.user_data.get("selected_engines", set())
    if engine in selected:
        selected.discard(engine)
    else:
        selected.add(engine)
    context.user_data["selected_engines"] = selected
    await query.edit_message_text(
        "Select engines (tap to toggle, then Confirm):",
        reply_markup=_engines_keyboard(selected),
    )
    return ST_ENGINE_SELECT


async def name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip().lower()
    # Validate name
    import re
    if not re.match(r"^[a-z][a-z0-9-]*[a-z0-9]$", name) or len(name) < 3:
        await update.message.reply_text(
            "Invalid name. Use lowercase letters, numbers, hyphens. Min 3 chars. Try again:"
        )
        return ST_NAME_INPUT

    projects = state.load_projects()
    if name in projects:
        await update.message.reply_text("Project name already exists. Choose another:")
        return ST_NAME_INPUT

    context.user_data["project_name"] = name
    context.user_data["voice_segments"] = []
    await update.message.reply_text(
        f"Project: {name}\n\n"
        "Now describe your requirements.\n"
        "Send voice messages (Hebrew/English) or text.\n"
        "Press Done when finished.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Done", callback_data="req:done"),
            InlineKeyboardButton("Clear All", callback_data="req:clear"),
        ]]),
    )
    return ST_REQUIREMENTS_INPUT


async def requirements_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice message during requirements gathering."""
    await update.message.chat.send_action(ChatAction.TYPING)

    # Download voice file
    voice_file = await update.message.voice.get_file()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        ogg_path = tmp.name
    await voice_file.download_to_drive(ogg_path)

    # Convert and transcribe
    try:
        wav_path = voice.ogg_to_wav(ogg_path)
        text = await voice.transcribe(wav_path)
    finally:
        Path(ogg_path).unlink(missing_ok=True)
        Path(ogg_path.rsplit(".", 1)[0] + ".wav").unlink(missing_ok=True)

    segments = context.user_data.get("voice_segments", [])
    segments.append(text)
    context.user_data["voice_segments"] = segments

    segment_count = len(segments)
    await update.message.reply_text(
        f"Segment {segment_count}: {text}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Done", callback_data="req:done"),
            InlineKeyboardButton("Delete Last", callback_data="req:del_last"),
            InlineKeyboardButton("Clear All", callback_data="req:clear"),
        ]]),
    )
    return ST_REQUIREMENTS_INPUT


async def requirements_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text message during requirements gathering."""
    text = update.message.text.strip()
    if not text:
        return ST_REQUIREMENTS_INPUT

    segments = context.user_data.get("voice_segments", [])
    segments.append(text)
    context.user_data["voice_segments"] = segments

    segment_count = len(segments)
    await update.message.reply_text(
        f"Segment {segment_count}: {text}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Done", callback_data="req:done"),
            InlineKeyboardButton("Delete Last", callback_data="req:del_last"),
            InlineKeyboardButton("Clear All", callback_data="req:clear"),
        ]]),
    )
    return ST_REQUIREMENTS_INPUT


async def requirements_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline buttons during requirements gathering."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    segments = context.user_data.get("voice_segments", [])

    if action == "del_last":
        if segments:
            removed = segments.pop()
            context.user_data["voice_segments"] = segments
            await query.edit_message_text(
                f"Removed: {removed[:50]}...\n{len(segments)} segments remaining.\n\nContinue adding or press Done.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Done", callback_data="req:done"),
                    InlineKeyboardButton("Clear All", callback_data="req:clear"),
                ]]),
            )
        return ST_REQUIREMENTS_INPUT

    if action == "clear":
        context.user_data["voice_segments"] = []
        await query.edit_message_text(
            "All segments cleared. Send voice or text to start over.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Done", callback_data="req:done"),
            ]]),
        )
        return ST_REQUIREMENTS_INPUT

    if action == "done":
        if not segments:
            await query.edit_message_text(
                "No input received. Send voice or text first.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Done", callback_data="req:done"),
                ]]),
            )
            return ST_REQUIREMENTS_INPUT

        full_hebrew = "\n\n".join(segments)
        context.user_data["hebrew_text"] = full_hebrew

        await query.edit_message_text("Translating to English...")

        # Translate Hebrew → English
        english_text = await voice.translate_to_english(full_hebrew)
        context.user_data["requirements_text"] = english_text

        await query.edit_message_text(
            f"Hebrew input:\n{full_hebrew[:500]}\n\n"
            f"English translation:\n{english_text}\n\n"
            "Is the English translation correct?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Approve", callback_data="trans:approve"),
                InlineKeyboardButton("Re-translate", callback_data="trans:retry"),
                InlineKeyboardButton("Edit", callback_data="trans:edit"),
            ]]),
        )
        return ST_TRANSLATION_REVIEW

    return ST_REQUIREMENTS_INPUT


async def translation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle translation review buttons."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "approve":
        # English translation approved — proceed to confirmation
        english_text = context.user_data["requirements_text"]
        engines = ", ".join(
            factory.ENGINES[e]["name"]
            for e in context.user_data["selected_engines"]
        )
        name = context.user_data["project_name"]

        await query.edit_message_text(
            f"Project: {name}\n"
            f"Engines: {engines}\n\n"
            f"Requirements (English):\n{english_text}\n\n"
            "Confirm to start the factory?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Start Factory", callback_data="confirm:yes"),
                InlineKeyboardButton("Cancel", callback_data="confirm:no"),
            ]]),
        )
        return ST_CONFIRM

    if action == "retry":
        # Re-translate with fresh API call
        hebrew_text = context.user_data.get("hebrew_text", "")
        await query.edit_message_text("Re-translating...")

        english_text = await voice.translate_to_english(hebrew_text)
        context.user_data["requirements_text"] = english_text

        await query.edit_message_text(
            f"New translation:\n{english_text}\n\n"
            "Is this correct?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Approve", callback_data="trans:approve"),
                InlineKeyboardButton("Re-translate", callback_data="trans:retry"),
                InlineKeyboardButton("Edit", callback_data="trans:edit"),
            ]]),
        )
        return ST_TRANSLATION_REVIEW

    if action == "edit":
        await query.edit_message_text(
            "Send the corrected English text:"
        )
        return ST_TRANSLATION_REVIEW

    return ST_TRANSLATION_REVIEW


async def translation_text_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle manual English text correction during translation review."""
    corrected = update.message.text.strip()
    if not corrected:
        await update.message.reply_text("Please send the corrected English text:")
        return ST_TRANSLATION_REVIEW

    context.user_data["requirements_text"] = corrected

    engines = ", ".join(
        factory.ENGINES[e]["name"]
        for e in context.user_data["selected_engines"]
    )
    name = context.user_data["project_name"]

    await update.message.reply_text(
        f"Project: {name}\n"
        f"Engines: {engines}\n\n"
        f"Requirements (English):\n{corrected}\n\n"
        "Confirm to start the factory?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Start Factory", callback_data="confirm:yes"),
            InlineKeyboardButton("Cancel", callback_data="confirm:no"),
        ]]),
    )
    return ST_CONFIRM


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle final confirmation."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "no":
        await query.edit_message_text("Project creation cancelled.")
        return ConversationHandler.END

    # Create the project and start factory runs
    name = context.user_data["project_name"]
    engines = context.user_data["selected_engines"]
    requirements = context.user_data["requirements_text"]
    user_id = update.effective_user.id

    state.create_project(
        name=name,
        engines=list(engines),
        description=requirements[:200],
        requirements=requirements,
        created_by=user_id,
    )

    started = []
    for engine in engines:
        factory.setup_project(name, engine, requirements)
        session = factory.start_engine(name, engine)

        # Start log monitor
        monitor = factory.LogMonitor(
            name, engine,
            on_event=lambda evt, uid=user_id: _handle_factory_event(evt, uid, context.application),
        )
        monitor.start()
        _monitors[(name, engine)] = monitor
        started.append(f"{factory.ENGINES[engine]['name']} (session: {session})")

    await query.edit_message_text(
        f"Project '{name}' created and factory started!\n\n"
        + "\n".join(f"- {s}" for s in started)
        + "\n\nUse /status " + name + " to check progress."
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=REPLY_KB)
    return ConversationHandler.END


# ─── Factory event handler (called from LogMonitor) ──────────────────────────

async def _handle_factory_event(event: dict, user_id: int, app):
    """Send Telegram notification for factory events."""
    engine_name = factory.ENGINES.get(event.get("engine", ""), {}).get("name", event.get("engine", ""))
    project = event.get("project", "?")

    if event["type"] == "phase":
        action = event["action"]
        phase = event["phase"]
        score = event.get("score")
        if action == "start":
            text = f"[{project}/{engine_name}] Phase {phase} started"
        elif action == "end":
            text = f"[{project}/{engine_name}] Phase {phase} completed (score: {score})"
        else:
            return
    elif event["type"] == "error":
        text = f"[{project}/{engine_name}] ERROR: {event['message']}"
    elif event["type"] == "complete":
        data = event.get("data", {})
        text = (
            f"[{project}/{engine_name}] FACTORY COMPLETE!\n"
            f"Duration: {data.get('duration_minutes', '?')} min\n"
            f"Cost: ${data.get('total_cost', '?')}\n"
            f"Tests: {data.get('test_results', {})}"
        )
    elif event["type"] == "cost":
        return  # Don't spam cost updates
    elif event["type"] == "session_died":
        text = f"[{project}/{engine_name}] Session died unexpectedly! Use /logs {project} to check."
    elif event["type"] == "clarify":
        data = event.get("data", {})
        text = (
            f"[{project}/{engine_name}] Clarification needed:\n\n"
            f"{data.get('question', '?')}"
        )
        # TODO: add inline buttons for multiple choice answers
    else:
        return

    try:
        await app.bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        log.error("Failed to send notification to %s: %s", user_id, e)


# ─── /projects — List projects ───────────────────────────────────────────────

async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    projects = state.load_projects()
    if not projects:
        await update.message.reply_text("No projects yet. Use /new to create one.")
        return

    lines = []
    for name, proj in projects.items():
        engines = ", ".join(proj.get("engines", []))
        status = proj.get("status", "unknown")
        lines.append(f"- {name} [{status}] ({engines})")

    text = "Projects:\n\n" + "\n".join(lines)
    text += "\n\nUse /status <name> for details."
    await update.message.reply_text(text)


# ─── /factory — Start factory run for existing project ───────────────────────

async def cmd_factory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /factory <project-name>")
        return

    name = args[0]
    projects = state.load_projects()
    if name not in projects:
        await update.message.reply_text(f"Project '{name}' not found. Use /projects to list.")
        return

    proj = projects[name]
    engines = proj.get("engines", ["claude"])
    user_id = update.effective_user.id

    started = []
    for engine in engines:
        requirements = proj.get("requirements", proj.get("description", ""))
        proj_dir = factory._project_dir(name, engine)
        if not proj_dir.exists():
            factory.setup_project(name, engine, requirements)
        session = factory.start_engine(name, engine)
        monitor = factory.LogMonitor(
            name, engine,
            on_event=lambda evt, uid=user_id: _handle_factory_event(evt, uid, context.application),
        )
        monitor.start()
        _monitors[(name, engine)] = monitor
        started.append(factory.ENGINES[engine]["name"])

    await update.message.reply_text(
        f"Factory started for '{name}':\n" + "\n".join(f"- {s}" for s in started)
    )


# ─── /status — Show run status ───────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    args = context.args
    if not args:
        # Show all active runs
        sessions = factory.list_active_sessions()
        if not sessions:
            await update.message.reply_text("No active factory runs.")
            return
        await update.message.reply_text(
            "Active sessions:\n" + "\n".join(f"- {s}" for s in sessions)
        )
        return

    name = args[0]
    projects = state.load_projects()
    if name not in projects:
        await update.message.reply_text(f"Project '{name}' not found.")
        return

    proj = projects[name]
    lines = [f"Project: {name}", f"Status: {proj.get('status', 'unknown')}"]

    for engine in proj.get("engines", []):
        session = factory._tmux_session_name(name, engine)
        alive = factory.is_session_alive(session)
        output = factory.get_session_output(session, 10) if alive else "(not running)"
        lines.append(f"\n{factory.ENGINES[engine]['name']}: {'running' if alive else 'stopped'}")
        if alive and output:
            lines.append(f"```\n{output[-500:]}\n```")

    await update.message.reply_text("\n".join(lines))


# ─── /stop — Stop factory run ────────────────────────────────────────────────

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /stop <project-name>")
        return

    name = args[0]
    projects = state.load_projects()
    if name not in projects:
        await update.message.reply_text(f"Project '{name}' not found.")
        return

    stopped = []
    for engine in projects[name].get("engines", []):
        # Stop monitor
        key = (name, engine)
        if key in _monitors:
            _monitors[key].stop()
            del _monitors[key]
        factory.stop_engine(name, engine)
        stopped.append(factory.ENGINES[engine]["name"])

    await update.message.reply_text(
        f"Stopped: {', '.join(stopped)}" if stopped else "Nothing to stop."
    )


# ─── /logs — View factory log ────────────────────────────────────────────────

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /logs <project-name> [engine]")
        return

    name = args[0]
    engine = args[1] if len(args) > 1 else None
    projects = state.load_projects()
    if name not in projects:
        await update.message.reply_text(f"Project '{name}' not found.")
        return

    engines = [engine] if engine else projects[name].get("engines", [])
    for eng in engines:
        session = factory._tmux_session_name(name, eng)
        if factory.is_session_alive(session):
            output = factory.get_session_output(session, 50)
            if output:
                # Truncate to Telegram message limit
                if len(output) > 3900:
                    output = output[-3900:]
                await update.message.reply_text(
                    f"[{factory.ENGINES[eng]['name']}]\n```\n{output}\n```"
                )
            else:
                await update.message.reply_text(f"[{factory.ENGINES[eng]['name']}] No output yet.")
        else:
            # Try reading log file
            log_path = factory._log_file(name, eng)
            if log_path.exists():
                content = log_path.read_text()[-3900:]
                await update.message.reply_text(
                    f"[{factory.ENGINES[eng]['name']}] (stopped)\n```\n{content}\n```"
                )
            else:
                await update.message.reply_text(
                    f"[{factory.ENGINES[eng]['name']}] No log file found."
                )


# ─── /settings ────────────────────────────────────────────────────────────────

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    settings = state.load_settings()
    buttons = [
        [InlineKeyboardButton(
            f"STT: {settings['stt_provider']}", callback_data="set:stt_provider"
        )],
        [InlineKeyboardButton(
            f"TTS: {settings['tts_provider']}", callback_data="set:tts_provider"
        )],
        [InlineKeyboardButton(
            f"TTS Voice: {settings['tts_voice']}", callback_data="set:tts_voice"
        )],
        [InlineKeyboardButton(
            f"Default Engines: {', '.join(settings['default_engines'])}",
            callback_data="set:default_engines",
        )],
    ]
    await update.message.reply_text(
        "Settings (tap to change):", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]

    options = {
        "stt_provider": ["auto", "groq", "openai"],
        "tts_provider": ["edge", "openai"],
        "tts_voice": ["en-US-AriaNeural", "en-US-GuyNeural", "en-GB-SoniaNeural"],
        "default_engines": ["claude", "gemini", "opencode", "aider"],
    }

    if key not in options:
        return

    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"setval:{key}:{opt}")]
        for opt in options[key]
    ]
    await query.edit_message_text(
        f"Choose {key}:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def settings_value_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    key, value = parts[1], parts[2]

    if key == "default_engines":
        # Toggle in list
        settings = state.load_settings()
        engines = settings.get("default_engines", [])
        if value in engines:
            engines.remove(value)
        else:
            engines.append(value)
        if not engines:
            engines = ["claude"]
        state.update_setting("default_engines", engines)
        await query.edit_message_text(f"Default engines: {', '.join(engines)}")
    else:
        state.update_setting(key, value)
        await query.edit_message_text(f"{key} set to: {value}")


# ─── /engines — Check engine status ──────────────────────────────────────────

async def cmd_engines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    statuses = factory.check_all_engines()
    lines = []
    for name, info in statuses.items():
        eng = factory.ENGINES[name]
        if info["installed"]:
            lines.append(f"[OK] {eng['name']}: {info['version']}")
        else:
            lines.append(f"[--] {eng['name']}: not installed")
    await update.message.reply_text("Engine Status:\n\n" + "\n".join(lines))


# ─── /health — System health ─────────────────────────────────────────────────

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context):
        return
    health = factory.system_health()
    if "error" in health:
        await update.message.reply_text(f"Health check error: {health['error']}")
        return

    text = (
        f"System Health\n\n"
        f"CPU: {health['cpu_percent']}%\n"
        f"RAM: {health['memory']['used_percent']}% "
        f"of {health['memory']['total_gb']} GB\n"
        f"Disk: {health['disk']['used_percent']}% "
        f"of {health['disk']['total_gb']} GB"
    )

    sessions = factory.list_active_sessions()
    if sessions:
        text += f"\n\nActive tmux sessions: {len(sessions)}"
        for s in sessions[:10]:
            text += f"\n  - {s}"

    await update.message.reply_text(text)


# ─── /admin — User management ────────────────────────────────────────────────

@auth.admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Admin commands:\n"
            "/admin list - Show all users\n"
            "/admin add <telegram_id> <name> - Add user\n"
            "/admin remove <telegram_id> - Remove user"
        )
        return

    action = args[0]

    if action == "list":
        users = state.load_users()
        if not users:
            await update.message.reply_text("No users registered.")
            return
        lines = []
        for uid, info in users.items():
            role = info.get("role", "user")
            name = info.get("name", "?")
            lines.append(f"- {uid}: {name} ({role})")
        await update.message.reply_text("Users:\n" + "\n".join(lines))

    elif action == "add" and len(args) >= 3:
        try:
            uid = int(args[1])
        except ValueError:
            await update.message.reply_text("Invalid user ID.")
            return
        name = " ".join(args[2:])
        state.add_user(uid, name, "user")
        await update.message.reply_text(f"Added user {uid} ({name}).")

    elif action == "remove" and len(args) >= 2:
        try:
            uid = int(args[1])
        except ValueError:
            await update.message.reply_text("Invalid user ID.")
            return
        if uid == config.ADMIN_TELEGRAM_ID:
            await update.message.reply_text("Cannot remove admin.")
            return
        if state.remove_user(uid):
            await update.message.reply_text(f"Removed user {uid}.")
        else:
            await update.message.reply_text(f"User {uid} not found.")

    else:
        await update.message.reply_text("Usage: /admin list|add|remove")


# ─── Voice handler (outside conversation — for general voice chat) ───────────

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages outside of the project wizard.
    Transcribes Hebrew, translates to English, sends both back."""
    if not await _authorized(update, context):
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    # Download and convert
    voice_file = await update.message.voice.get_file()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        ogg_path = tmp.name
    await voice_file.download_to_drive(ogg_path)

    try:
        wav_path = voice.ogg_to_wav(ogg_path)
        hebrew_text = await voice.transcribe(wav_path)
    finally:
        Path(ogg_path).unlink(missing_ok=True)
        Path(ogg_path.rsplit(".", 1)[0] + ".wav").unlink(missing_ok=True)

    # Translate to English
    english_text = await voice.translate_to_english(hebrew_text)

    # Send both Hebrew transcription and English translation
    await update.message.reply_text(
        f"Hebrew: {hebrew_text}\n\nEnglish: {english_text}"
    )

    # Send English text as voice (TTS)
    ogg_response = await voice.text_to_speech(english_text)
    if ogg_response:
        try:
            await update.message.reply_voice(voice=open(ogg_response, "rb"))
        finally:
            Path(ogg_response).unlink(missing_ok=True)


# ─── Text handler for reply keyboard buttons ─────────────────────────────────

async def reply_keyboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle persistent reply keyboard button presses."""
    if not await _authorized(update, context):
        return
    text = update.message.text
    if text == "New Project":
        return await cmd_new(update, context)
    elif text == "Projects":
        return await cmd_projects(update, context)
    elif text == "Settings":
        return await cmd_settings(update, context)
    elif text == "Health":
        return await cmd_health(update, context)


# ─── Build and run ───────────────────────────────────────────────────────────

def build_app() -> Application:
    """Build the Telegram bot application with all handlers."""
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Project creation wizard (ConversationHandler)
    new_project_conv = ConversationHandler(
        entry_points=[CommandHandler("new", cmd_new)],
        states={
            ST_ENGINE_SELECT: [
                CallbackQueryHandler(engine_toggle, pattern=r"^eng:"),
            ],
            ST_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, name_input),
            ],
            ST_REQUIREMENTS_INPUT: [
                MessageHandler(filters.VOICE, requirements_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, requirements_text),
                CallbackQueryHandler(requirements_callback, pattern=r"^req:"),
            ],
            ST_TRANSLATION_REVIEW: [
                CallbackQueryHandler(translation_callback, pattern=r"^trans:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, translation_text_edit),
            ],
            ST_CONFIRM: [
                CallbackQueryHandler(confirm_callback, pattern=r"^confirm:"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
    )
    app.add_handler(new_project_conv)

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("factory", cmd_factory))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("engines", cmd_engines))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Callback query handlers for settings
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^set:"))
    app.add_handler(CallbackQueryHandler(settings_value_callback, pattern=r"^setval:"))

    # Voice handler (outside conversation)
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))

    # Reply keyboard text handler
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^(New Project|Projects|Settings|Health)$"),
        reply_keyboard_handler,
    ))

    return app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    log.info("Starting Factory Control Bot...")
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
