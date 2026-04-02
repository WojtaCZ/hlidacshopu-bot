FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core.py telegram_bot.py discord_bot.py ./
VOLUME /data
CMD ["sh", "-c", "python ${BOT_PLATFORM:-telegram}_bot.py"]
