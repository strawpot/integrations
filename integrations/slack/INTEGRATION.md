---
name: slack
description: Slack bot adapter for StrawPot conversations via Socket Mode
metadata:
  strawpot:
    entry_point: python adapter.py
    auto_start: false
    install:
      macos: pip install -r requirements.txt
      linux: pip install -r requirements.txt
    env:
      STRAWPOT_BOT_TOKEN:
        required: true
        description: Slack Bot User OAuth Token (xoxb-...)
      STRAWPOT_APP_TOKEN:
        required: true
        description: Slack App-Level Token for Socket Mode (xapp-...)
---

# Slack Adapter

Connects a Slack bot to StrawPot via imu using Socket Mode. Messages
mentioning the bot become tasks in imu conversations; session outputs
are replied back in the thread.

## Setup

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Socket Mode** under Settings → Socket Mode
3. Generate an **App-Level Token** with `connections:write` scope
4. Under OAuth & Permissions, add these Bot Token Scopes:
   - `app_mentions:read` — receive @mentions
   - `chat:write` — send messages
   - `im:history` — read DMs
   - `im:read` — access DM channels
   - `im:write` — open DMs
5. Under Event Subscriptions, subscribe to these bot events:
   - `app_mention` — @mentions in channels
   - `message.im` — direct messages
6. Install the app to your workspace
7. Copy the **Bot User OAuth Token** (`xoxb-...`) and **App-Level Token** (`xapp-...`)
8. Install this integration and its dependencies:
   ```bash
   cp -r integrations/slack ~/.strawpot/integrations/slack
   cd ~/.strawpot/integrations/slack && pip install -r requirements.txt
   ```
9. In the StrawPot GUI, go to **Integrations** → **slack** → **Configure**
10. Paste both tokens and click Save
11. Click **Start**

> **Future:** `strawhub install integration slack` will handle step 8
> automatically, including running the `install` command from the manifest.

## How it works

- **Channel mentions**: `@strawpot fix the login bug` creates a new thread
  and a new imu conversation. The bot replies in the thread.
- **Thread replies**: replies in an existing thread continue the same
  imu conversation.
- **DMs**: each DM conversation maps to a separate imu conversation.
  Use `/new` to reset.
- **Socket Mode**: the adapter connects outbound to Slack's WebSocket —
  no public URL or ngrok needed. Ideal for local StrawPot.

## Conversation mapping

| Slack context | Conversation boundary |
|---------------|----------------------|
| Channel @mention | New thread = new conversation |
| Thread reply | Same thread = same conversation |
| DM | Per-DM conversation, `/new` to reset |

The adapter stores `(channel_id, thread_ts) → conversation_id` in a
local SQLite database (`.adapter.db` in the integration directory).

## Slash commands

| Command | Description |
|---------|-------------|
| `/new` | Start a fresh imu conversation (DMs only) |

## Message limits

Slack supports up to 40K characters per message in blocks. Long outputs
are sent as a single message (no chunking needed in most cases).
