# StrawPot Integrations

Official integrations for [StrawPot](https://strawpot.com) — the multi-agent orchestration runtime.

## What are integrations?

Integrations are standalone adapter processes that bridge external chat and community platforms to StrawPot. Each integration is a directory containing an `INTEGRATION.md` manifest with YAML frontmatter, an entry point script, and optional dependencies.

All chat messages route through **imu** — StrawPot's self-operation agent. Adapters are thin message relays between the chat platform and imu's conversation API. See the [integration design doc](https://github.com/strawpot/strawpot/blob/main/designs/integration/DESIGN.md) for the full architecture.

## Available Integrations

| Integration | Description |
|-------------|-------------|
| [telegram](integrations/telegram/) | Telegram bot adapter for StrawPot conversations |
| [slack](integrations/slack/) | Slack bot adapter via Socket Mode |
| [discord](integrations/discord/) | Discord bot adapter via Gateway WebSocket |

## Installation

```bash
# Manual install (Phase 1)
cp -r integrations/telegram ~/.strawpot/integrations/telegram
cd ~/.strawpot/integrations/telegram && pip install -r requirements.txt

# Future: strawhub install integration telegram
```

Then configure and start from the StrawPot GUI **Integrations** page.

## Structure

```
integrations/
└── <name>/
    ├── INTEGRATION.md      # manifest (frontmatter + docs)
    ├── adapter.py          # entry point
    └── requirements.txt    # dependencies
```

Each `INTEGRATION.md` follows the [StrawHub frontmatter schema](https://strawhub.dev) with `name`, `description`, and `metadata.strawpot` fields including `entry_point`, `auto_start`, `install`, and `config`.

## License

[MIT](LICENSE)
