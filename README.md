# Hlídač Shopů Telegram Bot

A Telegram bot that monitors product prices on Czech and Slovak e-shops using the [Hlídač Shopů](https://www.hlidacshopu.cz/) API. It checks prices hourly and sends you a message when a price drops or hits an all-time low.

Two-way communication — add and remove products, set per-product alert thresholds, and check prices on demand, all from within Telegram.

## Features

- **Price drop alerts** with absolute and percentage values
- **All-time low detection** from the full price history
- **Per-product drop thresholds** — only get notified when the drop matters to you
- **50+ supported shops** — Alza, Mall, Datart, Notino, Rohlik, Kaufland, IKEA, and many more
- **Startup notification** so you know the bot is running
- **Persistent storage** — tracked products survive restarts

## Bot commands

| Command | Description |
|---|---|
| `/add <url> [drop%]` | Add a product to monitor. Optionally set a minimum drop % to trigger alerts. |
| `/remove <number>` | Remove a product by its number (see `/list`). |
| `/list` | Show all monitored products with current price, all-time low, and threshold. |
| `/set <number> <drop%>` | Change the drop threshold for an existing product. |
| `/check` | Force an immediate price check on all products. |
| `/help` | Show available commands. |

You can also just paste a product URL directly (with an optional threshold) and the bot will add it automatically.

## Setup

### 1. Create a Telegram bot

Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, pick a name, and copy the API token.

### 2. Configure

Copy the example env file and fill in your token:

```bash
cp .env.example .env
```

```env
TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
```

### 3. Run with Docker

```bash
docker compose up -d
```

That's it. The bot will start polling for messages and checking prices every hour.

### 4. Lock down access (optional)

Message your bot `/start` — it will reply with your chat ID. Add it to `.env` to restrict the bot to only you:

```env
ALLOWED_CHAT_IDS=123456789
```

Then restart:

```bash
docker compose up -d
```

## Configuration

All configuration is done through environment variables (set them in `.env`):

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | *(required)* | Bot token from @BotFather |
| `ALLOWED_CHAT_IDS` | *(empty)* | Comma-separated chat IDs that can use the bot. Empty = no restriction. |
| `CHECK_INTERVAL` | `3600` | Price check interval in seconds (3600 = 1 hour) |
| `DROP_THRESHOLD` | `0` | Default minimum price drop % to trigger a notification. Can be overridden per product. |

## Notifications

When a price drops below your threshold, you get a message like:

```
Price drop: Samsung Galaxy S24 Ultra

31990 -> 28990 CZK
-3000 CZK (9.4% off)

Real discount: 8%

https://www.alza.cz/samsung-galaxy-s24-ultra-d12345.htm
```

When the price hits the lowest point in the entire tracked history:

```
ALL-TIME LOW: Samsung Galaxy S24 Ultra

31990 -> 27490 CZK
-4500 CZK (14.1% off)

https://www.alza.cz/samsung-galaxy-s24-ultra-d12345.htm
```

## Data

Product data is stored in a Docker volume (`bot-data`). To back it up:

```bash
docker cp hlidac-bot:/data/products.json ./products-backup.json
```

## License

MIT
