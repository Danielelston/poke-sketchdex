# PokeSketchDex

A self-hosted Discord bot that posts a **daily Pokémon sketch challenge** — pings a role, shows 1–2 reference images from [PokéAPI](https://pokeapi.co/), opens a thread for submissions, and gamifies contributions with EXP, levels, streaks, upvotes, a catch-and-collect system, and weekly community-voted wild encounters.

Multi-guild by design; runs comfortably on a tiny LXC on Proxmox.

Full design docs live in the author's Obsidian vault (`Areas/Homelab/Projects/poke-sketchdex/`).

## Features

- **Daily post** at a per-guild configurable time + timezone, pinging a `@Sketchers` role.
- **1–2 reference images** (official artwork + sprite), cached locally.
- **No-repeat selection** across the full national dex (Gen 1–9, #1–1025) until the pool is exhausted, then it resets. Pure-random and restricted-range modes too. Admins can manually reset the pool with `/reset-pool`.
- **Submissions** via `/submit` inside the auto-created daily thread. Run `/submit` in an *older* thread (within a configurable grace period, default 7 days) to contribute to past days.
- **EXP + levels**, tracked both **per-server** and **globally** (summed across every server you sketch in) — see `/profile`.
- **Personal + global streaks** (submitting anywhere on a given day keeps your streak alive, only counted once), server-wide streaks, and a leaderboard.
- **Upvotes** via 👍 reactions on submission messages — giving *and* receiving an upvote both earn a small amount of EXP, each with its own daily cap.
- **PokéBox** — every `/submit` automatically scans that species into your personal dex-completion tracker (`/pokebox`), free and unlimited.
- **Catch, Party & Storage** — spend a weekly-granted pokeball (`/catch`) to add an eligible sketch to your collection: 6 active party slots + a 20-total storage box, with `/swap` and `/release` to manage them. Caught images are downloaded and cached locally (downsized to keep storage bounded).
- **Weekly two-stage community vote & wild encounters** — each week, players vote on a *category* (Location / Type / Event / Habitat / Generation), then on a *specific choice* within it. The winning combination spawns a fresh daily wild-encounter thread all week, with its own Pokémon and its own EXP — `/submit` works there too, alongside the main daily challenge. Admins can author custom themed Events (`/event-create`) and set the weekly vote day (`/set-vote-day`).

## Slash commands

Run `/help` in Discord for the always-current, in-app reference (player commands + live EXP numbers pulled straight from the code) and `/help-admin` for the admin command list. Summary:

### Player
| Command | What |
|---|---|
| `/submit` | Submit your sketch (today's thread or an older/wild-encounter thread) |
| `/profile [user]` | Level, EXP, and streaks — per-server and global |
| `/leaderboard` | Top sketchers in this server by EXP |
| `/streak` · `/stats` | Server-wide streak and totals |
| `/pokebox [user]` | Dex completion tracker (free, automatic) |
| `/catch [target] [nickname]` | Spend a pokeball to catch an eligible sketch into your party/storage |
| `/party [user]` | Your 6 active party slots |
| `/box [user]` | Paginated view of your storage box |
| `/swap <box_slot> <active_slot>` | Swap a boxed mon into your active party |
| `/release <slot> [active]` | Release a caught mon and free its slot |
| `/pokeballs [user]` | Pokeball balance and next weekly grant |
| `/help` | In-app help and EXP reference |

### Admin (Manage Server)
| Command | What |
|---|---|
| `/setup channel role [time] [timezone]` | Configure daily posts |
| `/set-mode` | `no_repeat` or `random` selection |
| `/set-generations dex_min dex_max` | Restrict the dex range |
| `/set-grace-period` | Days a thread stays open for `/submit` backfill (default 7) |
| `/set-catch-window` | Hours a submission stays catchable via `/catch` (default 24) |
| `/pause` · `/resume` | Toggle daily posts |
| `/post-now` | Post today's challenge immediately (test) |
| `/reset-pool` | Manually reset the no-repeat pool (confirmation required) |
| `/set-vote-day` | Weekday the weekly category vote posts (default Sunday) |
| `/event-create` / `/event-list` / `/event-disable` | Author and manage custom wild-encounter Events |
| `/help-admin` | In-app admin reference (viewable by anyone) |

## Quick start (local dev)

```bash
git clone https://github.com/Danielelston/poke-sketchdex.git
cd poke-sketchdex
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env       # then paste your bot token into .env
python -m pokesketch
```

For instant slash-command registration during development, set `POKESKETCH_DEV_GUILD_IDS` in `.env` to your test server's ID. The bot only ever registers commands guild-scoped (never globally) — on startup it syncs to every guild it's currently in, and to any new guild on join, so commands appear instantly everywhere with no duplicates.

## Discord application setup

1. Create an application + bot at <https://discord.com/developers/applications>.
2. Under **Bot**, enable the **Server Members Intent** (used for role/member lookups). Message Content intent is **not** required — submissions use `/submit`.
3. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`, and bot permissions: *Send Messages, Embed Links, Attach Files, Create Public Threads, Send Messages in Threads, Add Reactions, Manage Messages* (and *Manage Roles* only if you later add level→role rewards).
4. Invite the bot, then run `/setup` in your server.

## Self-hosting on Proxmox

Designed to run as a lightweight, outbound-only `systemd` service in an unprivileged Debian 12 LXC. See [`deploy/`](deploy/) for the systemd unit, a provisioning script, and the nightly DB backup + daily health-check timers. The SQLite database in `data/` is the entire bot state — back it up before every deploy (`cp data/pokesketch.db data/pokesketch.db.pre-deploy-bak`) and include the LXC in your vzdump job. Cached reference art (`data/images_cache/`) and caught-sketch images (`data/party_cache/`) live alongside it.

## Configuration

All via environment / `.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `POKESKETCH_DISCORD_TOKEN` | — | **Required.** Bot token. |
| `POKESKETCH_DEV_GUILD_IDS` | — | Comma-separated guild IDs (currently unused by the sync path — the bot always syncs guild-scoped to every joined guild; kept for future use). |
| `POKESKETCH_DB_PATH` | `data/pokesketch.db` | SQLite file path. |
| `POKESKETCH_IMAGE_CACHE_DIR` | `data/images_cache` | Cached PokéAPI JSON/images. |
| `POKESKETCH_LOG_LEVEL` | `INFO` | Log verbosity. |

Everything else (EXP amounts, daily caps, party/storage caps, pokeball formula) is tuned via named constants in `src/pokesketch/leveling.py` and `src/pokesketch/pokebox.py`, not environment variables — see those files for current values, or `/help` for the live in-Discord numbers.

## Development

```bash
ruff check .                 # lint
python scripts/smoke_test.py # offline smoke test (no token needed) — covers selection,
                              # leveling, global EXP, upvote EXP (give + receive), PokeBox,
                              # catch/party/storage, weekly vote + wild encounters, and /help
```

## License

MIT — see [LICENSE](LICENSE).
