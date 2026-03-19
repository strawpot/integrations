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

# Suppress noisy httpx request logging (pollers every few seconds)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL = os.environ.get("STRAWPOT_API_URL", "http://127.0.0.1:52532")
BOT_TOKEN = os.environ.get("STRAWPOT_BOT_TOKEN", "")
PROJECT_ID: int | None = (
    int(os.environ["STRAWPOT_PROJECT_ID"])
    if os.environ.get("STRAWPOT_PROJECT_ID")
    else None
)
# Max chars per Discord message (leave room under 2000 limit)
DC_MAX_LEN = int(os.environ.get("DC_MAX_LEN", "1900"))
# How often to poll for session status if WebSocket fails (seconds)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))
# How often to poll conversations for new sessions from other sources (seconds)
CONV_POLL_INTERVAL = int(os.environ.get("CONV_POLL_INTERVAL", "10"))
# How often to poll for pending notifications to deliver (seconds)
NOTIFY_POLL_INTERVAL = int(os.environ.get("NOTIFY_POLL_INTERVAL", "5"))
# Integration name — passed by StrawPot GUI at launch
INTEGRATION_NAME = os.environ.get("STRAWPOT_INTEGRATION_NAME", "discord")

if not BOT_TOKEN:
    logger.error("STRAWPOT_BOT_TOKEN is not set")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Local database: (channel_id, thread_id) → conversation_id
# ---------------------------------------------------------------------------

# Persistent data directory provided by StrawPot GUI (survives reinstalls).
# Falls back to current directory if not set (e.g. running standalone).
_DATA_DIR = Path(os.environ.get("STRAWPOT_DATA_DIR") or str(Path(__file__).parent))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = _DATA_DIR / "adapter.db"


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_conversations (
            channel_id        TEXT NOT NULL,
            thread_id         TEXT NOT NULL,
            conv_id           INTEGER NOT NULL,
            last_session_id   TEXT,
            PRIMARY KEY (channel_id, thread_id)
        )
        """
    )
    # Migration: add last_session_id for existing databases
    try:
        conn.execute(
            "ALTER TABLE thread_conversations ADD COLUMN last_session_id TEXT"
        )
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migration: add latest_thread_id for channel-level conversation mapping
    try:
        conn.execute(
            "ALTER TABLE thread_conversations ADD COLUMN latest_thread_id TEXT"
        )
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Map run_id → thread for routing poller responses to the correct thread
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_threads (
            run_id      TEXT PRIMARY KEY,
            channel_id  TEXT NOT NULL,
            thread_id   TEXT NOT NULL
        )
        """
    )
    # Pending replies: track thread_id for queued tasks (no run_id yet)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_replies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id  TEXT NOT NULL,
            thread_id   TEXT NOT NULL,
            ack_msg_id  TEXT,
            run_id      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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


def clear_conv_id(channel_id: str, thread_id: str) -> None:
    """Remove the conversation mapping for a thread (e.g. after deletion)."""
    db.execute(
        "DELETE FROM thread_conversations WHERE channel_id = ? AND thread_id = ?",
        (channel_id, thread_id),
    )
    db.commit()


def update_last_session_id(channel_id: str, thread_id: str, run_id: str) -> None:
    """Update the last seen session ID for a thread."""
    db.execute(
        "UPDATE thread_conversations SET last_session_id = ? "
        "WHERE channel_id = ? AND thread_id = ?",
        (run_id, channel_id, thread_id),
    )
    db.commit()


def _update_latest_thread(channel_id: str, thread_id: str) -> None:
    """Track the latest thread ID for a channel conversation (for reply targeting)."""
    db.execute(
        "UPDATE thread_conversations SET latest_thread_id = ? "
        "WHERE channel_id = ? AND thread_id = 'channel'",
        (thread_id, channel_id),
    )
    db.commit()


def _set_session_thread(run_id: str, channel_id: str, thread_id: str) -> None:
    """Record which thread a session was initiated from."""
    db.execute(
        "INSERT OR REPLACE INTO session_threads (run_id, channel_id, thread_id) VALUES (?, ?, ?)",
        (run_id, channel_id, thread_id),
    )
    db.commit()


def _get_session_thread(run_id: str) -> tuple[str, str] | None:
    """Get the (channel_id, thread_id) for a session."""
    row = db.execute(
        "SELECT channel_id, thread_id FROM session_threads WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return (row[0], row[1]) if row else None


def _delete_session_thread(run_id: str) -> None:
    """Clean up session thread mapping after delivery."""
    db.execute("DELETE FROM session_threads WHERE run_id = ?", (run_id,))
    db.commit()


def _add_pending_reply(channel_id: str, thread_id: str, ack_msg_id: str | None) -> None:
    """Record a pending reply for a queued task."""
    db.execute(
        "INSERT INTO pending_replies (channel_id, thread_id, ack_msg_id) VALUES (?, ?, ?)",
        (channel_id, thread_id, ack_msg_id),
    )
    db.commit()


def _assign_pending_reply(channel_id: str, run_id: str) -> bool:
    """Assign a run_id to the oldest unassigned pending reply. Returns True if assigned."""
    row = db.execute(
        "SELECT id FROM pending_replies "
        "WHERE channel_id = ? AND run_id IS NULL ORDER BY id ASC LIMIT 1",
        (channel_id,),
    ).fetchone()
    if not row:
        return False
    db.execute(
        "UPDATE pending_replies SET run_id = ? WHERE id = ?",
        (run_id, row[0]),
    )
    db.commit()
    return True


def _pop_pending_reply_by_run_id(run_id: str) -> tuple[str, str, str | None] | None:
    """Pop a pending reply by run_id. Returns (channel_id, thread_id, ack_msg_id) or None."""
    row = db.execute(
        "SELECT id, channel_id, thread_id, ack_msg_id FROM pending_replies WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if not row:
        return None
    db.execute("DELETE FROM pending_replies WHERE id = ?", (row[0],))
    db.commit()
    return (row[1], row[2], row[3])


# ---------------------------------------------------------------------------
# StrawPot API helpers
# ---------------------------------------------------------------------------


async def create_conversation(client: httpx.AsyncClient) -> int:
    """Create a conversation and return its ID.

    When running project-scoped (STRAWPOT_PROJECT_ID is set), creates in that
    project via the general conversations endpoint.  Otherwise falls back to
    the imu-specific endpoint (project_id=0).
    """
    if PROJECT_ID is not None:
        resp = await client.post(
            f"{API_URL}/api/conversations",
            json={"project_id": PROJECT_ID},
        )
    else:
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
# Conversation poller — detects sessions from other sources (scheduler, GUI)
# ---------------------------------------------------------------------------


async def conversation_poller() -> None:
    """Poll watched conversations and relay new completed sessions to Discord."""
    await bot.wait_until_ready()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while not bot.is_closed():
                try:
                    rows = db.execute(
                        "SELECT channel_id, thread_id, conv_id, last_session_id, latest_thread_id "
                        "FROM thread_conversations"
                    ).fetchall()
                    for channel_id, thread_id, conv_id, last_seen, latest_thread in rows:
                        try:
                            resp = await client.get(
                                f"{API_URL}/api/conversations/{conv_id}"
                            )
                            if resp.status_code != 200:
                                continue
                            sessions = resp.json().get("sessions", [])
                            if not sessions:
                                continue

                            # If no marker yet, initialize to latest without delivering
                            if last_seen is None:
                                update_last_session_id(
                                    channel_id, thread_id, sessions[-1]["run_id"]
                                )
                                continue

                            # Collect new sessions after the marker
                            found_marker = False
                            new_sessions = []
                            for session in sessions:
                                if not found_marker:
                                    if session["run_id"] == last_seen:
                                        found_marker = True
                                    continue
                                new_sessions.append(session)

                            # Pass 1: assign pending_replies to running sessions
                            if thread_id == "channel":
                                for session in new_sessions:
                                    if session["status"] == "running":
                                        if not _get_session_thread(session["run_id"]):
                                            _assign_pending_reply(
                                                channel_id, session["run_id"]
                                            )

                            # Pass 2: deliver completed sessions
                            for session in new_sessions:
                                if session["status"] not in (
                                    "completed",
                                    "failed",
                                    "stopped",
                                ):
                                    continue
                                summary = (
                                    session.get("summary")
                                    or f"Session {session['status']}."
                                )
                                # Resolve destination: session_threads → assigned pending_reply → latest_thread → channel
                                dest_id = None
                                ack_msg_id = None
                                if thread_id == "dm":
                                    dest_id = int(channel_id)
                                elif thread_id == "channel":
                                    st = _get_session_thread(session["run_id"])
                                    if st:
                                        dest_id = int(st[1])
                                        _delete_session_thread(session["run_id"])
                                    else:
                                        pr = _pop_pending_reply_by_run_id(
                                            session["run_id"]
                                        )
                                        if pr:
                                            dest_id = int(pr[1])
                                            ack_msg_id = pr[2]
                                        elif latest_thread:
                                            dest_id = int(latest_thread)
                                        else:
                                            dest_id = int(channel_id)
                                else:
                                    dest_id = int(thread_id)
                                dest = bot.get_channel(dest_id)
                                if dest is None:
                                    try:
                                        dest = await bot.fetch_channel(dest_id)
                                    except Exception:
                                        continue
                                # Delete queued-task ack message if present
                                if ack_msg_id:
                                    try:
                                        ack_msg = await dest.fetch_message(int(ack_msg_id))
                                        await ack_msg.delete()
                                    except Exception:
                                        pass
                                for chunk_text in chunk_message(summary):
                                    await dest.send(chunk_text)
                                update_last_session_id(
                                    channel_id,
                                    thread_id,
                                    session["run_id"],
                                )
                        except Exception:
                            logger.debug("Poller error for conv %d", conv_id)
                except Exception:
                    logger.exception("Conversation poller error")
                await asyncio.sleep(CONV_POLL_INTERVAL)
    except asyncio.CancelledError:
        logger.debug("Conversation poller cancelled")


# ---------------------------------------------------------------------------
# Notification poller — delivers messages pushed via the notify API
# ---------------------------------------------------------------------------


async def notification_poller() -> None:
    """Poll for pending notifications and deliver them to Discord channels."""
    await bot.wait_until_ready()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while not bot.is_closed():
                try:
                    resp = await client.get(
                        f"{API_URL}/api/integrations/{INTEGRATION_NAME}/notifications"
                    )
                    if resp.status_code == 200:
                        for item in resp.json():
                            chat_id = item.get("chat_id")
                            message = item.get("message", "")
                            nid = item["id"]
                            if not chat_id:
                                logger.warning("Notification %d has no chat_id, skipping", nid)
                                continue
                            try:
                                dest = bot.get_channel(int(chat_id))
                                if dest is None:
                                    dest = await bot.fetch_channel(int(chat_id))
                                for chunk_text in chunk_message(message):
                                    await dest.send(chunk_text)
                                # ACK after successful delivery
                                await client.post(
                                    f"{API_URL}/api/integrations/{INTEGRATION_NAME}"
                                    f"/notifications/{nid}/ack"
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to deliver notification %d to channel %s",
                                    nid, chat_id,
                                )
                except Exception:
                    logger.debug("Notification poller error")
                await asyncio.sleep(NOTIFY_POLL_INTERVAL)
    except asyncio.CancelledError:
        logger.debug("Notification poller cancelled")


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
    """Handle @mention in a channel — all mentions in the same channel share one conversation."""
    # Create a new thread from this message (for UX)
    thread = await message.create_thread(
        name=text[:97] + "..." if len(text) > 100 else text,
        auto_archive_duration=1440,  # 24 hours
    )

    ack = await thread.send("On it...")

    channel_id = str(message.channel.id)

    async with httpx.AsyncClient(timeout=30) as client:
        # Map the entire channel to one conversation
        conv_id = await get_or_create_conversation(client, channel_id, "channel")

        try:
            result = await submit_task(client, conv_id, text)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("Conversation %d deleted, creating new one for %s", conv_id, channel_id)
                clear_conv_id(channel_id, "channel")
                conv_id = await get_or_create_conversation(client, channel_id, "channel")
                try:
                    result = await submit_task(client, conv_id, text)
                except httpx.HTTPStatusError as exc2:
                    logger.error("Task submission failed after retry: %s", exc2)
                    await ack.edit(content="Failed to submit task. Is StrawPot GUI running?")
                    return
            else:
                logger.error("Task submission failed: %s", exc)
                await ack.edit(content="Failed to submit task. Is StrawPot GUI running?")
                return

        if result.get("queued"):
            await ack.edit(content="Queued — will run after the current session finishes.")
            _add_pending_reply(channel_id, str(thread.id), str(ack.id))
            return

        run_id = result.get("run_id")
        if not run_id:
            await ack.edit(content="Task submitted.")
            return

    # Record which thread this session belongs to
    _set_session_thread(run_id, channel_id, str(thread.id))

    # Wait for session to complete
    await wait_for_session_ws(run_id)

    # Update marker and track latest thread for poller replies
    update_last_session_id(channel_id, "channel", run_id)
    _update_latest_thread(channel_id, str(thread.id))
    _delete_session_thread(run_id)

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
    """Handle reply in an existing thread — continue the channel conversation."""
    thread = message.channel
    channel_id = str(thread.parent_id)

    ack = await message.reply("On it...")

    async with httpx.AsyncClient(timeout=30) as client:
        # Use channel-level conversation (same as mentions)
        conv_id = await get_or_create_conversation(client, channel_id, "channel")

        try:
            result = await submit_task(client, conv_id, text)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("Conversation %d deleted, creating new one for %s", conv_id, channel_id)
                clear_conv_id(channel_id, "channel")
                conv_id = await get_or_create_conversation(client, channel_id, "channel")
                try:
                    result = await submit_task(client, conv_id, text)
                except httpx.HTTPStatusError as exc2:
                    logger.error("Task submission failed after retry: %s", exc2)
                    await ack.edit(content="Failed to submit task. Is StrawPot GUI running?")
                    return
            else:
                logger.error("Task submission failed: %s", exc)
                await ack.edit(content="Failed to submit task. Is StrawPot GUI running?")
                return

        if result.get("queued"):
            await ack.edit(content="Queued — will run after the current session finishes.")
            _add_pending_reply(channel_id, str(thread.id), str(ack.id))
            return

        run_id = result.get("run_id")
        if not run_id:
            await ack.edit(content="Task submitted.")
            return

    _set_session_thread(run_id, channel_id, str(thread.id))
    await wait_for_session_ws(run_id)

    update_last_session_id(channel_id, "channel", run_id)
    _update_latest_thread(channel_id, str(thread.id))
    _delete_session_thread(run_id)

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
            if exc.response.status_code == 404:
                logger.warning("Conversation %d deleted, creating new one for %s/dm", conv_id, channel_id)
                clear_conv_id(channel_id, "dm")
                conv_id = await get_or_create_conversation(client, channel_id, "dm")
                try:
                    result = await submit_task(client, conv_id, text)
                except httpx.HTTPStatusError as exc2:
                    logger.error("Task submission failed after retry: %s", exc2)
                    await ack.edit(content="Failed to submit task. Is StrawPot GUI running?")
                    return
            else:
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

    # Update marker immediately so the conversation poller skips this session
    update_last_session_id(channel_id, "dm", run_id)

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
    bot.loop.create_task(conversation_poller())
    bot.loop.create_task(notification_poller())
    logger.info("Conversation and notification pollers started")


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
    if PROJECT_ID is not None:
        logger.info("Project-scoped: project_id=%d", PROJECT_ID)

    def handle_sigterm(signum, frame):
        logger.info("Received SIGTERM, shutting down...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_sigterm)

    logger.info("Bot is running — connecting via Gateway WebSocket")
    bot.run(BOT_TOKEN, log_handler=None)
    logger.info("Bot stopped")


if __name__ == "__main__":
    main()
