#!/usr/bin/env python3
"""
Hlídač Shopů Telegram Bot

Telegram frontend for the Hlídač Shopů price monitoring bot.
"""

import asyncio
import logging
import os
import sys

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import core

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED_CHAT_IDS = os.environ.get("ALLOWED_CHAT_IDS", "")  # comma-separated

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hlidac-bot")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def get_allowed_chat_ids() -> set[int]:
    if not ALLOWED_CHAT_IDS:
        return set()
    return {int(cid.strip()) for cid in ALLOWED_CHAT_IDS.split(",") if cid.strip()}


def is_authorized(update: Update) -> bool:
    allowed = get_allowed_chat_ids()
    if not allowed:
        return True  # no restriction configured
    return update.effective_chat.id in allowed


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "Ahoj! I'm your Hlídač Shopů price watcher.\n\n"
        "Send me a product link from a Czech/Slovak e-shop "
        "and I'll monitor it for price drops.\n\n"
        f"Your chat ID: {chat_id}\n\n"
        "Commands:\n"
        "/add <url> [drop%] - Add product(s), one per line\n"
        "/remove <number> - Remove a product\n"
        "/list - Show monitored products\n"
        "/set <number> <drop%> - Change drop threshold\n"
        "/check - Force a price check now\n"
        "/help - Show this message"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text or ""
    items = core.parse_product_lines(text)
    if not items:
        await update.message.reply_text(
            "Usage: /add <url> [drop%]\n"
            "Example: /add https://www.alza.cz/product 5%\n\n"
            "You can add multiple products at once:\n"
            "/add https://www.alza.cz/product1\n"
            "https://www.alza.cz/product2 5%\n"
            "https://www.datart.cz/product3"
        )
        return

    if len(items) == 1:
        await _add_product(update, items[0][0], items[0][1])
    else:
        await update.message.reply_text(f"Processing {len(items)} links...")
        msg = await core.add_products_batch(items, update.effective_chat.id)
        await update.message.reply_text(msg)


async def _add_product(update: Update, url: str, threshold: float | None = None):
    await update.message.reply_text("Fetching product info...")
    msg, _ = await core.add_product(url, update.effective_chat.id, threshold)
    await update.message.reply_text(msg)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /remove <number> (see /list for numbers)")
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please provide a number: /remove <number>")
        return

    msg = core.remove_product(update.effective_chat.id, idx)
    await update.message.reply_text(msg)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /set <number> <drop%>\n"
            "Example: /set 1 5%"
        )
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("First argument must be a product number (see /list).")
        return

    threshold = core.parse_threshold(context.args[1])
    if threshold is None:
        await update.message.reply_text("Second argument must be a percentage (e.g. 5%).")
        return

    msg = core.set_threshold(update.effective_chat.id, idx, threshold)
    await update.message.reply_text(msg)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = core.format_product_list(update.effective_chat.id)
    await update.message.reply_text(msg)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.message.reply_text("Checking all products now...")
    bot = context.application.bot

    async def send_notification(chat_id: int, text: str):
        await bot.send_message(chat_id=chat_id, text=text)

    notifications = await core.check_all_prices(send_notification)

    if notifications == 0:
        await update.message.reply_text("No price changes detected.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain messages -- auto-detect URLs and optional threshold."""
    if not is_authorized(update):
        return

    text = update.message.text or ""
    items = core.parse_product_lines(text)
    if not items:
        await update.message.reply_text(
            "Send me a product URL to start monitoring, or use /help."
        )
    elif len(items) == 1:
        await _add_product(update, items[0][0], items[0][1])
    else:
        await update.message.reply_text(f"Processing {len(items)} links...")
        msg = await core.add_products_batch(items, update.effective_chat.id)
        await update.message.reply_text(msg)


# ---------------------------------------------------------------------------
# Background price checker
# ---------------------------------------------------------------------------


async def send_startup_notification(bot):
    """Send a notification to all known chat IDs that the bot has started."""
    allowed = get_allowed_chat_ids()
    if not allowed:
        # Gather chat IDs from saved products as fallback
        products = core.load_products()
        allowed = {p["chat_id"] for p in products if p.get("chat_id")}

    product_count = len(core.load_products())
    text = core.format_startup_message(product_count)

    for chat_id in allowed:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            log.warning("Could not send startup message to %s: %s", chat_id, e)


async def periodic_checker(app: Application):
    """Background task that runs price checks at the configured interval."""
    bot = app.bot
    await send_startup_notification(bot)
    log.info("Background checker started (interval: %ds)", core.CHECK_INTERVAL)

    async def send_notification(chat_id: int, text: str):
        await bot.send_message(chat_id=chat_id, text=text)

    while True:
        await asyncio.sleep(core.CHECK_INTERVAL)
        try:
            await core.check_all_prices(send_notification)
        except Exception as e:
            log.error("Periodic check failed: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not TELEGRAM_TOKEN:
        print("Error: Set TELEGRAM_TOKEN environment variable.")
        print("Get one from @BotFather on Telegram.")
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(app: Application):
        app.create_task(periodic_checker(app))

    app.post_init = post_init

    log.info("Telegram bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
