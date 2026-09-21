# PokeSketch

A self-hosted Discord bot that posts a **daily Pokémon sketch challenge** — pings a role, shows 1–2 reference images from [PokéAPI](https://pokeapi.co/), opens a thread for submissions, and gamifies contributions with EXP, levels, streaks, and upvotes.

Multi-guild by design; runs comfortably on a tiny LXC on Proxmox.

Full design doc lives in the author's Obsidian vault (`Areas/Homelab/Proxmox/PokeSketch Bot Plan.md`).

## Features (v1)

- **Daily post** at a per-guild configurable time + timezone, pinging a `@Sketchers` role.
- **1–2 reference images** (official artwork + sprite), cached locally.
- **No-repeat selection** across the full national dex (Gen 1–9, #1–1025) until the pool is exhausted, then it resets. Pure-random and restricted-range modes too.
- **Submissions** via `/submit` inside the auto-created daily thread. Run `/submit` in an *older* thread to contribute to past days.
- **EXP + levels** for submitting, streaks, and receiving upvotes.
- **Personal + server-wide streaks**, leaderboard, and profile cards.
- **Upvotes** via 👍 reactions on submission messages.

## Slash commands

| Command | Who | What |
|---|---|---|
| `/setup channel role [time] [timezone]` | Manage Server | Configure daily posts |
| `/set-mode` | Manage Server | `no_repeat` or `random` |
| `/set-generations dex_min dex_max` | Manage Server | Restrict the dex range |
| `/pause` · `/resume` | Manage Server | Toggle daily posts |
| `/post-now` | Manage Server | Post today immediately (test) |
| `/submit image` | Everyone | Submit a sketch (in a daily thread) |
| `/profile [user]` | Everyone | Level, EXP, streak |
| `/leaderboard` | Everyone | Top 10 by EXP |
| `/streak` · `/stats` | Everyone | Server streak + totals |

## Quick start (local dev)

```bash
git clone https://github.com/Danielelston/pokesketch.git
cd pokesketch
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env       # then paste your bot token into .env
python -m pokesketch
```

For instant slash-command registration during development, set `POKESKETCH_DEV_GUILD_IDS` in `.env` to your test server's ID (global sync can take up to an hour).

## Discord application setup

1. Create an application + bot at <https://discord.com/developers/applications>.
2. Under **Bot**, enable the **Server Members Intent** (used for role/member lookups). Message Content intent is **not** required — submissions use `/submit`.
3. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`, and bot permissions: *Send Messages, Embed Links, Attach Files, Create Public Threads, Send Messages in Threads, Add Reactions, Manage Messages* (and *Manage Roles* only if you later add level→role rewards).
4. Invite the bot, then run `/setup` in your server.

## Self-hosting on Proxmox

Designed to run as a lightweight, outbound-only `systemd` service in an unprivileged Debian 12 LXC. See [`deploy/`](deploy/) for the systemd unit and a provisioning script. The SQLite database in `data/` is the entire bot state — back it up (include the LXC in your vzdump job).

## Configuration

All via environment / `.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `POKESKETCH_DISCORD_TOKEN` | — | **Required.** Bot token. |
| `POKESKETCH_DEV_GUILD_IDS` | — | Comma-separated guild IDs for instant command sync. |
| `POKESKETCH_DB_PATH` | `data/pokesketch.db` | SQLite file path. |
| `POKESKETCH_IMAGE_CACHE_DIR` | `data/images_cache` | Cached PokéAPI JSON/images. |
| `POKESKETCH_LOG_LEVEL` | `INFO` | Log verbosity. |

## Development

```bash
ruff check src/ scripts/     # lint
python scripts/smoke_test.py # offline smoke test (no token needed)
```

## License

MIT — see [LICENSE](LICENSE).
