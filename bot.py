#!/usr/bin/env python3
"""
Hlídač Shopů Telegram Bot

Two-way Telegram bot that monitors Czech/Slovak e-shop prices via the
Hlídač Shopů API and notifies you of price drops.

Commands:
    /start          - Welcome message
    /add <url>      - Add a product to monitor (or just send a URL)
    /remove <num>   - Remove a product by its number (see /list)
    /list           - Show all monitored products
    /check          - Force an immediate price check on all products
    /help           - Show help
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED_CHAT_IDS = os.environ.get("ALLOWED_CHAT_IDS", "")  # comma-separated
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "3600"))  # seconds
DROP_THRESHOLD = float(os.environ.get("DROP_THRESHOLD", "0"))  # percent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

API_BASE = "https://api.hlidacshopu.cz/v2"
PRODUCTS_FILE = DATA_DIR / "products.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hlidac-bot")

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_products() -> list[dict]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if PRODUCTS_FILE.exists():
        try:
            return json.loads(PRODUCTS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_products(products: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PRODUCTS_FILE.write_text(json.dumps(products, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Hlídač Shopů API
# ---------------------------------------------------------------------------


async def fetch_product(url: str) -> dict | None:
    api_url = f"{API_BASE}/detail?{urlencode({'url': url})}"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(api_url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            log.error("API error for %s: %s", url, e)
            return None


def extract_price(data: dict, key: str = "currentPrice") -> float | None:
    prices = data.get("data", {}).get(key, [])
    for point in reversed(prices):
        if point.get("y") is not None:
            return float(point["y"])
    return None


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
# URL detection
# ---------------------------------------------------------------------------

URL_PATTERN = re.compile(r"https?://[^\s]+")


def looks_like_product_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


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
        "/add <url> - Add a product\n"
        "/remove <number> - Remove a product\n"
        "/list - Show monitored products\n"
        "/check - Force a price check now\n"
        "/help - Show this message"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = " ".join(context.args) if context.args else ""
    url = looks_like_product_url(text)
    if not url:
        await update.message.reply_text("Please provide a product URL: /add <url>")
        return

    await _add_product(update, url)


async def _add_product(update: Update, url: str):
    products = load_products()

    # Check for duplicates
    if any(p["url"] == url for p in products):
        await update.message.reply_text("This product is already being monitored.")
        return

    await update.message.reply_text("Fetching product info...")

    data = await fetch_product(url)
    if data is None:
        await update.message.reply_text(
            "Could not fetch this product. Make sure the URL is from a supported shop.\n"
            "Supported: Alza, Mall, Datart, Notino, Rohlik, Kaufland, IKEA and ~50 more."
        )
        return

    metadata = data.get("metadata", {})
    name = metadata.get("name", "Unknown product")
    current_price = extract_price(data)

    product = {
        "url": url,
        "name": name,
        "last_price": current_price,
        "added": datetime.now().isoformat(),
        "chat_id": update.effective_chat.id,
    }
    products.append(product)
    save_products(products)

    price_str = f"{current_price:.0f} CZK" if current_price else "N/A"
    await update.message.reply_text(
        f"Added!\n\n"
        f"{name}\n"
        f"Current price: {price_str}\n\n"
        f"I'll notify you when the price drops."
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /remove <number> (see /list for numbers)")
        return

    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("Please provide a number: /remove <number>")
        return

    products = load_products()
    chat_id = update.effective_chat.id
    user_products = [p for p in products if p.get("chat_id") == chat_id]

    if idx < 0 or idx >= len(user_products):
        await update.message.reply_text(f"Invalid number. Use /list to see your products (1-{len(user_products)}).")
        return

    removed = user_products[idx]
    products.remove(removed)
    save_products(products)

    await update.message.reply_text(f"Removed: {removed['name']}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    products = load_products()
    chat_id = update.effective_chat.id
    user_products = [p for p in products if p.get("chat_id") == chat_id]

    if not user_products:
        await update.message.reply_text("No products being monitored. Send me a link to add one!")
        return

    lines = ["Monitored products:\n"]
    for i, p in enumerate(user_products, 1):
        price = f"{p['last_price']:.0f} CZK" if p.get("last_price") else "N/A"
        lines.append(f"{i}. {p['name']}\n   {price}\n   {p['url']}\n")

    await update.message.reply_text("\n".join(lines))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.message.reply_text("Checking all products now...")
    app = context.application
    drops = await check_all_prices(app.bot)

    if drops == 0:
        await update.message.reply_text("No price changes detected.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain messages — auto-detect URLs."""
    if not is_authorized(update):
        return

    text = update.message.text or ""
    url = looks_like_product_url(text)
    if url:
        await _add_product(update, url)
    else:
        await update.message.reply_text(
            "Send me a product URL to start monitoring, or use /help."
        )


# ---------------------------------------------------------------------------
# Background price checker
# ---------------------------------------------------------------------------


async def check_all_prices(bot) -> int:
    """Check prices of all products and send notifications. Returns drop count."""
    products = load_products()
    if not products:
        return 0

    drops = 0
    changed = False

    for product in products:
        try:
            data = await fetch_product(product["url"])
            if data is None:
                continue

            current_price = extract_price(data)
            if current_price is None:
                continue

            last_price = product.get("last_price")
            metadata = data.get("metadata", {})

            # Update name if we have a better one
            if metadata.get("name"):
                product["name"] = metadata["name"]

            if last_price is not None and current_price < last_price:
                drop_amount = last_price - current_price
                drop_pct = (drop_amount / last_price) * 100

                if drop_pct >= DROP_THRESHOLD:
                    drops += 1
                    lines = [
                        f"Price drop: {product['name']}\n",
                        f"{last_price:.0f} -> {current_price:.0f} CZK",
                        f"(-{drop_amount:.0f} CZK, -{drop_pct:.1f}%)\n",
                    ]

                    real_discount = metadata.get("realDiscount")
                    if real_discount is not None:
                        lines.append(f"Real discount: {real_discount * 100:.0f}%")

                    lines.append(f"\n{product['url']}")

                    await bot.send_message(
                        chat_id=product["chat_id"],
                        text="\n".join(lines),
                    )

            elif last_price is not None and current_price > last_price:
                increase = current_price - last_price
                increase_pct = (increase / last_price) * 100
                if increase_pct >= 5:  # only notify on significant increases
                    await bot.send_message(
                        chat_id=product["chat_id"],
                        text=(
                            f"Price increase: {product['name']}\n\n"
                            f"{last_price:.0f} -> {current_price:.0f} CZK "
                            f"(+{increase:.0f} CZK, +{increase_pct:.1f}%)\n\n"
                            f"{product['url']}"
                        ),
                    )

            product["last_price"] = current_price
            product["last_check"] = datetime.now().isoformat()
            changed = True

        except Exception as e:
            log.error("Error checking %s: %s", product.get("url"), e)

    if changed:
        save_products(products)

    log.info("Price check complete: %d products, %d drops", len(products), drops)
    return drops


async def periodic_checker(app: Application):
    """Background task that runs price checks at the configured interval."""
    bot = app.bot
    log.info("Background checker started (interval: %ds)", CHECK_INTERVAL)
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            await check_all_prices(bot)
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

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start background checker after bot is initialized
    async def post_init(app: Application):
        app.create_task(periodic_checker(app))

    app.post_init = post_init

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
