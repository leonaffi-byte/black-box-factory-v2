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

from . import auth, auth_engines, config, factory, state, voice

log = logging.getLogger(__name__)

# Conversation states for /new project wizard
(
    ST_ENGINE_SELECT,
    ST_NAME_INPUT,
    ST_PROJECT_TYPE,
    ST_DEPLOY_ASK,
    ST_SUBDOMAIN_INPUT,
    ST_REQUIREMENTS_INPUT,
    ST_TRANSLATION_REVIEW,
    ST_CONFIRM,
) = range(8)

# Active log monitors: {(project, engine): LogMonitor}
_monitors: dict[tuple[str, str], factory.LogMonitor] = {}

# Persistent reply keyboard
REPLY_KB = ReplyKeyboardMarkup(
    [["New Project", "Projects"], ["Auth", "Engines"], ["Settings", "Health"]],
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

    # Ask project type
    await update.message.reply_text(
        f"Project: {name}\n\nWhat type of project is this?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Telegram Bot", callback_data="ptype:bot")],
            [InlineKeyboardButton("Web Service / API", callback_data="ptype:web")],
            [InlineKeyboardButton("Standalone Software", callback_data="ptype:standalone")],
        ]),
    )
    return ST_PROJECT_TYPE


def _project_type_label(ptype: str) -> str:
    return {"bot": "Telegram Bot", "web": "Web Service / API", "standalone": "Standalone Software"}.get(ptype, ptype)


async def project_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle project type selection."""
    query = update.callback_query
    await query.answer()
    ptype = query.data.split(":", 1)[1]
    context.user_data["project_type"] = ptype

    # Web services always need deployment discussion
    # Bots usually need deployment too
    # Standalone might not
    if ptype in ("web", "bot"):
        # Ask about deployment
        return await _ask_deploy(query, context)
    else:
        # Standalone — ask if they want deployment at all
        await query.edit_message_text(
            f"Type: {_project_type_label(ptype)}\n\n"
            "Does this project need deployment?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Yes, deploy with Docker", callback_data="deploy:yes")],
                [InlineKeyboardButton("No deployment needed", callback_data="deploy:no")],
            ]),
        )
        return ST_DEPLOY_ASK


async def _ask_deploy(query, context: ContextTypes.DEFAULT_TYPE):
    """Ask deployment question for bot/web projects."""
    ptype = context.user_data["project_type"]
    server = config.DEPLOY_SERVER

    if not server:
        # No deploy server configured — inform user
        await query.edit_message_text(
            f"Type: {_project_type_label(ptype)}\n\n"
            "No DEPLOY_SERVER configured in .env.\n"
            "The factory will generate Docker files and a deploy guide, "
            "but won't auto-deploy.\n\n"
            "Continue without auto-deploy?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Continue", callback_data="deploy:no")],
            ]),
        )
        return ST_DEPLOY_ASK

    await query.edit_message_text(
        f"Type: {_project_type_label(ptype)}\n\n"
        f"Deploy to {server} via Docker + SSH?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Yes, deploy to {server}", callback_data="deploy:yes")],
            [InlineKeyboardButton("No deployment", callback_data="deploy:no")],
        ]),
    )
    return ST_DEPLOY_ASK


async def deploy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle deploy yes/no selection."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    ptype = context.user_data["project_type"]

    if action == "no":
        context.user_data["deploy"] = False
        context.user_data["subdomain"] = None
        return await _go_to_requirements(query, context)

    context.user_data["deploy"] = True
    context.user_data["deploy_server"] = config.DEPLOY_SERVER

    # If web service or bot with web interface — ask subdomain
    if ptype == "web":
        return await _ask_subdomain(query, context)

    # For bots: ask if there's an admin panel (web UI)
    if ptype == "bot":
        await query.edit_message_text(
            "Will this bot have a web admin panel?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Yes", callback_data="adminpanel:yes")],
                [InlineKeyboardButton("No", callback_data="adminpanel:no")],
            ]),
        )
        return ST_SUBDOMAIN_INPUT

    # Standalone with deploy — no subdomain needed
    context.user_data["subdomain"] = None
    return await _go_to_requirements(query, context)


async def _ask_subdomain(query, context: ContextTypes.DEFAULT_TYPE):
    """Ask for subdomain selection."""
    domain = config.DEPLOY_DOMAIN
    name = context.user_data["project_name"]

    if not domain:
        await query.edit_message_text(
            "No DEPLOY_DOMAIN configured in .env.\n"
            "Skipping subdomain setup.\n\nContinuing...",
        )
        context.user_data["subdomain"] = None
        return await _go_to_requirements(query, context)

    # Suggest project name as subdomain
    suggested = name.replace("-", "")
    await query.edit_message_text(
        f"Choose a subdomain on {domain}:\n\n"
        f"Suggested: {suggested}.{domain}\n\n"
        f"Send a subdomain name, or tap the suggestion:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{suggested}.{domain}", callback_data=f"subdomain:{suggested}")],
            [InlineKeyboardButton(f"{name}.{domain}", callback_data=f"subdomain:{name}")],
        ]),
    )
    return ST_SUBDOMAIN_INPUT


async def subdomain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle subdomain selection via button or admin panel question."""
    query = update.callback_query
    await query.answer()
    data = query.data

    # Handle admin panel question for bots
    if data.startswith("adminpanel:"):
        action = data.split(":", 1)[1]
        if action == "yes":
            return await _ask_subdomain(query, context)
        else:
            context.user_data["subdomain"] = None
            return await _go_to_requirements(query, context)

    # Handle subdomain selection
    subdomain = data.split(":", 1)[1]
    domain = config.DEPLOY_DOMAIN
    context.user_data["subdomain"] = f"{subdomain}.{domain}"
    return await _go_to_requirements(query, context)


async def subdomain_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom subdomain typed by user."""
    import re
    subdomain = update.message.text.strip().lower()

    # Strip domain suffix if user typed the full thing
    domain = config.DEPLOY_DOMAIN
    if domain and subdomain.endswith(f".{domain}"):
        subdomain = subdomain[: -(len(domain) + 1)]

    if not re.match(r"^[a-z][a-z0-9-]*[a-z0-9]$", subdomain) or len(subdomain) < 2:
        await update.message.reply_text(
            "Invalid subdomain. Use lowercase letters, numbers, hyphens. Try again:"
        )
        return ST_SUBDOMAIN_INPUT

    context.user_data["subdomain"] = f"{subdomain}.{domain}"
    return await _go_to_requirements_msg(update, context)


async def _text_fallback_engines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remind user to use buttons during engine selection."""
    await update.message.reply_text(
        "Please tap the engine buttons above to toggle them, then press Confirm."
    )
    return ST_ENGINE_SELECT


async def _text_fallback_project_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remind user to use buttons during project type selection."""
    await update.message.reply_text(
        "Please tap one of the project type buttons above:\n"
        "Telegram Bot / Web Service / Standalone"
    )
    return ST_PROJECT_TYPE


async def _text_fallback_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remind user to use buttons during deploy question."""
    await update.message.reply_text(
        "Please tap one of the deployment buttons above."
    )
    return ST_DEPLOY_ASK


async def _text_fallback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remind user to use buttons for final confirmation."""
    await update.message.reply_text(
        "Please tap Start Factory or Cancel above."
    )
    return ST_CONFIRM


async def _go_to_requirements(query, context: ContextTypes.DEFAULT_TYPE):
    """Transition to requirements input from a callback query."""
    await query.edit_message_text(
        _deployment_summary(context) + "\n\n"
        "Now describe your requirements.\n"
        "Send voice messages (Hebrew/English) or text.\n"
        "Press Done when finished.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Done", callback_data="req:done"),
            InlineKeyboardButton("Clear All", callback_data="req:clear"),
        ]]),
    )
    return ST_REQUIREMENTS_INPUT


async def _go_to_requirements_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transition to requirements input from a text message."""
    await update.message.reply_text(
        _deployment_summary(context) + "\n\n"
        "Now describe your requirements.\n"
        "Send voice messages (Hebrew/English) or text.\n"
        "Press Done when finished.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Done", callback_data="req:done"),
            InlineKeyboardButton("Clear All", callback_data="req:clear"),
        ]]),
    )
    return ST_REQUIREMENTS_INPUT


def _deployment_summary(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Build a summary string of deployment choices so far."""
    ud = context.user_data
    name = ud.get("project_name", "?")
    ptype = _project_type_label(ud.get("project_type", "?"))
    deploy = ud.get("deploy", False)
    subdomain = ud.get("subdomain")

    lines = [f"Project: {name}", f"Type: {ptype}"]
    if deploy:
        server = ud.get("deploy_server", config.DEPLOY_SERVER)
        lines.append(f"Deploy: Docker -> {server}")
        if subdomain:
            lines.append(f"URL: {subdomain}")
    else:
        lines.append("Deploy: no")
    return "\n".join(lines)


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

        deploy_info = _deployment_summary(context)
        await query.edit_message_text(
            f"{deploy_info}\n"
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
    deploy_info = _deployment_summary(context)

    await update.message.reply_text(
        f"{deploy_info}\n"
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
    try:
      name = context.user_data["project_name"]
      engines = context.user_data["selected_engines"]
      requirements = context.user_data["requirements_text"]
    except KeyError as e:
      log.error("confirm_callback: missing user_data key %s. user_data=%s", e, dict(context.user_data))
      await query.edit_message_text(f"Error: wizard state lost (key {e}). Please /cancel and /new to start again.")
      return ConversationHandler.END
    user_id = update.effective_user.id

    state.create_project(
        name=name,
        engines=list(engines),
        description=requirements[:200],
        requirements=requirements,
        created_by=user_id,
        project_type=context.user_data.get("project_type", "standalone"),
        deploy=context.user_data.get("deploy", False),
        deploy_server=context.user_data.get("deploy_server", ""),
        subdomain=context.user_data.get("subdomain", "") or "",
    )

    deploy_config = {
        "project_type": context.user_data.get("project_type", "standalone"),
        "deploy": context.user_data.get("deploy", False),
        "deploy_server": context.user_data.get("deploy_server", ""),
        "subdomain": context.user_data.get("subdomain", "") or "",
    }

    started = []
    for engine in engines:
        factory.setup_project(name, engine, requirements, deploy_config=deploy_config)
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
            deploy_config = {
                "project_type": proj.get("project_type", "standalone"),
                "deploy": proj.get("deploy", False),
                "deploy_server": proj.get("deploy_server", ""),
                "subdomain": proj.get("subdomain", ""),
            }
            factory.setup_project(name, engine, requirements, deploy_config=deploy_config)
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
    elif text == "Engines":
        return await cmd_engines(update, context)
    elif text == "Auth":
        return await cmd_auth(update, context)



# ─── Auth panel ──────────────────────────────────────────────────────────────
# Conversation states (offset from 10 to avoid clashing with wizard's 0-7)
AUTH_PANEL      = 10
AUTH_OAUTH_CODE = 11
AUTH_API_KEY    = 12

# Engine display labels
_ENGINE_LABELS = {
    "claude":   "🤖 Claude Code",
    "gemini":   "💎 Gemini CLI",
    "opencode": "📝 OpenCode",
    "aider":    "🔧 Aider",
    "pi":       "🔵 pi",
}

# Store active auth operations: user_id -> {"engine": str, "phase": str}
_auth_state: dict[int, dict] = {}


def _auth_keyboard(statuses: dict) -> InlineKeyboardMarkup:
    """Build the auth panel keyboard with live status indicators."""
    rows = []
    for eng, label in _ENGINE_LABELS.items():
        ok, desc = statuses.get(eng, (False, "?"))
        icon = "✅" if ok else "❌"
        rows.append([InlineKeyboardButton(
            f"{icon} {label}",
            callback_data=f"auth:select:{eng}",
        )])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="auth:refresh")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="auth:close")])
    return InlineKeyboardMarkup(rows)


def _engine_action_keyboard(eng: str, ok: bool) -> InlineKeyboardMarkup:
    """Show all 3 provider OAuth options + direct key for every engine."""
    rows = [
        [InlineKeyboardButton(
            "🟠 Anthropic OAuth  (claude.ai)",
            callback_data=f"auth:poauth:{eng}:anthropic",
        )],
        [InlineKeyboardButton(
            "🔵 Google / Gemini OAuth",
            callback_data=f"auth:poauth:{eng}:google",
        )],
        [InlineKeyboardButton(
            "🟢 OpenAI OAuth  (platform.openai.com)",
            callback_data=f"auth:poauth:{eng}:openai",
        )],
        [InlineKeyboardButton(
            "🔑 Set API key directly",
            callback_data=f"auth:apikey:{eng}",
        )],
        [InlineKeyboardButton(
            "🔍 Check status",
            callback_data=f"auth:status:{eng}",
        )],
        [InlineKeyboardButton("⬅️ Back", callback_data="auth:back")],
    ]
    return InlineKeyboardMarkup(rows)



async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the engine authentication panel."""
    if not await _authorized(update, context):
        return ConversationHandler.END

    await update.message.chat.send_action(ChatAction.TYPING)
    statuses = await auth_engines.all_status()

    lines = ["🔐 *Engine Auth Panel*\n"]
    for eng, label in _ENGINE_LABELS.items():
        ok, desc = statuses.get(eng, (False, "?"))
        lines.append(f"{label}: {desc}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_auth_keyboard(statuses),
    )
    return AUTH_PANEL


async def auth_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses on the auth panel."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")  # ["auth", action, ...]
    action = parts[1] if len(parts) > 1 else ""
    uid = update.effective_user.id

    if action == "close":
        await query.edit_message_text("Auth panel closed.")
        return ConversationHandler.END

    if action == "back" or action == "refresh":
        await query.edit_message_text("Loading...", reply_markup=None)
        statuses = await auth_engines.all_status()
        lines = ["🔐 *Engine Auth Panel*\n"]
        for eng, label in _ENGINE_LABELS.items():
            ok, desc = statuses.get(eng, (False, "?"))
            lines.append(f"{label}: {desc}")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_auth_keyboard(statuses),
        )
        return AUTH_PANEL

    if action == "select":
        eng = parts[2]
        label = _ENGINE_LABELS.get(eng, eng)
        ok, desc = await _get_engine_status(eng)
        await query.edit_message_text(
            f"*{label}*\n\nStatus: {desc}\n\nChoose action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_engine_action_keyboard(eng, ok),
        )
        context.user_data["auth_engine"] = eng
        return AUTH_PANEL

    if action == "status":
        eng = parts[2]
        label = _ENGINE_LABELS.get(eng, eng)
        ok, desc = await _get_engine_status(eng)
        await query.edit_message_text(
            f"*{label}*\n\nStatus: {desc}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_engine_action_keyboard(eng, ok),
        )
        return AUTH_PANEL

    if action in ("oauth", "poauth"):
        eng = parts[2]
        provider = (parts[3] if len(parts) > 3
                    else ("anthropic" if eng == "claude"
                          else "google" if eng == "gemini"
                          else "anthropic"))
        label = _ENGINE_LABELS.get(eng, eng)

        _PMETA = {
            "anthropic": {
                "label": "Anthropic  (claude.ai)",  "icon": "🟠",
                "step2": "21️⃣ Log in at *claude.ai* → browser redirects to console.anthropic.com",
                "code_label": "full redirect URL  (or just code#state from URL bar)",
                "hint_re": None,
            },
            "google": {
                "label": "Google / Gemini",  "icon": "🔵",
                "step2": "22️⃣ Sign in → browser tries localhost:8085 (will fail)",
                "code_label": "full redirect URL from address bar (http://localhost:8085/...)",
                "hint_re": None,
            },
            "openai": {
                "label": "OpenAI",  "icon": "🟢",
                "step2": "2️⃣ Create / copy an *API key* from that page",
                "code_label": "API key  (starts with sk-...)",
                "hint_re": None,
            },
        }
        pm = _PMETA.get(provider, _PMETA["anthropic"])

        await query.edit_message_text(
            f"Starting {pm['icon']} {pm['label']} for {label}..."
        )

        if provider == "anthropic":
            ok, result = await auth_engines.anthropic_start_oauth()
        elif provider == "google":
            ok, result = await auth_engines.gemini_start_oauth()
        elif provider == "openai":
            ok, result = await auth_engines.openai_start_oauth()
        else:
            await query.edit_message_text(f"Unknown provider: {provider}")
            return AUTH_PANEL

        if not ok:
            await query.edit_message_text(
                f"❌ Could not start {pm['label']}:\n\n{result}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Back", callback_data="auth:back"),
                ]]),
            )
            return AUTH_PANEL

        context.user_data["auth_engine"]   = eng
        context.user_data["auth_provider"] = provider
        _auth_state[uid] = {"engine": eng, "provider": provider, "phase": "oauth"}

        import re as _re
        hint = ""
        if pm["hint_re"]:
            _m = _re.search(pm["hint_re"], result)
            hint = f"\n_(State: `{_m.group(1)[:12]}...`)_" if _m else ""

        await query.edit_message_text(
            f"*{pm['icon']} {pm['label']}  →  {label}*\n\n"
            f"1️⃣ Open in your browser:\n`{result}`\n\n"
            f"{pm['step2']}{hint}\n\n"
            f"3️⃣ Paste the *{pm['code_label']}* here\n\n"
            "/cancel\\_auth to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return AUTH_OAUTH_CODE


    if action == "apikey":
        eng = parts[2]
        label = _ENGINE_LABELS.get(eng, eng)
        context.user_data["auth_engine"] = eng
        _auth_state[uid] = {"engine": eng, "phase": "apikey"}

        provider_hints = {
            "claude": "ANTHROPIC_API_KEY (sk-ant-...)",
            "gemini": "GOOGLE_API_KEY or GEMINI_API_KEY",
            "opencode": "OPENROUTER_API_KEY or ANTHROPIC_API_KEY",
            "aider": "GROQ_API_KEY (gsk_...) | OPENROUTER_API_KEY | ANTHROPIC_API_KEY",
            "pi": "ANTHROPIC_API_KEY (sk-ant-...)",
        }
        await query.edit_message_text(
            f"*{label} — Set API Key*\n\n"
            f"Expected: `{provider_hints.get(eng, 'API key')}`\n\n"
            f"Paste your API key now.\n"
            f"_(For Aider: format as `groq:gsk_...` or `openrouter:sk-or-...` "
            f"to pick provider)_\n\n"
            f"Send /cancel\\_auth to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return AUTH_API_KEY

    return AUTH_PANEL


async def _get_engine_status(eng: str) -> tuple[bool, str]:
    """Single-engine status check."""
    fns = {
        "claude": auth_engines.claude_auth_status,
        "gemini": auth_engines.gemini_auth_status,
        "opencode": auth_engines.opencode_auth_status,
        "aider": auth_engines.aider_auth_status,
        "pi": auth_engines.pi_auth_status,
    }
    fn = fns.get(eng)
    if fn:
        return await fn()
    return False, "Unknown engine"


async def auth_oauth_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the OAuth code from the user and deliver it to the auth server."""
    if not await _authorized(update, context):
        return ConversationHandler.END

    uid = update.effective_user.id
    code = update.message.text.strip()

    # Pass full text to deliver functions (they handle code#state and full URLs)

    eng = context.user_data.get("auth_engine", "")
    label = _ENGINE_LABELS.get(eng, eng)

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text(f"Delivering code to {label} auth server...")

    provider = context.user_data.get("auth_provider", "")
    if not provider:
        provider = ("anthropic" if eng == "claude"
                    else "google" if eng == "gemini"
                    else "anthropic")

    if provider == "anthropic":
        ok, msg = await auth_engines.anthropic_deliver_code(code, eng)
        if ok:
            note = await auth_engines.after_anthropic_oauth(eng)
            if note:
                msg += "\n\n" + note
    elif provider == "google":
        ok, msg = await auth_engines.gemini_deliver_code(code)
        if ok:
            note = await auth_engines.after_google_oauth(eng)
            if note:
                msg += "\n\n" + note
    elif provider == "openai":
        ok, msg = await auth_engines.openai_set_key(code, eng)
    else:
        await update.message.reply_text(f"Unknown provider: {provider}")
        return ConversationHandler.END

    _auth_state.pop(uid, None)

    if ok:
        await update.message.reply_text(
            f"✅ *{label} authenticated!*\n\n{msg}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=REPLY_KB,
        )
    else:
        await update.message.reply_text(
            f"❌ *Auth failed for {label}*\n\n{msg}\n\n"
            f"Try /auth to start over.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=REPLY_KB,
        )
    return ConversationHandler.END


async def auth_api_key_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive an API key from the user and store it."""
    if not await _authorized(update, context):
        return ConversationHandler.END

    uid = update.effective_user.id
    text = update.message.text.strip()
    eng = context.user_data.get("auth_engine", "")
    label = _ENGINE_LABELS.get(eng, eng)

    # Parse optional "provider:key" format for aider/opencode
    provider = eng
    api_key = text
    if ':' in text and not text.startswith('sk-') and not text.startswith('gsk_') and not text.startswith('AIza'):
        parts = text.split(':', 1)
        if len(parts[0]) < 15:  # looks like a provider name, not a key
            provider = parts[0].lower()
            api_key = parts[1]

    await update.message.chat.send_action(ChatAction.TYPING)

    if eng == "claude":
        ok, msg = await auth_engines.claude_set_api_key(api_key)
    elif eng == "gemini":
        ok, msg = await auth_engines.gemini_set_api_key(api_key)
    elif eng == "opencode":
        ok, msg = await auth_engines.opencode_set_key(provider, api_key)
    elif eng == "aider":
        ok, msg = await auth_engines.aider_set_key(provider, api_key)
    elif eng == "pi":
        ok, msg = await auth_engines.pi_set_key(api_key)
    else:
        await update.message.reply_text(f"Unknown engine: {eng}")
        return ConversationHandler.END

    _auth_state.pop(uid, None)

    icon = "✅" if ok else "❌"
    await update.message.reply_text(
        f"{icon} *{label}*\n\n{msg}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=REPLY_KB,
    )
    return ConversationHandler.END


async def auth_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the auth flow."""
    uid = update.effective_user.id
    eng = context.user_data.get("auth_engine", "")

    # Kill any running OAuth process
    if eng == "claude":
        await auth_engines.claude_cancel()
    elif eng == "gemini":
        await auth_engines.gemini_cancel()

    _auth_state.pop(uid, None)
    await update.message.reply_text("Auth cancelled.", reply_markup=REPLY_KB)
    return ConversationHandler.END


# ─── Build and run ───────────────────────────────────────────────────────────



async def _debug_all_updates(update: Update, context) -> None:
    """Log every incoming update for debugging."""
    if update.callback_query:
        q = update.callback_query
        log.info("CALLBACK: user=%s data=%r msg_id=%s",
                 q.from_user.id if q.from_user else "?",
                 q.data, q.message.message_id if q.message else "?")
    elif update.message:
        m = update.message
        log.info("MESSAGE: user=%s type=%s text=%r",
                 m.from_user.id if m.from_user else "?",
                 "voice" if m.voice else "text",
                 m.text[:50] if m.text else None)

async def error_handler(update: object, context) -> None:
    """Log all exceptions raised by handlers."""
    import traceback
    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    log.error("Handler exception:\n%s", tb)
    if hasattr(update, "effective_message") and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"Bot error: {type(context.error).__name__}: {context.error}\n\nUse /cancel and /new to restart."
            )
        except Exception:
            pass


def build_app() -> Application:
    """Build the Telegram bot application with all handlers."""
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.ALL, _debug_all_updates), group=-1)
    app.add_handler(CallbackQueryHandler(_debug_all_updates), group=-1)
    # Project creation wizard (ConversationHandler)
    new_project_conv = ConversationHandler(
        entry_points=[CommandHandler("new", cmd_new)],
        states={
            ST_ENGINE_SELECT: [
                CallbackQueryHandler(engine_toggle, pattern=r"^eng:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _text_fallback_engines),
            ],
            ST_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, name_input),
            ],
            ST_PROJECT_TYPE: [
                CallbackQueryHandler(project_type_callback, pattern=r"^ptype:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _text_fallback_project_type),
            ],
            ST_DEPLOY_ASK: [
                CallbackQueryHandler(deploy_callback, pattern=r"^deploy:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _text_fallback_deploy),
            ],
            ST_SUBDOMAIN_INPUT: [
                CallbackQueryHandler(subdomain_callback, pattern=r"^(subdomain:|adminpanel:)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, subdomain_text),
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
                MessageHandler(filters.TEXT & ~filters.COMMAND, _text_fallback_confirm),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
        allow_reentry=True,
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

    # Auth management conversation
    auth_conv = ConversationHandler(
        entry_points=[
            CommandHandler("auth", cmd_auth),
            MessageHandler(filters.TEXT & filters.Regex(r"^Auth$"), cmd_auth),
        ],
        states={
            AUTH_PANEL: [
                CallbackQueryHandler(auth_panel_callback, pattern=r"^auth:"),
            ],
            AUTH_OAUTH_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_oauth_code_input),
                CommandHandler("cancel_auth", auth_cancel),
            ],
            AUTH_API_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_api_key_input),
                CommandHandler("cancel_auth", auth_cancel),
            ],
        },
        fallbacks=[
            CommandHandler("cancel_auth", auth_cancel),
            CommandHandler("cancel", auth_cancel),
        ],
        per_user=True,
        allow_reentry=True,
    )
    app.add_handler(auth_conv)
    app.add_handler(CommandHandler("auth", cmd_auth))
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
        filters.TEXT & filters.Regex(r"^(New Project|Projects|Settings|Health|Auth|Engines)$"),
        reply_keyboard_handler,
    ))

    app.add_error_handler(error_handler)
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
