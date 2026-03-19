"""Slack adapter for StrawPot — relays messages to/from imu via Socket Mode."""

import asyncio
import json
import logging
import os
import re
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

# Suppress noisy httpx request logging (notification poller every 5s)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL = os.environ.get("STRAWPOT_API_URL", "http://127.0.0.1:52532")
BOT_TOKEN = os.environ.get("STRAWPOT_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("STRAWPOT_APP_TOKEN", "")
PROJECT_ID: int | None = (
    int(os.environ["STRAWPOT_PROJECT_ID"])
    if os.environ.get("STRAWPOT_PROJECT_ID")
    else None
)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))
# How often to poll conversations for new sessions from other sources (seconds)
CONV_POLL_INTERVAL = int(os.environ.get("CONV_POLL_INTERVAL", "10"))
# How often to poll for pending notifications to deliver (seconds)
NOTIFY_POLL_INTERVAL = int(os.environ.get("NOTIFY_POLL_INTERVAL", "5"))
# Integration name — passed by StrawPot GUI at launch
INTEGRATION_NAME = os.environ.get("STRAWPOT_INTEGRATION_NAME", "slack")

if not BOT_TOKEN:
    logger.error("STRAWPOT_BOT_TOKEN is not set")
    sys.exit(1)
if not APP_TOKEN:
    logger.error("STRAWPOT_APP_TOKEN is not set")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Local database: (channel_id, thread_ts) → conversation_id
# ---------------------------------------------------------------------------

# Persistent data directory provided by StrawPot GUI (survives reinstalls).
# Falls back to current directory if not set (e.g. running standalone).
_DATA_DIR = Path(os.environ.get("STRAWPOT_DATA_DIR") or str(Path(__file__).parent))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = _DATA_DIR / "adapter.db"


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_conversations (
            channel_id        TEXT NOT NULL,
            thread_ts         TEXT NOT NULL,
            conv_id           INTEGER NOT NULL,
            last_session_id   TEXT,
            PRIMARY KEY (channel_id, thread_ts)
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
    # Migration: add latest_thread_ts for channel-level conversation mapping
    try:
        conn.execute(
            "ALTER TABLE thread_conversations ADD COLUMN latest_thread_ts TEXT"
        )
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Map run_id → thread for routing poller responses to the correct thread
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_threads (
            run_id      TEXT PRIMARY KEY,
            channel_id  TEXT NOT NULL,
            thread_ts   TEXT NOT NULL
        )
        """
    )
    # Pending replies: track thread_ts for queued tasks (no run_id yet)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_replies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id  TEXT NOT NULL,
            thread_ts   TEXT NOT NULL,
            ack_ts      TEXT,
            run_id      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Migration: add run_id column for existing databases
    try:
        conn.execute("ALTER TABLE pending_replies ADD COLUMN run_id TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
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


def clear_conv_id(channel_id: str, thread_ts: str) -> None:
    """Remove the conversation mapping for a thread (e.g. after deletion)."""
    with _db_lock:
        db.execute(
            "DELETE FROM thread_conversations WHERE channel_id = ? AND thread_ts = ?",
            (channel_id, thread_ts),
        )
        db.commit()


def update_last_session_id(channel_id: str, thread_ts: str, run_id: str) -> None:
    """Update the last seen session ID for a thread."""
    with _db_lock:
        db.execute(
            "UPDATE thread_conversations SET last_session_id = ? "
            "WHERE channel_id = ? AND thread_ts = ?",
            (run_id, channel_id, thread_ts),
        )
        db.commit()


def _update_thread_ts(channel_id: str, thread_ts: str) -> None:
    """Track the latest thread_ts for a channel conversation (for reply targeting)."""
    with _db_lock:
        db.execute(
            "UPDATE thread_conversations SET latest_thread_ts = ? "
            "WHERE channel_id = ? AND thread_ts = 'channel'",
            (thread_ts, channel_id),
        )
        db.commit()


def _get_latest_thread_ts(channel_id: str) -> str | None:
    """Get the latest thread_ts for reply targeting."""
    with _db_lock:
        row = db.execute(
            "SELECT latest_thread_ts FROM thread_conversations "
            "WHERE channel_id = ? AND thread_ts = 'channel'",
            (channel_id,),
        ).fetchone()
    return row[0] if row else None


def _set_session_thread(run_id: str, channel_id: str, thread_ts: str) -> None:
    """Record which thread a session was initiated from."""
    with _db_lock:
        db.execute(
            "INSERT OR REPLACE INTO session_threads (run_id, channel_id, thread_ts) VALUES (?, ?, ?)",
            (run_id, channel_id, thread_ts),
        )
        db.commit()


def _get_session_thread(run_id: str) -> tuple[str, str] | None:
    """Get the (channel_id, thread_ts) for a session."""
    with _db_lock:
        row = db.execute(
            "SELECT channel_id, thread_ts FROM session_threads WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def _delete_session_thread(run_id: str) -> None:
    """Clean up session thread mapping after delivery."""
    with _db_lock:
        db.execute("DELETE FROM session_threads WHERE run_id = ?", (run_id,))
        db.commit()


def _add_pending_reply(channel_id: str, thread_ts: str, ack_ts: str | None) -> None:
    """Record a pending reply for a queued task."""
    with _db_lock:
        db.execute(
            "INSERT INTO pending_replies (channel_id, thread_ts, ack_ts) VALUES (?, ?, ?)",
            (channel_id, thread_ts, ack_ts),
        )
        db.commit()


def _assign_pending_reply(channel_id: str, run_id: str) -> bool:
    """Assign a run_id to the oldest unassigned pending reply. Returns True if assigned."""
    with _db_lock:
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
    """Pop a pending reply by run_id. Returns (channel_id, thread_ts, ack_ts) or None."""
    with _db_lock:
        row = db.execute(
            "SELECT id, channel_id, thread_ts, ack_ts FROM pending_replies WHERE run_id = ?",
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


def create_conversation() -> int:
    """Create a conversation and return its ID.

    When running project-scoped (STRAWPOT_PROJECT_ID is set), creates in that
    project via the general conversations endpoint.  Otherwise falls back to
    the imu-specific endpoint (project_id=0).
    """
    if PROJECT_ID is not None:
        resp = httpx.post(
            f"{API_URL}/api/conversations",
            json={"project_id": PROJECT_ID},
            timeout=30,
        )
    else:
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


def md_to_slack_mrkdwn(text: str) -> str:
    """Convert common Markdown to Slack mrkdwn format.

    Handles: fenced code blocks, inline code, bold, italic, strikethrough,
    headings, links, and bullet lists.
    """
    # Fenced code blocks: ```lang\n...\n``` — Slack supports these natively
    # but strip the language hint since Slack ignores it
    text = re.sub(
        r"```\w*\n(.*?)```",
        lambda m: f"```\n{m.group(1)}```",
        text,
        flags=re.DOTALL,
    )

    # Protect inline code from further transformations
    code_spans: list[str] = []

    def _save_code(m: re.Match) -> str:
        code_spans.append(m.group(0))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    # Protect fenced blocks
    text = re.sub(r"```.*?```", _save_code, text, flags=re.DOTALL)
    # Protect inline code
    text = re.sub(r"`[^`]+`", _save_code, text)

    # Bold: **text** or __text__ → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)

    # Italic: _text_ → _text_ (already Slack-compatible)
    # Single *text* that wasn't **bold** → _text_ for italic
    # (skip this to avoid conflicts with Slack's *bold*)

    # Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Headings: ### Title → *Title*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # Bullet lists: leading "- " or "* " → "• "
    text = re.sub(r"^[\-\*]\s+", "• ", text, flags=re.MULTILINE)

    # Restore code spans
    for i, code in enumerate(code_spans):
        text = text.replace(f"\x00CODE{i}\x00", code)

    return text


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
# Conversation poller — detects sessions from other sources (scheduler, GUI)
# ---------------------------------------------------------------------------


def _run_conversation_poller() -> None:
    """Poll watched conversations and relay new completed sessions to Slack."""
    import time

    while True:
        try:
            with _db_lock:
                rows = db.execute(
                    "SELECT channel_id, thread_ts, conv_id, last_session_id, latest_thread_ts "
                    "FROM thread_conversations"
                ).fetchall()
            for channel_id, thread_ts, conv_id, last_seen, latest_thread in rows:
                try:
                    resp = httpx.get(
                        f"{API_URL}/api/conversations/{conv_id}", timeout=30
                    )
                    if resp.status_code != 200:
                        continue
                    sessions = resp.json().get("sessions", [])
                    if not sessions:
                        continue

                    # If no marker yet, initialize to latest without delivering
                    if last_seen is None:
                        update_last_session_id(
                            channel_id, thread_ts, sessions[-1]["run_id"]
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
                    # so we know which session belongs to which Slack thread
                    if thread_ts == "channel":
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
                        # Route to the thread that initiated this session.
                        # Priority: session_threads → assigned pending_reply → latest_thread
                        reply_ts = None
                        pending_ack_ts = None
                        if thread_ts == "channel":
                            st = _get_session_thread(session["run_id"])
                            if st:
                                reply_ts = st[1]
                                _delete_session_thread(session["run_id"])
                            else:
                                pr = _pop_pending_reply_by_run_id(
                                    session["run_id"]
                                )
                                if pr:
                                    reply_ts = pr[1]
                                    pending_ack_ts = pr[2]
                                else:
                                    reply_ts = latest_thread
                        elif thread_ts != "dm":
                            reply_ts = thread_ts
                        # Delete queued-task ack message if present
                        if pending_ack_ts:
                            try:
                                app.client.chat_delete(channel=channel_id, ts=pending_ack_ts)
                            except Exception:
                                pass
                        for chunk in chunk_message(md_to_slack_mrkdwn(summary)):
                            app.client.chat_postMessage(
                                channel=channel_id,
                                thread_ts=reply_ts,
                                text=chunk,
                            )
                        update_last_session_id(
                            channel_id, thread_ts, session["run_id"]
                        )
                except Exception:
                    logger.debug("Poller error for conv %d", conv_id)
        except Exception:
            logger.exception("Conversation poller error")
        time.sleep(CONV_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Notification poller — delivers messages pushed via the notify API
# ---------------------------------------------------------------------------


def _run_notification_poller() -> None:
    """Poll for pending notifications and deliver them to Slack channels."""
    import time

    while True:
        try:
            resp = httpx.get(
                f"{API_URL}/api/integrations/{INTEGRATION_NAME}/notifications",
                timeout=30,
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
                        for chunk in chunk_message(md_to_slack_mrkdwn(message)):
                            app.client.chat_postMessage(
                                channel=chat_id,
                                text=chunk,
                            )
                        # ACK after successful delivery
                        httpx.post(
                            f"{API_URL}/api/integrations/{INTEGRATION_NAME}"
                            f"/notifications/{nid}/ack",
                            timeout=30,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to deliver notification %d to channel %s",
                            nid, chat_id,
                        )
        except Exception:
            logger.debug("Notification poller error")
        time.sleep(NOTIFY_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Slack app setup
# ---------------------------------------------------------------------------

app = App(token=BOT_TOKEN)


def _strip_mention(text: str, bot_user_id: str) -> str:
    """Remove the @mention of our bot from the message text."""
    return text.replace(f"<@{bot_user_id}>", "").strip()


@app.event("app_mention")
def handle_mention(event, say, context):
    """Handle @mention in a channel — all mentions in the same channel share one conversation."""
    channel = event["channel"]
    user_text = _strip_mention(event.get("text", ""), context.get("bot_user_id", ""))
    if not user_text:
        return

    # Reply in the thread where the mention happened
    thread_ts = event.get("thread_ts") or event["ts"]

    logger.info("Mention in %s (thread %s): %s", channel, thread_ts, user_text[:100])

    # Acknowledge
    ack_resp = say(text="On it...", thread_ts=thread_ts)
    ack_ts = ack_resp.get("ts") if isinstance(ack_resp, dict) else None

    # Map the entire channel to one conversation (use "channel" as the key)
    conv_id = get_or_create_conversation(channel, "channel")
    # Track latest thread for reply targeting by the conversation poller
    _update_thread_ts(channel, thread_ts)

    try:
        result = submit_task(conv_id, user_text)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            logger.warning("Conversation %d deleted, creating new one for %s", conv_id, channel)
            clear_conv_id(channel, "channel")
            conv_id = get_or_create_conversation(channel, "channel")
            try:
                result = submit_task(conv_id, user_text)
            except httpx.HTTPStatusError as exc2:
                logger.error("Task submission failed after retry: %s", exc2)
                say(text="Failed to submit task. Is StrawPot GUI running?", thread_ts=thread_ts)
                return
        else:
            logger.error("Task submission failed: %s", exc)
            say(text="Failed to submit task. Is StrawPot GUI running?", thread_ts=thread_ts)
            return

    if result.get("queued"):
        if ack_ts:
            try:
                app.client.chat_update(channel=channel, ts=ack_ts, text="Queued — will run after the current session finishes.")
            except Exception:
                pass
        # Record pending reply so the poller can deliver to the right thread
        _add_pending_reply(channel, thread_ts, ack_ts)
        return

    run_id = result.get("run_id")
    if not run_id:
        return

    # Record which thread this session belongs to (for poller fallback)
    _set_session_thread(run_id, channel, thread_ts)

    # Wait for session to complete
    wait_for_session(run_id)

    # Update marker immediately so the conversation poller skips this session
    update_last_session_id(channel, "channel", run_id)
    _delete_session_thread(run_id)

    summary = get_session_summary(conv_id, run_id)

    # Delete "On it..." and post summary
    if ack_ts:
        try:
            app.client.chat_delete(channel=channel, ts=ack_ts)
        except Exception:
            pass

    for chunk in chunk_message(md_to_slack_mrkdwn(summary)):
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
        if exc.response.status_code == 404:
            logger.warning("Conversation %d deleted, creating new one for %s/dm", conv_id, channel)
            clear_conv_id(channel, "dm")
            conv_id = get_or_create_conversation(channel, "dm")
            try:
                result = submit_task(conv_id, text)
            except httpx.HTTPStatusError as exc2:
                logger.error("Task submission failed after retry: %s", exc2)
                say(text="Failed to submit task. Is StrawPot GUI running?", channel=channel)
                return
        else:
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

    # Update marker immediately so the conversation poller skips this session
    update_last_session_id(channel, "dm", run_id)

    summary = get_session_summary(conv_id, run_id)

    if ack_ts:
        try:
            app.client.chat_delete(channel=channel, ts=ack_ts)
        except Exception:
            pass

    for chunk in chunk_message(md_to_slack_mrkdwn(summary)):
        say(text=chunk, channel=channel)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    logger.info("Starting Slack adapter (Socket Mode)")
    logger.info("API URL: %s", API_URL)
    if PROJECT_ID is not None:
        logger.info("Project-scoped: project_id=%d", PROJECT_ID)

    # Start background pollers in daemon threads
    poller_thread = threading.Thread(
        target=_run_conversation_poller, daemon=True
    )
    poller_thread.start()
    notify_thread = threading.Thread(
        target=_run_notification_poller, daemon=True
    )
    notify_thread.start()
    logger.info("Conversation and notification pollers started")

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
