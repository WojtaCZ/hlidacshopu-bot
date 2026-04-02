#!/usr/bin/env python3
"""
Hlídač Shopů Bot -- shared business logic

Platform-agnostic code for price monitoring via the Hlídač Shopů API.
Used by both telegram_bot.py and discord_bot.py.
"""

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "3600"))  # seconds
DEFAULT_DROP_THRESHOLD = float(os.environ.get("DROP_THRESHOLD", "0"))  # percent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

API_BASE = "https://api.hlidacshopu.cz/v2"
PRODUCTS_FILE = DATA_DIR / "products.json"

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
# Product management
# ---------------------------------------------------------------------------


async def add_product(
    url: str, chat_id: int, threshold: float | None = None,
) -> tuple[str, dict | None]:
    """Add a product. Returns (message, product_dict_or_None)."""
    products = load_products()

    if any(p["url"] == url for p in products):
        return "This product is already being monitored.", None

    data = await fetch_product(url)
    if data is None:
        return (
            "Could not fetch this product. Make sure the URL is from a supported shop.\n"
            "Supported: Alza, Mall, Datart, Notino, Rohlik, Kaufland, IKEA and ~50 more."
        ), None

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
        "chat_id": chat_id,
    }
    products.append(product)
    save_products(products)

    price_str = f"{current_price:.0f} CZK" if current_price else "N/A"
    low_str = f"{all_time_low:.0f} CZK" if all_time_low else "N/A"
    threshold_str = f"{drop_threshold:.1f}%" if drop_threshold > 0 else "any drop"

    msg = (
        f"Added!\n\n"
        f"{name}\n"
        f"Current price: {price_str}\n"
        f"All-time low: {low_str}\n"
        f"Notify on: {threshold_str}\n\n"
        f"I'll notify you when the price drops."
    )
    return msg, product


def remove_product(chat_id: int, index: int) -> str:
    """Remove a product by 1-based index. Returns response message."""
    products = load_products()
    user_products = [p for p in products if p.get("chat_id") == chat_id]

    idx = index - 1
    if idx < 0 or idx >= len(user_products):
        return f"Invalid number. Use /list to see your products (1-{len(user_products)})."

    removed = user_products[idx]
    products.remove(removed)
    save_products(products)
    return f"Removed: {removed['name']}"


def set_threshold(chat_id: int, index: int, threshold: float) -> str:
    """Set threshold for a product by 1-based index. Returns response message."""
    products = load_products()
    user_products = [p for p in products if p.get("chat_id") == chat_id]

    idx = index - 1
    if idx < 0 or idx >= len(user_products):
        return f"Invalid number. Use /list to see your products (1-{len(user_products)})."

    target = user_products[idx]
    target["drop_threshold"] = threshold
    save_products(products)
    return f"Updated: {target['name']}\nNew drop threshold: {threshold:.1f}%"


def format_product_list(chat_id: int) -> str:
    """Format the product list for a user."""
    user_products = get_user_products(chat_id)
    if not user_products:
        return "No products being monitored. Send me a link to add one!"

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
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Notification formatting
# ---------------------------------------------------------------------------


def format_price_drop(
    product: dict,
    current_price: float,
    last_price: float,
    all_time_low: float | None,
    is_new_low: bool,
    real_discount: float | None,
) -> str:
    """Format a price drop notification message."""
    drop_amount = last_price - current_price
    drop_pct = (drop_amount / last_price) * 100
    header = "ALL-TIME LOW" if is_new_low else "Price drop"

    lines = [
        f"{header}: {product['name']}\n",
        f"{last_price:.0f} -> {current_price:.0f} CZK",
        f"-{drop_amount:.0f} CZK ({drop_pct:.1f}% off)\n",
    ]

    if all_time_low is not None and not is_new_low:
        lines.append(f"All-time low: {all_time_low:.0f} CZK")

    if real_discount is not None:
        lines.append(f"Real discount: {real_discount * 100:.0f}%")

    lines.append(f"\n{product['url']}")
    return "\n".join(lines)


def format_all_time_low(
    product: dict, current_price: float, all_time_low: float,
) -> str:
    """Format an all-time low notification (no drop from last check)."""
    drop_from_low = all_time_low - current_price
    drop_pct_from_low = (drop_from_low / all_time_low) * 100
    return (
        f"ALL-TIME LOW: {product['name']}\n\n"
        f"Current price: {current_price:.0f} CZK\n"
        f"Previous low: {all_time_low:.0f} CZK\n"
        f"-{drop_from_low:.0f} CZK ({drop_pct_from_low:.1f}% below previous low)\n\n"
        f"{product['url']}"
    )


def format_startup_message(product_count: int) -> str:
    """Format the startup notification message."""
    return (
        f"Hlídač Shopů bot started!\n\n"
        f"Monitoring {product_count} product(s)\n"
        f"Check interval: {CHECK_INTERVAL // 60} min\n"
        f"Default threshold: {DEFAULT_DROP_THRESHOLD:.1f}%"
    )


# ---------------------------------------------------------------------------
# Background price checker
# ---------------------------------------------------------------------------


async def check_all_prices(
    send_notification: Callable[[int, str], Awaitable[None]],
) -> int:
    """Check prices of all products and send notifications.

    send_notification(chat_id, text) is called for each notification.
    Returns notification count.
    """
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
                drop_pct = ((last_price - current_price) / last_price) * 100

                if drop_pct >= threshold:
                    notifications += 1
                    is_new_low = (
                        all_time_low is not None and current_price <= all_time_low
                    ) or (
                        history_low is not None and current_price <= history_low
                    )

                    text = format_price_drop(
                        product, current_price, last_price,
                        all_time_low, is_new_low,
                        metadata.get("realDiscount"),
                    )
                    await send_notification(product["chat_id"], text)

            # --- All-time low notification (even without a drop from last check) ---
            elif (
                last_price is not None
                and current_price == last_price
                and all_time_low is not None
                and current_price < all_time_low
            ):
                notifications += 1
                text = format_all_time_low(product, current_price, all_time_low)
                await send_notification(product["chat_id"], text)

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
