"""Slack adapter for StrawPot — relays messages to/from imu via Socket Mode."""

import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
from pathlib import Path

import httpx
import websockets
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("strawpot.slack")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL = os.environ.get("STRAWPOT_API_URL", "http://127.0.0.1:52532")
BOT_TOKEN = os.environ.get("STRAWPOT_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("STRAWPOT_APP_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))

if not BOT_TOKEN:
    logger.error("STRAWPOT_BOT_TOKEN is not set")
    sys.exit(1)
if not APP_TOKEN:
    logger.error("STRAWPOT_APP_TOKEN is not set")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Local database: (channel_id, thread_ts) → conversation_id
# ---------------------------------------------------------------------------

_DB_PATH = Path(__file__).parent / ".adapter.db"


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_conversations (
            channel_id  TEXT NOT NULL,
            thread_ts   TEXT NOT NULL,
            conv_id     INTEGER NOT NULL,
            PRIMARY KEY (channel_id, thread_ts)
        )
        """
    )
    conn.commit()
    return conn


db = _init_db()
_db_lock = threading.Lock()


def get_conv_id(channel_id: str, thread_ts: str) -> int | None:
    with _db_lock:
        row = db.execute(
            "SELECT conv_id FROM thread_conversations WHERE channel_id = ? AND thread_ts = ?",
            (channel_id, thread_ts),
        ).fetchone()
    return row[0] if row else None


def set_conv_id(channel_id: str, thread_ts: str, conv_id: int) -> None:
    with _db_lock:
        db.execute(
            "INSERT OR REPLACE INTO thread_conversations (channel_id, thread_ts, conv_id) VALUES (?, ?, ?)",
            (channel_id, thread_ts, conv_id),
        )
        db.commit()


# ---------------------------------------------------------------------------
# StrawPot API helpers
# ---------------------------------------------------------------------------


def create_conversation() -> int:
    """Create a new imu conversation and return its ID."""
    resp = httpx.post(f"{API_URL}/api/imu/conversations", timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def get_or_create_conversation(channel_id: str, thread_ts: str) -> int:
    """Get existing conversation for this thread, or create a new one."""
    conv_id = get_conv_id(channel_id, thread_ts)
    if conv_id is not None:
        return conv_id
    conv_id = create_conversation()
    set_conv_id(channel_id, thread_ts, conv_id)
    logger.info("Created conversation %d for %s/%s", conv_id, channel_id, thread_ts)
    return conv_id


def submit_task(conv_id: int, text: str) -> dict:
    """Submit a task to a conversation. Returns the response dict."""
    resp = httpx.post(
        f"{API_URL}/api/conversations/{conv_id}/tasks",
        json={"task": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_session_summary(conv_id: int, run_id: str) -> str:
    """Fetch the session summary from the conversation."""
    resp = httpx.get(f"{API_URL}/api/conversations/{conv_id}", timeout=30)
    resp.raise_for_status()
    for session in resp.json().get("sessions", []):
        if session["run_id"] == run_id:
            return session.get("summary") or f"Session {session['status']}."
    return "Session completed."


# ---------------------------------------------------------------------------
# Session monitoring
# ---------------------------------------------------------------------------


def wait_for_session(run_id: str) -> None:
    """Wait for a session to complete. Try WebSocket first, fall back to polling."""
    try:
        asyncio.run(_wait_for_session_ws(run_id))
    except Exception as exc:
        logger.warning("WebSocket monitoring failed: %s — falling back to polling", exc)
        _wait_for_session_poll(run_id)


async def _wait_for_session_ws(run_id: str) -> None:
    ws_url = API_URL.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{ws_url}/ws/sessions/{run_id}"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "init", "trace_offset": 0}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "stream_complete":
                return
            if msg.get("type") == "error":
                logger.warning("WS error: %s", msg.get("message"))
                return


def _wait_for_session_poll(run_id: str) -> None:
    terminal = {"completed", "failed", "stopped"}
    while True:
        import time
        time.sleep(POLL_INTERVAL)
        try:
            resp = httpx.get(f"{API_URL}/api/sessions/{run_id}", timeout=30)
            resp.raise_for_status()
            if resp.json().get("status") in terminal:
                return
        except Exception as exc:
            logger.warning("Poll error: %s", exc)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def chunk_message(text: str, max_len: int = 39_000) -> list[str]:
    """Split text into chunks for Slack's message limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# ---------------------------------------------------------------------------
# Slack app setup
# ---------------------------------------------------------------------------

app = App(token=BOT_TOKEN)


def _strip_mention(text: str, bot_user_id: str) -> str:
    """Remove the @mention of our bot from the message text."""
    return text.replace(f"<@{bot_user_id}>", "").strip()


@app.event("app_mention")
def handle_mention(event, say, context):
    """Handle @mention in a channel — create/continue thread conversation."""
    channel = event["channel"]
    user_text = _strip_mention(event.get("text", ""), context.get("bot_user_id", ""))
    if not user_text:
        return

    # Use existing thread or start a new one from this message
    thread_ts = event.get("thread_ts") or event["ts"]

    logger.info("Mention in %s (thread %s): %s", channel, thread_ts, user_text[:100])

    # Acknowledge
    ack_resp = say(text="On it...", thread_ts=thread_ts)
    ack_ts = ack_resp.get("ts") if isinstance(ack_resp, dict) else None

    conv_id = get_or_create_conversation(channel, thread_ts)

    try:
        result = submit_task(conv_id, user_text)
    except httpx.HTTPStatusError as exc:
        logger.error("Task submission failed: %s", exc)
        say(text="Failed to submit task. Is StrawPot GUI running?", thread_ts=thread_ts)
        return

    if result.get("queued"):
        if ack_ts:
            try:
                app.client.chat_update(channel=channel, ts=ack_ts, text="Queued — will run after the current session finishes.")
            except Exception:
                pass
        return

    run_id = result.get("run_id")
    if not run_id:
        return

    # Wait for session to complete
    wait_for_session(run_id)

    summary = get_session_summary(conv_id, run_id)

    # Delete "On it..." and post summary
    if ack_ts:
        try:
            app.client.chat_delete(channel=channel, ts=ack_ts)
        except Exception:
            pass

    for chunk in chunk_message(summary):
        say(text=chunk, thread_ts=thread_ts)


@app.event("message")
def handle_dm(event, say, context):
    """Handle DM messages."""
    # Only process DMs (im channel type)
    channel_type = event.get("channel_type")
    if channel_type != "im":
        return

    # Ignore bot messages, message_changed, etc.
    if event.get("subtype"):
        return

    text = event.get("text", "").strip()
    if not text:
        return

    channel = event["channel"]

    # Handle /new command in DMs
    if text.lower() == "/new":
        conv_id = create_conversation()
        set_conv_id(channel, "dm", conv_id)
        logger.info("New DM conversation %d for %s", conv_id, channel)
        say(text="New conversation started.", channel=channel)
        return

    logger.info("DM from %s: %s", channel, text[:100])

    # DMs use a single thread key "dm" per channel
    ack_resp = say(text="On it...", channel=channel)
    ack_ts = ack_resp.get("ts") if isinstance(ack_resp, dict) else None

    conv_id = get_or_create_conversation(channel, "dm")

    try:
        result = submit_task(conv_id, text)
    except httpx.HTTPStatusError as exc:
        logger.error("Task submission failed: %s", exc)
        say(text="Failed to submit task. Is StrawPot GUI running?", channel=channel)
        return

    if result.get("queued"):
        if ack_ts:
            try:
                app.client.chat_update(channel=channel, ts=ack_ts, text="Queued — will run after the current session finishes.")
            except Exception:
                pass
        return

    run_id = result.get("run_id")
    if not run_id:
        return

    wait_for_session(run_id)

    summary = get_session_summary(conv_id, run_id)

    if ack_ts:
        try:
            app.client.chat_delete(channel=channel, ts=ack_ts)
        except Exception:
            pass

    for chunk in chunk_message(summary):
        say(text=chunk, channel=channel)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    logger.info("Starting Slack adapter (Socket Mode)")
    logger.info("API URL: %s", API_URL)

    handler = SocketModeHandler(app, APP_TOKEN)

    def handle_sigterm(signum, frame):
        logger.info("Received SIGTERM, shutting down...")
        handler.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    logger.info("Bot is running — connected via Socket Mode")
    handler.start()


if __name__ == "__main__":
    main()
