---
name: discord
description: Discord bot adapter for StrawPot conversations
metadata:
  strawpot:
    entry_point: python adapter.py
    install:
      macos: pip install -r requirements.txt
      linux: pip install -r requirements.txt
    env:
      STRAWPOT_BOT_TOKEN:
        required: true
        description: Discord bot token from the Developer Portal
      POLL_INTERVAL:
        required: false
        description: Session poll interval in seconds when WebSocket fails (default 3)
      DC_MAX_LEN:
        required: false
        description: Max characters per Discord message (default 1900)
---

# Discord Adapter

Connects a Discord bot to StrawPot via imu. Messages mentioning the bot
become tasks in imu conversations; session outputs are replied back in
the thread.

## Setup

1. Create a Discord Application at [discord.com/developers](https://discord.com/developers/applications)
2. Under **Bot**, click **Add Bot** and copy the bot token
3. Under **Bot**, enable these Privileged Gateway Intents:
   - **Message Content Intent** — read message text
4. Under **OAuth2** → **URL Generator**, select scopes:
   - `bot`
5. Select bot permissions:
   - Send Messages
   - Send Messages in Threads
   - Create Public Threads
   - Read Message History
6. Use the generated URL to invite the bot to your server
7. Install this integration:
   ```bash
   strawhub install integration discord
   ```
8. In the StrawPot GUI, go to **Integrations** → **discord** → **Configure**
9. Paste the bot token and click Save
10. Click **Start**

## How it works

- **Channel mentions**: `@strawpot fix the login bug` creates a new thread
  and a new imu conversation. The bot replies in the thread.
- **Thread replies**: replies in an existing thread continue the same
  imu conversation.
- **DMs**: each DM conversation maps to a separate imu conversation.
  Send `!new` to reset.
- **Gateway WebSocket**: the bot connects outbound to Discord's Gateway —
  no public URL needed. Ideal for local StrawPot.

## Conversation mapping

| Discord context | Conversation boundary |
|-----------------|----------------------|
| Channel @mention | New thread = new conversation |
| Thread reply | Same thread = same conversation |
| DM | Per-DM conversation, `!new` to reset |

The adapter stores `(channel_id, thread_id) → conversation_id` in a
local SQLite database (`.adapter.db` in the integration directory).

## Commands

| Command | Description |
|---------|-------------|
| `!new` | Start a fresh imu conversation (DMs only) |

## Message limits

Discord supports up to 2000 characters per message. Long outputs are
automatically chunked at newline boundaries.
