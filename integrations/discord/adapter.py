"""Discord adapter for StrawPot — relays messages to/from imu via Gateway WebSocket."""

import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path

import discord
import httpx
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("strawpot.discord")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL = os.environ.get("STRAWPOT_API_URL", "http://127.0.0.1:52532")
BOT_TOKEN = os.environ.get("STRAWPOT_BOT_TOKEN", "")
# Max chars per Discord message (leave room under 2000 limit)
DC_MAX_LEN = int(os.environ.get("DC_MAX_LEN", "1900"))
# How often to poll for session status if WebSocket fails (seconds)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))

if not BOT_TOKEN:
    logger.error("STRAWPOT_BOT_TOKEN is not set")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Local database: (channel_id, thread_id) → conversation_id
# ---------------------------------------------------------------------------

_DB_PATH = Path(__file__).parent / ".adapter.db"


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_conversations (
            channel_id  TEXT NOT NULL,
            thread_id   TEXT NOT NULL,
            conv_id     INTEGER NOT NULL,
            PRIMARY KEY (channel_id, thread_id)
        )
        """
    )
    conn.commit()
    return conn


db = _init_db()


def get_conv_id(channel_id: str, thread_id: str) -> int | None:
    row = db.execute(
        "SELECT conv_id FROM thread_conversations WHERE channel_id = ? AND thread_id = ?",
        (channel_id, thread_id),
    ).fetchone()
    return row[0] if row else None


def set_conv_id(channel_id: str, thread_id: str, conv_id: int) -> None:
    db.execute(
        "INSERT OR REPLACE INTO thread_conversations (channel_id, thread_id, conv_id) VALUES (?, ?, ?)",
        (channel_id, thread_id, conv_id),
    )
    db.commit()


# ---------------------------------------------------------------------------
# StrawPot API helpers
# ---------------------------------------------------------------------------


async def create_conversation(client: httpx.AsyncClient) -> int:
    """Create a new imu conversation and return its ID."""
    resp = await client.post(f"{API_URL}/api/imu/conversations")
    resp.raise_for_status()
    return resp.json()["id"]


async def get_or_create_conversation(
    client: httpx.AsyncClient, channel_id: str, thread_id: str
) -> int:
    """Get existing conversation for this thread, or create a new one."""
    conv_id = get_conv_id(channel_id, thread_id)
    if conv_id is not None:
        return conv_id
    conv_id = await create_conversation(client)
    set_conv_id(channel_id, thread_id, conv_id)
    logger.info("Created conversation %d for %s/%s", conv_id, channel_id, thread_id)
    return conv_id


async def submit_task(
    client: httpx.AsyncClient, conv_id: int, text: str
) -> dict:
    """Submit a task to a conversation. Returns the response dict."""
    resp = await client.post(
        f"{API_URL}/api/conversations/{conv_id}/tasks",
        json={"task": text},
    )
    resp.raise_for_status()
    return resp.json()


async def get_session_summary(
    client: httpx.AsyncClient, conv_id: int, run_id: str
) -> str:
    """Fetch the session summary from the conversation."""
    resp = await client.get(f"{API_URL}/api/conversations/{conv_id}")
    resp.raise_for_status()
    for session in resp.json().get("sessions", []):
        if session["run_id"] == run_id:
            return session.get("summary") or f"Session {session['status']}."
    return "Session completed."


# ---------------------------------------------------------------------------
# Session monitoring
# ---------------------------------------------------------------------------


async def wait_for_session_ws(run_id: str) -> None:
    """Wait for a session to complete via WebSocket."""
    ws_url = API_URL.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{ws_url}/ws/sessions/{run_id}"
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "init", "trace_offset": 0}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "stream_complete":
                    return
                if msg.get("type") == "error":
                    logger.warning("WS error: %s", msg.get("message"))
                    return
    except Exception as exc:
        logger.warning("WebSocket monitoring failed: %s — falling back to polling", exc)
        await wait_for_session_poll(run_id)


async def wait_for_session_poll(run_id: str) -> None:
    """Fallback: poll session status until terminal."""
    terminal = {"completed", "failed", "stopped"}
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                resp = await client.get(f"{API_URL}/api/sessions/{run_id}")
                resp.raise_for_status()
                if resp.json().get("status") in terminal:
                    return
            except Exception as exc:
                logger.warning("Poll error: %s", exc)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def chunk_message(text: str) -> list[str]:
    """Split text into chunks that fit Discord's 2000-char message limit."""
    if len(text) <= DC_MAX_LEN:
        return [text]
    chunks = []
    while text:
        if len(text) <= DC_MAX_LEN:
            chunks.append(text)
            break
        # Try to break at a newline
        cut = text.rfind("\n", 0, DC_MAX_LEN)
        if cut < DC_MAX_LEN // 2:
            cut = DC_MAX_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


def _strip_mention(text: str) -> str:
    """Remove the @mention of our bot from the message text."""
    return text.replace(f"<@{bot.user.id}>", "").strip() if bot.user else text.strip()


async def _handle_channel_mention(message: discord.Message, text: str) -> None:
    """Handle @mention in a channel — create thread and process task."""
    # Create a new thread from this message
    thread = await message.create_thread(
        name=text[:97] + "..." if len(text) > 100 else text,
        auto_archive_duration=1440,  # 24 hours
    )

    ack = await thread.send("On it...")

    channel_id = str(message.channel.id)
    thread_id = str(thread.id)

    async with httpx.AsyncClient(timeout=30) as client:
        conv_id = await get_or_create_conversation(client, channel_id, thread_id)

        try:
            result = await submit_task(client, conv_id, text)
        except httpx.HTTPStatusError as exc:
            logger.error("Task submission failed: %s", exc)
            await ack.edit(content="Failed to submit task. Is StrawPot GUI running?")
            return

        if result.get("queued"):
            await ack.edit(content="Queued — will run after the current session finishes.")
            return

        run_id = result.get("run_id")
        if not run_id:
            await ack.edit(content="Task submitted.")
            return

    # Wait for session to complete
    await wait_for_session_ws(run_id)

    async with httpx.AsyncClient(timeout=30) as client:
        summary = await get_session_summary(client, conv_id, run_id)

    # Delete "On it..." and post summary
    try:
        await ack.delete()
    except Exception:
        pass

    for chunk in chunk_message(summary):
        await thread.send(chunk)


async def _handle_thread_reply(message: discord.Message, text: str) -> None:
    """Handle reply in an existing thread — continue conversation."""
    thread = message.channel
    channel_id = str(thread.parent_id)
    thread_id = str(thread.id)

    ack = await message.reply("On it...")

    async with httpx.AsyncClient(timeout=30) as client:
        conv_id = await get_or_create_conversation(client, channel_id, thread_id)

        try:
            result = await submit_task(client, conv_id, text)
        except httpx.HTTPStatusError as exc:
            logger.error("Task submission failed: %s", exc)
            await ack.edit(content="Failed to submit task. Is StrawPot GUI running?")
            return

        if result.get("queued"):
            await ack.edit(content="Queued — will run after the current session finishes.")
            return

        run_id = result.get("run_id")
        if not run_id:
            await ack.edit(content="Task submitted.")
            return

    await wait_for_session_ws(run_id)

    async with httpx.AsyncClient(timeout=30) as client:
        summary = await get_session_summary(client, conv_id, run_id)

    try:
        await ack.delete()
    except Exception:
        pass

    for chunk in chunk_message(summary):
        await thread.send(chunk)


async def _handle_dm(message: discord.Message) -> None:
    """Handle DM messages."""
    text = message.content.strip()
    if not text:
        return

    channel_id = str(message.channel.id)

    # Handle !new command
    if text.lower() == "!new":
        async with httpx.AsyncClient(timeout=30) as client:
            conv_id = await create_conversation(client)
        set_conv_id(channel_id, "dm", conv_id)
        logger.info("New DM conversation %d for %s", conv_id, channel_id)
        await message.reply("New conversation started.")
        return

    logger.info("DM from %s: %s", channel_id, text[:100])

    ack = await message.reply("On it...")

    async with httpx.AsyncClient(timeout=30) as client:
        conv_id = await get_or_create_conversation(client, channel_id, "dm")

        try:
            result = await submit_task(client, conv_id, text)
        except httpx.HTTPStatusError as exc:
            logger.error("Task submission failed: %s", exc)
            await ack.edit(content="Failed to submit task. Is StrawPot GUI running?")
            return

        if result.get("queued"):
            await ack.edit(content="Queued — will run after the current session finishes.")
            return

        run_id = result.get("run_id")
        if not run_id:
            await ack.edit(content="Task submitted.")
            return

    await wait_for_session_ws(run_id)

    async with httpx.AsyncClient(timeout=30) as client:
        summary = await get_session_summary(client, conv_id, run_id)

    try:
        await ack.delete()
    except Exception:
        pass

    for chunk in chunk_message(summary):
        await message.channel.send(chunk)


@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user.name, bot.user.id)


@bot.event
async def on_message(message: discord.Message):
    # Ignore own messages
    if message.author == bot.user:
        return

    # DMs
    if isinstance(message.channel, discord.DMChannel):
        await _handle_dm(message)
        return

    # Thread reply (bot is mentioned or replying in a thread the bot created)
    if isinstance(message.channel, discord.Thread):
        text = _strip_mention(message.content)
        if text:
            await _handle_thread_reply(message, text)
        return

    # Channel mention — check if bot is mentioned
    if bot.user and bot.user.mentioned_in(message):
        text = _strip_mention(message.content)
        if text:
            logger.info("Mention in %s: %s", message.channel.id, text[:100])
            await _handle_channel_mention(message, text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    logger.info("Starting Discord adapter")
    logger.info("API URL: %s", API_URL)

    loop = asyncio.new_event_loop()

    def handle_sigterm(signum, frame):
        logger.info("Received SIGTERM, shutting down...")
        loop.create_task(bot.close())

    signal.signal(signal.SIGTERM, handle_sigterm)

    logger.info("Bot is running — connecting via Gateway WebSocket")
    bot.run(BOT_TOKEN, log_handler=None)
    logger.info("Bot stopped")


if __name__ == "__main__":
    main()
