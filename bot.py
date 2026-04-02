#!/usr/bin/env python3
"""
Hlídač Shopů Telegram Bot

Two-way Telegram bot that monitors Czech/Slovak e-shop prices via the
Hlídač Shopů API and notifies you of price drops.

Commands:
    /start              - Welcome message
    /add <url> [drop%]  - Add a product (optional per-product drop threshold)
    /remove <num>       - Remove a product by its number (see /list)
    /list               - Show all monitored products with prices and thresholds
    /set <num> <drop%>  - Change the drop threshold for a product
    /check              - Force an immediate price check on all products
    /help               - Show help
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
DEFAULT_DROP_THRESHOLD = float(os.environ.get("DROP_THRESHOLD", "0"))  # percent
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
    """Get the most recent non-null price from the time series."""
    prices = data.get("data", {}).get(key, [])
    for point in reversed(prices):
        if point.get("y") is not None:
            return float(point["y"])
    return None


def extract_all_time_low(data: dict) -> float | None:
    """Compute the all-time lowest price from the full price history."""
    prices = data.get("data", {}).get("currentPrice", [])
    valid = [float(p["y"]) for p in prices if p.get("y") is not None]
    return min(valid) if valid else None


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
# URL & argument parsing
# ---------------------------------------------------------------------------

URL_PATTERN = re.compile(r"https?://[^\s]+")


def looks_like_product_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def parse_threshold(text: str) -> float | None:
    """Parse a threshold like '5', '5%', '10.5%' from text."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*%?", text)
    if match:
        return float(match.group(1))
    return None


def get_user_products(chat_id: int) -> list[dict]:
    return [p for p in load_products() if p.get("chat_id") == chat_id]


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
        "/add <url> [drop%] - Add a product (e.g. /add <url> 5%)\n"
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

    text = " ".join(context.args) if context.args else ""
    url = looks_like_product_url(text)
    if not url:
        await update.message.reply_text(
            "Usage: /add <url> [drop%]\n"
            "Example: /add https://www.alza.cz/product 5%"
        )
        return

    # Parse optional threshold from the remaining text after the URL
    remaining = text.replace(url, "").strip()
    threshold = parse_threshold(remaining) if remaining else None

    await _add_product(update, url, threshold)


async def _add_product(update: Update, url: str, threshold: float | None = None):
    products = load_products()

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
    all_time_low = extract_all_time_low(data)
    drop_threshold = threshold if threshold is not None else DEFAULT_DROP_THRESHOLD

    product = {
        "url": url,
        "name": name,
        "last_price": current_price,
        "all_time_low": all_time_low,
        "drop_threshold": drop_threshold,
        "added": datetime.now().isoformat(),
        "chat_id": update.effective_chat.id,
    }
    products.append(product)
    save_products(products)

    price_str = f"{current_price:.0f} CZK" if current_price else "N/A"
    low_str = f"{all_time_low:.0f} CZK" if all_time_low else "N/A"
    threshold_str = f"{drop_threshold:.1f}%" if drop_threshold > 0 else "any drop"

    await update.message.reply_text(
        f"Added!\n\n"
        f"{name}\n"
        f"Current price: {price_str}\n"
        f"All-time low: {low_str}\n"
        f"Notify on: {threshold_str}\n\n"
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
        await update.message.reply_text(
            f"Invalid number. Use /list to see your products (1-{len(user_products)})."
        )
        return

    removed = user_products[idx]
    products.remove(removed)
    save_products(products)

    await update.message.reply_text(f"Removed: {removed['name']}")


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
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("First argument must be a product number (see /list).")
        return

    threshold = parse_threshold(context.args[1])
    if threshold is None:
        await update.message.reply_text("Second argument must be a percentage (e.g. 5%).")
        return

    products = load_products()
    chat_id = update.effective_chat.id
    user_products = [p for p in products if p.get("chat_id") == chat_id]

    if idx < 0 or idx >= len(user_products):
        await update.message.reply_text(
            f"Invalid number. Use /list to see your products (1-{len(user_products)})."
        )
        return

    target = user_products[idx]
    target["drop_threshold"] = threshold
    save_products(products)

    await update.message.reply_text(
        f"Updated: {target['name']}\n"
        f"New drop threshold: {threshold:.1f}%"
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    chat_id = update.effective_chat.id
    user_products = get_user_products(chat_id)

    if not user_products:
        await update.message.reply_text("No products being monitored. Send me a link to add one!")
        return

    lines = ["Monitored products:\n"]
    for i, p in enumerate(user_products, 1):
        price = f"{p['last_price']:.0f} CZK" if p.get("last_price") else "N/A"
        low = f"{p['all_time_low']:.0f} CZK" if p.get("all_time_low") else "N/A"
        threshold = p.get("drop_threshold", DEFAULT_DROP_THRESHOLD)
        thr_str = f"{threshold:.1f}%" if threshold > 0 else "any"
        lines.append(
            f"{i}. {p['name']}\n"
            f"   Price: {price} | Low: {low} | Alert: {thr_str}\n"
            f"   {p['url']}\n"
        )

    await update.message.reply_text("\n".join(lines))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.message.reply_text("Checking all products now...")
    app = context.application
    notifications = await check_all_prices(app.bot)

    if notifications == 0:
        await update.message.reply_text("No price changes detected.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain messages — auto-detect URLs and optional threshold."""
    if not is_authorized(update):
        return

    text = update.message.text or ""
    url = looks_like_product_url(text)
    if url:
        remaining = text.replace(url, "").strip()
        threshold = parse_threshold(remaining) if remaining else None
        await _add_product(update, url, threshold)
    else:
        await update.message.reply_text(
            "Send me a product URL to start monitoring, or use /help."
        )


# ---------------------------------------------------------------------------
# Background price checker
# ---------------------------------------------------------------------------


async def check_all_prices(bot) -> int:
    """Check prices of all products and send notifications. Returns notification count."""
    products = load_products()
    if not products:
        return 0

    notifications = 0
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
            all_time_low = product.get("all_time_low")
            threshold = product.get("drop_threshold", DEFAULT_DROP_THRESHOLD)
            metadata = data.get("metadata", {})

            if metadata.get("name"):
                product["name"] = metadata["name"]

            # Recompute all-time low from full history
            history_low = extract_all_time_low(data)

            # --- Price drop notification ---
            if last_price is not None and current_price < last_price:
                drop_amount = last_price - current_price
                drop_pct = (drop_amount / last_price) * 100

                if drop_pct >= threshold:
                    notifications += 1
                    is_new_low = (
                        all_time_low is not None and current_price <= all_time_low
                    ) or (
                        history_low is not None and current_price <= history_low
                    )

                    header = (
                        "ALL-TIME LOW" if is_new_low else "Price drop"
                    )

                    lines = [
                        f"{header}: {product['name']}\n",
                        f"{last_price:.0f} -> {current_price:.0f} CZK",
                        f"-{drop_amount:.0f} CZK ({drop_pct:.1f}% off)\n",
                    ]

                    if all_time_low is not None and not is_new_low:
                        lines.append(f"All-time low: {all_time_low:.0f} CZK")

                    real_discount = metadata.get("realDiscount")
                    if real_discount is not None:
                        lines.append(f"Real discount: {real_discount * 100:.0f}%")

                    lines.append(f"\n{product['url']}")

                    await bot.send_message(
                        chat_id=product["chat_id"],
                        text="\n".join(lines),
                    )

            # --- All-time low notification (even without a drop from last check) ---
            # This catches cases where the stored all_time_low was stale
            elif (
                last_price is not None
                and current_price == last_price
                and all_time_low is not None
                and current_price < all_time_low
            ):
                notifications += 1
                drop_from_low = all_time_low - current_price
                drop_pct_from_low = (drop_from_low / all_time_low) * 100

                await bot.send_message(
                    chat_id=product["chat_id"],
                    text=(
                        f"ALL-TIME LOW: {product['name']}\n\n"
                        f"Current price: {current_price:.0f} CZK\n"
                        f"Previous low: {all_time_low:.0f} CZK\n"
                        f"-{drop_from_low:.0f} CZK ({drop_pct_from_low:.1f}% below previous low)\n\n"
                        f"{product['url']}"
                    ),
                )

            # Update stored data
            product["last_price"] = current_price
            if history_low is not None:
                product["all_time_low"] = min(
                    history_low,
                    product.get("all_time_low") or history_low,
                )
            product["last_check"] = datetime.now().isoformat()
            changed = True

        except Exception as e:
            log.error("Error checking %s: %s", product.get("url"), e)

    if changed:
        save_products(products)

    log.info(
        "Price check complete: %d products, %d notifications",
        len(products),
        notifications,
    )
    return notifications


async def send_startup_notification(bot):
    """Send a notification to all known chat IDs that the bot has started."""
    allowed = get_allowed_chat_ids()
    if not allowed:
        # Gather chat IDs from saved products as fallback
        products = load_products()
        allowed = {p["chat_id"] for p in products if p.get("chat_id")}

    product_count = len(load_products())
    for chat_id in allowed:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"Hlídač Shopů bot started!\n\n"
                    f"Monitoring {product_count} product(s)\n"
                    f"Check interval: {CHECK_INTERVAL // 60} min\n"
                    f"Default threshold: {DEFAULT_DROP_THRESHOLD:.1f}%"
                ),
            )
        except Exception as e:
            log.warning("Could not send startup message to %s: %s", chat_id, e)


async def periodic_checker(app: Application):
    """Background task that runs price checks at the configured interval."""
    bot = app.bot
    await send_startup_notification(bot)
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

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
