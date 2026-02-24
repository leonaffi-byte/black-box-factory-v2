"""Whitelist-based authentication middleware for Telegram bot."""

from telegram import Update
from telegram.ext import ContextTypes

from . import config, state


async def auth_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if user is whitelisted. Silently ignore unauthorized users."""
    if update.effective_user is None:
        return False
    user_id = update.effective_user.id

    # Admin is always authorized
    if user_id == config.ADMIN_TELEGRAM_ID:
        # Auto-register admin on first use
        users = state.load_users()
        if str(user_id) not in users:
            state.add_user(
                user_id,
                update.effective_user.full_name,
                "admin",
            )
        return True

    users = state.load_users()
    user = users.get(str(user_id))
    return user is not None and user.get("active", True)


def admin_only(func):
    """Decorator: only allow admin to run this handler."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id == config.ADMIN_TELEGRAM_ID:
            return await func(update, context)
        if update.message:
            await update.message.reply_text("Admin only.")
    return wrapper
