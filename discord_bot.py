#!/usr/bin/env python3
"""
Hlídač Shopů Discord Bot

Discord frontend for the Hlídač Shopů price monitoring bot.
Uses slash commands and discord.ext.tasks for periodic checking.
"""

import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands, tasks

import core

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")  # comma-separated
ALLOWED_CHANNEL_IDS = os.environ.get("ALLOWED_CHANNEL_IDS", "")  # comma-separated
NOTIFICATION_CHANNEL_ID = os.environ.get("NOTIFICATION_CHANNEL_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hlidac-bot")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def get_allowed_user_ids() -> set[int]:
    if not ALLOWED_USER_IDS:
        return set()
    return {int(uid.strip()) for uid in ALLOWED_USER_IDS.split(",") if uid.strip()}


def get_allowed_channel_ids() -> set[int]:
    if not ALLOWED_CHANNEL_IDS:
        return set()
    return {int(cid.strip()) for cid in ALLOWED_CHANNEL_IDS.split(",") if cid.strip()}


def is_authorized(interaction: discord.Interaction) -> bool:
    allowed_users = get_allowed_user_ids()
    allowed_channels = get_allowed_channel_ids()

    if not allowed_users and not allowed_channels:
        return True  # no restriction configured

    if allowed_users and interaction.user.id in allowed_users:
        return True
    if allowed_channels and interaction.channel_id in allowed_channels:
        return True

    return False


def is_message_authorized(message: discord.Message) -> bool:
    allowed_users = get_allowed_user_ids()
    allowed_channels = get_allowed_channel_ids()

    if not allowed_users and not allowed_channels:
        return True

    if allowed_users and message.author.id in allowed_users:
        return True
    if allowed_channels and message.channel.id in allowed_channels:
        return True

    return False


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------------
# Helper: send notification to a channel by ID
# ---------------------------------------------------------------------------


async def send_notification(chat_id: int, text: str):
    channel = bot.get_channel(chat_id)
    if channel:
        await channel.send(text)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


@bot.tree.command(name="add", description="Add a product to monitor")
@app_commands.describe(
    url="Product URL from a Czech/Slovak e-shop",
    threshold="Minimum price drop % to notify (e.g. 5)",
)
async def cmd_add(
    interaction: discord.Interaction, url: str, threshold: float | None = None,
):
    if not is_authorized(interaction):
        await interaction.response.send_message("You are not authorized.", ephemeral=True)
        return

    if not core.looks_like_product_url(url):
        await interaction.response.send_message(
            "Please provide a valid product URL.\n"
            "Example: `/add url:https://www.alza.cz/product threshold:5`",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    msg, _ = await core.add_product(url, interaction.channel_id, threshold)
    await interaction.followup.send(msg)


@bot.tree.command(name="remove", description="Remove a product by its number (see /list)")
@app_commands.describe(number="Product number from /list")
async def cmd_remove(interaction: discord.Interaction, number: int):
    if not is_authorized(interaction):
        await interaction.response.send_message("You are not authorized.", ephemeral=True)
        return

    msg = core.remove_product(interaction.channel_id, number)
    await interaction.response.send_message(msg)


@bot.tree.command(name="set", description="Change the drop threshold for a product")
@app_commands.describe(
    number="Product number from /list",
    threshold="New minimum drop % to notify",
)
async def cmd_set(interaction: discord.Interaction, number: int, threshold: float):
    if not is_authorized(interaction):
        await interaction.response.send_message("You are not authorized.", ephemeral=True)
        return

    msg = core.set_threshold(interaction.channel_id, number, threshold)
    await interaction.response.send_message(msg)


@bot.tree.command(name="list", description="Show all monitored products")
async def cmd_list(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await interaction.response.send_message("You are not authorized.", ephemeral=True)
        return

    msg = core.format_product_list(interaction.channel_id)
    await interaction.response.send_message(msg)


@bot.tree.command(name="check", description="Force an immediate price check on all products")
async def cmd_check(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await interaction.response.send_message("You are not authorized.", ephemeral=True)
        return

    await interaction.response.defer()
    notifications = await core.check_all_prices(send_notification)
    if notifications == 0:
        await interaction.followup.send("No price changes detected.")
    else:
        await interaction.followup.send(f"Check complete. {notifications} notification(s) sent.")


@bot.tree.command(name="help", description="Show available commands")
async def cmd_help(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Hlídač Shopů price watcher**\n\n"
        "Send me a product link from a Czech/Slovak e-shop "
        "and I'll monitor it for price drops.\n\n"
        "**Commands:**\n"
        "`/add <url> [threshold]` - Add a product\n"
        "`/remove <number>` - Remove a product\n"
        "`/list` - Show monitored products\n"
        "`/set <number> <threshold>` - Change drop threshold\n"
        "`/check` - Force a price check now\n"
        "`/help` - Show this message"
    )


# ---------------------------------------------------------------------------
# Auto-detect URLs in plain messages
# ---------------------------------------------------------------------------


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if not is_message_authorized(message):
        return

    url = core.looks_like_product_url(message.content)
    if url:
        remaining = message.content.replace(url, "").strip()
        threshold = core.parse_threshold(remaining) if remaining else None

        async with message.channel.typing():
            msg, _ = await core.add_product(url, message.channel.id, threshold)
        await message.reply(msg)

    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Background price checker
# ---------------------------------------------------------------------------


@tasks.loop(seconds=core.CHECK_INTERVAL)
async def periodic_checker():
    try:
        await core.check_all_prices(send_notification)
    except Exception as e:
        log.error("Periodic check failed: %s", e)


@periodic_checker.before_loop
async def before_checker():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info("Discord bot logged in as %s", bot.user)

    # Sync slash commands with Discord
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash commands", len(synced))
    except Exception as e:
        log.error("Failed to sync commands: %s", e)

    # Send startup notification
    if NOTIFICATION_CHANNEL_ID:
        channel = bot.get_channel(int(NOTIFICATION_CHANNEL_ID))
        if channel:
            product_count = len(core.load_products())
            text = core.format_startup_message(product_count)
            try:
                await channel.send(text)
            except Exception as e:
                log.warning("Could not send startup message: %s", e)

    # Start periodic checker
    if not periodic_checker.is_running():
        periodic_checker.start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not DISCORD_TOKEN:
        print("Error: Set DISCORD_TOKEN environment variable.")
        print("Create a bot at https://discord.com/developers/applications")
        sys.exit(1)

    log.info("Discord bot starting...")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
