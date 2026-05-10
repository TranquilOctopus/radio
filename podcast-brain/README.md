# podcast-brain

A personal podcast knowledge system. Pipes podcast audio through on-device
transcription and a local LLM, then emits an Obsidian-compatible markdown
vault backed by a Kuzu graph that surfaces cross-source connections. Cloud
Claude calls (Sonnet) only at the per-episode summary and weekly synthesis
level, with prompt caching and a hard monthly budget cap.

No conversational chat layer — output is a browsable, diffable, git-versioned
vault you read in Obsidian (or any markdown editor).

## Architecture

```
PodcastIndex / RSS / OPML / URL ──► inbox ──► transcribe (Whisper, GPU)
                                  │
                                  ▼
                              chunk (sentence-aligned, ~3-min windows)
                                  │
                          ┌───────┴────────┐
                          ▼                ▼
            extract (local LLM)    summarize (Sonnet, once per episode)
            entities/claims              │
                          │              │
                          └──────┬───────┘
                                 ▼
              ┌──────────────────┴──────────────────┐
              ▼                                     ▼
      Kuzu graph                            Obsidian vault (primary surface)
      (nodes/edges)                         vault/{episodes,people,concepts,claims,digests}
              │
              ▼
      weekly synthesizer (Sonnet) → digests + concept pages with backlinks
```

The vault directory is itself a git repo — every ingest run can commit, so you
get history and can diff what the system added.

## Quickstart

### 1. Install

Pick the extra that matches your hardware:

```bash
# macOS Apple Silicon
pip install -e .[mac]

# Linux + NVIDIA CUDA
pip install -e .[cuda]

# CPU-only (slow; CI / no-GPU dev)
pip install -e .[cpu]

# Optional: yt-dlp for ingesting one-off URLs (YouTube, SoundCloud)
pip install -e .[url]

# Dev: tests
pip install -e .[dev]
```

System dependencies the package does NOT install:

- `ffmpeg` — required for `yt-dlp` audio extraction (only if you use `url add`)
- `ollama` — required for the local LLM extractor (default backend)

### 2. Configure

Copy `config.toml` to your working directory and edit. Two fields are required
for full functionality, the rest have sensible defaults:

```toml
[ingest.podcastindex]
api_key = "..."        # free credentials at https://podcastindex.org/signup
api_secret = "..."

[budget]
monthly_cap_usd = 20.0  # hard cap on Claude spend
```

Without PodcastIndex credentials you can still use direct RSS URLs and
Apple Podcasts share URLs, but `feed backfill` (full historical episodes)
will refuse to run.

### 3. Install ollama and pull a model

```bash
# https://ollama.com — install for your OS
ollama pull qwen2.5:14b-instruct-q4_K_M    # default, ~9 GB
# or for tighter VRAM:
ollama pull llama3.1:8b-instruct-q4_K_M    # ~5 GB
```

Edit `[extract] local_model` in `config.toml` if you picked a different model.

### 4. Add feeds

```bash
# By RSS URL
podcast-brain feed add https://example.com/podcast/rss

# By Apple Podcasts share URL
podcast-brain feed add 'https://podcasts.apple.com/us/podcast/foo/id1234567890'

# By show name (interactive prompt picks from iTunes search)
podcast-brain feed add "Lex Fridman"

# Bulk import from a podcast app (Apple Podcasts, Overcast, Pocket Casts...)
podcast-brain feed import library.opml
```

Each feed has a `style` that controls extraction: `informational` (default;
interview/lecture/news), `banter` (chat/comedy — quotes only, not claims),
`narrative` (serial storytelling), or `skip` (record subscription, don't
summarize). Override with `--style <s>` on add, or later via
`podcast-brain feed style <slug> <style>`.

### 5. Pull historical episodes

```bash
podcast-brain feed backfill lex-fridman --from 2023
```

Episodes land in a backlog queue. The pacer (configured in `[backlog]`) trickles
them into the pipeline at `max_episodes_per_day` with `per_show_daily_cap`
fairness so one prolific show doesn't monopolize.

### 6. Run the pipeline

```bash
# One-shot: process everything pending and exit
podcast-brain ingest now

# Long-running daemon: poll feeds + advance pipeline + weekly digest cron
podcast-brain ingest daemon
```

### 7. Read the output

```bash
# Open the vault directory in Obsidian (or any markdown editor)
open vault/

# Live status dashboard
podcast-brain serve   # then http://127.0.0.1:8765

# Ad-hoc graph queries (Cypher)
podcast-brain query 'MATCH (e:Episode)-[:CONTAINS]->(c:Claim) RETURN count(c)'

# Month-to-date Claude spend
podcast-brain budget
```

## CLI reference

```
podcast-brain feed add <rss-url | show-name | apple-podcasts-url> [--style <s>] [--auto-style]
podcast-brain feed style <show> <informational|banter|narrative|skip>
podcast-brain feed import <opml-file>
podcast-brain feed backfill <show> [--from YYYY]

podcast-brain ingest now              # one-shot
podcast-brain ingest daemon           # long-running

podcast-brain transcribe <audio-file> # standalone Whisper, no pipeline

podcast-brain digest weekly [--week YYYY-Www]   # default: previous week

podcast-brain query "<cypher>"        # ad-hoc graph query

podcast-brain inbox watch <dir>       # daemon: pick up dropped audio files

podcast-brain url add <url>           # one-off via yt-dlp

podcast-brain serve [--host H] [--port N]   # FastAPI dashboard

podcast-brain budget                  # MTD Claude spend
```

## Configuration

`config.toml` sections:

| Section | Purpose |
|---|---|
| `[paths]` | Where audio, transcripts, graph, queue DB, and vault live. |
| `[compute]` | Whisper backend (`auto`/`mlx`/`faster-whisper-cuda`/`faster-whisper-cpu`), Whisper model, PyTorch device, model serialization. |
| `[backlog]` | Daily release cap and per-show fairness for back-catalog ingest. |
| `[extract]` | LLM extraction backend (`local` via ollama or `claude` via Haiku). Local model + endpoint. JSON mode. |
| `[summarize]` | Episode summary length, weekly digest length, banter exclusion. |
| `[transcribe]` | Diarization, language hint, summary language preference. |
| `[chunker]` | Chunk window length and overlap. |
| `[budget]` | Monthly USD cap, warn threshold, summarize model. |
| `[notion]` | Optional Notion mirror (off by default). |
| `[ingest.podcastindex]` | PodcastIndex API credentials. |
| `[ingest.inbox]` | Watch-folder defaults. |

The full annotated `config.toml` ships in the repo with comments on every
field.

## Per-episode state machine

```
PENDING → DOWNLOADED → TRANSCRIBED → CHUNKED → EXTRACTED → CANONICALIZED → SUMMARIZED → INDEXED → DONE
                                                                                               ↘ FAILED  (any stage; retryable)
                                                                                               ↘ BUDGET_PAUSED (cap hit)
```

Stages are idempotent — every intermediate artifact is written atomically to
disk under `data/`, so killing the process mid-run and restarting picks up
exactly where it left off. A `BUDGET_PAUSED` job is terminal for the daemon
(won't auto-retry); clear it manually after raising the cap or letting the
month roll over.

## Show styles

Different podcasts call for different extraction. Each feed carries a `style`:

| Style | Extracts | Skips | Goes to weekly digest? |
|---|---|---|---|
| `informational` (default) | People, orgs, concepts, claims, predictions | — | Yes |
| `banter` | Quotes, mentions, vibe summary | Claims, predictions, concept extraction | No (by default) |
| `narrative` | Chronology, characters, arc notes | Per-chunk claims | Yes (arc-aware template) |
| `skip` | Nothing | Everything | No |

Auto-classification on first ingest is stubbed (`__pending_classification`
sentinel maps to `informational` for now); the hook in `pkm/extract/classify.py`
is the place to wire a real classifier later.

## Hardware notes

| Platform | Whisper backend | LLM backend | VRAM serialization |
|---|---|---|---|
| Linux + NVIDIA 12 GB (e.g. 3080 Ti) | `faster-whisper` CUDA | ollama (CUDA) | **On.** Whisper unloads before LLM loads. Auto-detected. |
| Linux + NVIDIA 16+ GB | `faster-whisper` CUDA | ollama (CUDA) | Off. Both resident. |
| macOS Apple Silicon (16 GB+ unified memory) | `mlx-whisper` | ollama (Metal) | Off. Unified memory. |
| macOS Apple Silicon 8 GB | `mlx-whisper` | ollama (8B model recommended) | Off (no equivalent constraint). |
| CPU-only | `faster-whisper` CPU | ollama CPU | Off. Slow. |

The pipeline calls `torch.cuda.mem_get_info()` to detect VRAM at startup; if
total < 16 GiB, it serializes Whisper unload before LLM load. Override via
`[compute] serialize_models = "true" | "false" | "auto"`.

## Sources

| Source | How |
|---|---|
| RSS feed | `feed add <rss-url>` |
| Apple Podcasts share URL | `feed add 'https://podcasts.apple.com/.../id<N>'` (resolved via iTunes Lookup) |
| Show name | `feed add "Show Name"` (interactive iTunes search) |
| OPML export | `feed import library.opml` (Apple Podcasts, Overcast, Pocket Casts, AntennaPod) |
| Full back-catalog | `feed backfill <slug> [--from YYYY]` (via PodcastIndex API) |
| One-off URL | `url add <youtube/soundcloud/episode-page-url>` (via yt-dlp) |
| Manual file drop | `inbox watch <dir>` daemon, or drop into the configured watch dir |
| Discord bot | `/feed add`, `/feed search`, `/url` — see "Optional: Discord bot" below |

**Out of scope** (DRM / ToS): Apple Podcasts Subscriptions paid feeds,
Patreon-locked feeds, Spotify exclusives.

## Cost controls

- All Claude calls go through `pkm/budget.py:BudgetTracker` which records every
  call (model, input/output tokens, cache read/write, USD) into the
  `claude_calls` table.
- `[budget] monthly_cap_usd` is enforced before each Sonnet call. When the
  projected cost would push month-to-date over the cap, the job is marked
  `BUDGET_PAUSED` and the pipeline skips it.
- Local LLM extraction is the high-volume path (potentially hundreds of chunks
  per episode) and is free; only the once-per-episode summary and once-per-week
  synthesis hit Sonnet. Order-of-magnitude estimate at 30 hr/week: $5-15/week.
- `podcast-brain budget` shows MTD spend, cap, percentage used, and a per-model
  breakdown.

## Reading the vault

The vault is plain markdown with YAML frontmatter and `[[wikilinks]]` —
opens natively in Obsidian, Logseq, or any compatible editor. Layout:

```
vault/
├── episodes/<show-slug>/<YYYY-MM-DD>-<title-slug>.md
├── people/<slug>.md
├── concepts/<slug>.md
├── organizations/<slug>.md
├── claims/<hash>.md
└── digests/weekly/<YYYY-Www>.md
```

Backlinks (the "Mentioned in" sections on people/concept pages) are regenerated
from the Kuzu graph on each ingest, so they always reflect the current state of
the corpus.

For database-style queries over the vault, install Obsidian's Dataview plugin —
e.g. "all episodes mentioning concept X", "unresolved predictions by speaker Y".

## Optional: Discord bot

A slash-command Discord bot that talks to the local FastAPI dashboard, so you
can add feeds, check status, and read the weekly digest from your phone.

The bot and dashboard are designed to live on the same machine — the dashboard
stays bound to `127.0.0.1` (no auth), and the bot connects over localhost.

### Setup

1. Install the extra: `pip install -e .[discord]`
2. Create a Discord application and bot at
   <https://discord.com/developers/applications> → New Application → Bot →
   Reset Token. Copy the token.
3. Invite the bot to your server: under OAuth2 → URL Generator, tick
   `bot` and `applications.commands` scopes, then `Send Messages` permission.
   Open the generated URL and authorise it for your server.
4. Find your Discord user ID: enable Developer Mode in Discord settings
   (App Settings → Advanced → Developer Mode), then right-click your name
   → Copy User ID.
5. Configure `[bot.discord]` in `config.toml`:

   ```toml
   [bot.discord]
   enabled = true
   token = "PASTE_TOKEN_HERE"
   api_base_url = "http://127.0.0.1:8765"
   allowed_user_ids = [123456789012345678]   # your Discord user ID
   ```

6. Make sure the dashboard is running (`podcast-brain serve`), then:

   ```bash
   podcast-brain bot discord
   ```

   On first run, slash commands take up to ~1 hour to propagate globally
   the first time; restart your Discord client to refresh sooner.

### Slash commands

| Command | What it does |
|---|---|
| `/feed add <url> [style]` | Subscribe to an RSS or Apple Podcasts URL |
| `/feed search <name>` | iTunes search; replies with candidates and a copy-pasteable `/feed add` line for each |
| `/feed list` | Subscribed feeds with style + pending job count |
| `/feed style <slug> <style>` | Change a feed's style (`informational` / `banter` / `narrative` / `skip`) |
| `/queue status` | Counts of jobs by pipeline status |
| `/queue jobs [status] [limit]` | Recent jobs, optionally filtered by status |
| `/budget` | MTD Claude spend with per-model breakdown |
| `/digest` | Latest weekly digest (truncated to fit Discord's 2000-char limit) |
| `/url <url> [style]` | One-off ingest via yt-dlp (YouTube etc.) |

### Run as a service

The bot is a long-running foreground process — supervise it the same way as
the daemon. Example systemd user unit (`~/.config/systemd/user/podcast-brain-bot.service`):

```ini
[Unit]
Description=podcast-brain Discord bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/YOU/podcast-brain
ExecStart=/home/YOU/podcast-brain/.venv/bin/podcast-brain bot discord
Restart=on-failure
RestartSec=30s

[Install]
WantedBy=default.target
```

`systemctl --user enable --now podcast-brain-bot.service`. Logs via
`journalctl --user -u podcast-brain-bot -f`.

### Security caveats

- The dashboard has **no authentication**. It MUST stay bound to `127.0.0.1`
  (the default). If you ever change `--host`, add an auth layer first.
- `allowed_user_ids` is the only access control on the bot. Leaving it empty
  means anyone in any channel where the bot is present can issue commands and
  burn your Claude budget. **Always set it** to your own user IDs.
- Use a private Discord server where you control the membership; don't add the
  bot to a public server.

## Optional: Notion mirror

Notion is supported as a secondary output sink (off by default). Set
`[notion] enabled = true` and provide an integration token + database IDs
to push episode summaries and weekly digests to Notion in addition to the
markdown vault. The vault remains canonical.

## Tests

```bash
pip install -e .[dev]
python -m pytest tests/ -v
```

The suite is hermetic — no live Anthropic API calls, no live ollama, no
network. Whisper, ollama, and Anthropic clients are all mocked.

## Operating it

### Day-to-day workflow

The intended steady-state is:

1. **Daemon runs continuously** in the background. Polls feeds every 30 min,
   advances the pipeline every 5 min, runs a weekly digest Sunday 8 AM (local
   time).
2. **You add feeds when you discover new shows.** Run `feed add` from anywhere
   — the daemon picks up the new feed on its next poll cycle.
3. **You read the vault** in Obsidian whenever you want. New episode pages
   appear as the pipeline finishes them.
4. **Sunday morning the digest is waiting** at `vault/digests/weekly/<YYYY-Www>.md`.
5. **Periodically commit the vault** to git so you have history.

### Running the daemon as a service

The daemon is a long-running foreground process — `podcast-brain ingest daemon`.
For real use, supervise it.

**macOS (launchd):** create `~/Library/LaunchAgents/com.podcast-brain.daemon.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.podcast-brain.daemon</string>
    <key>WorkingDirectory</key><string>/Users/YOU/podcast-brain</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/podcast-brain/.venv/bin/podcast-brain</string>
        <string>ingest</string>
        <string>daemon</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/Users/YOU/podcast-brain/logs/daemon.log</string>
    <key>StandardErrorPath</key><string>/Users/YOU/podcast-brain/logs/daemon.err</string>
</dict>
</plist>
```

Load with `launchctl load ~/Library/LaunchAgents/com.podcast-brain.daemon.plist`.

**Linux (systemd user unit):** create `~/.config/systemd/user/podcast-brain.service`:

```ini
[Unit]
Description=podcast-brain ingest daemon
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/YOU/podcast-brain
ExecStart=/home/YOU/podcast-brain/.venv/bin/podcast-brain ingest daemon
Restart=on-failure
RestartSec=30s

[Install]
WantedBy=default.target
```

Enable with `systemctl --user enable --now podcast-brain.service`. Logs via
`journalctl --user -u podcast-brain -f`.

The dashboard (`podcast-brain serve`) is a separate process — run it the same
way under a second service unit, or just spin it up ad-hoc when you want to
look at status.

### Status and monitoring

- **Dashboard** — `podcast-brain serve`, then `http://127.0.0.1:8765`. Shows
  queue counts, recent jobs, feeds, MTD spend. Auto-refreshes every 30 s.
- **Budget** — `podcast-brain budget` prints MTD spend with a per-model
  breakdown. Run as a daily cron if you want a paper trail.
- **Ad-hoc graph queries** — `podcast-brain query "<cypher>"`. Examples:

  ```bash
  # How many episodes per podcast?
  podcast-brain query 'MATCH (e:Episode) RETURN e.podcast, count(*) ORDER BY count(*) DESC'

  # Concepts mentioned in 3+ episodes
  podcast-brain query 'MATCH (e:Episode)-[:MENTIONS]->(c:Concept) WITH c, count(DISTINCT e) AS n WHERE n >= 3 RETURN c.name, n ORDER BY n DESC'

  # Recent failures
  podcast-brain query 'MATCH (e:Episode) RETURN e.id, e.title LIMIT 5'  # (graph view)
  ```

  For job-level inspection (which jobs failed and why), open the SQLite queue
  directly: `sqlite3 data/jobs.db "SELECT id, episode_title, status, last_error FROM jobs WHERE status = 'FAILED'"`.

### When something goes wrong

**A job is stuck in `FAILED`.** The error is in `last_error`. Common causes:

- Audio download 404 — the show pulled the episode. Acceptable; leave it.
- Whisper crash on a corrupt mp3 — re-fetch or skip.
- Ollama not reachable — check `ollama serve` is running, then re-set the
  job to `PENDING` to retry: `sqlite3 data/jobs.db "UPDATE jobs SET status = 'PENDING' WHERE id = <N>"`.

To bulk-retry every FAILED job: `UPDATE jobs SET status = 'PENDING', attempts = 0 WHERE status = 'FAILED';`.

**A job is stuck in `BUDGET_PAUSED`.** You hit the monthly cap. Options:

1. Wait for the month to roll over (the cap is calendar-month-based).
2. Raise `[budget] monthly_cap_usd` in `config.toml`, then bulk-clear:
   `sqlite3 data/jobs.db "UPDATE jobs SET status = 'CANONICALIZED' WHERE status = 'BUDGET_PAUSED'"`.
   The daemon will pick them up on the next tick.

**The pipeline stops advancing.** Check the dashboard or `journalctl`. Most
likely:

- Ollama is down — `pkill ollama; ollama serve &`.
- Disk full — check `data/audio/` size; old files can be deleted (the
  pipeline doesn't need them after `INDEXED`).
- All jobs are in terminal status (`DONE`/`FAILED`/`BUDGET_PAUSED`) — there's
  nothing to do until new feeds are polled.

**Restart safety.** Every stage writes its output atomically and is
idempotent. Killing the daemon mid-run and restarting picks up exactly where
it left off — if Whisper had finished but extract crashed, the next run reuses
the transcript JSON without re-transcribing. No state is held in memory.

### Maintenance

**Vault git workflow.** The vault is a normal markdown directory; treat it as
a git repo:

```bash
cd vault && git init && git add . && git commit -m "Initial import"
```

After that, a daily cron commit gives you history without thinking:

```bash
# Daily at 2 AM
0 2 * * *  cd /path/to/vault && git add . && git commit -m "Auto: $(date +\%F)" --allow-empty
```

Or push to a private remote if you want backup.

**Audio file pruning.** `data/audio/` accumulates raw mp3/m4a files. Once an
episode is `DONE`, the audio is no longer needed. Reclaim space:

```bash
# Delete audio for episodes that finished more than 30 days ago
sqlite3 data/jobs.db "SELECT episode_url FROM jobs WHERE status = 'DONE' AND updated_at < date('now', '-30 days')" \
  | sed 's|.*/||' \
  | xargs -I{} rm -f data/audio/{}
```

(The pipeline won't redownload — it only re-runs when a job is re-set to
`PENDING`.)

**Database vacuuming.** SQLite's WAL grows over time. Once a month:

```bash
sqlite3 data/jobs.db "VACUUM"
```

**Log rotation.** If you set up the daemon under launchd/systemd with file
output, use `logrotate` (Linux) or just truncate periodically:

```bash
# Keep last 10 MB of daemon logs
truncate -s 10M ~/podcast-brain/logs/daemon.log
```

### Updating

- **Bumping the local LLM** — change `[extract] local_model` and `ollama pull`
  the new model. No code changes needed; the next chunk extracted uses the new
  model. Old transcripts/extractions on disk stay valid; only fresh extractions
  use the new prompt.
- **Bumping the Sonnet model** — change `[budget] summarize_model`. The
  prompt cache invalidates (caches are model-scoped) — first call after the
  switch pays full price, subsequent calls re-cache normally. Match the
  per-model price table in `pkm/pipeline.py:_PRICES_PER_MTOK` if you switch
  to a model that's not already listed, or budget tracking will report $0.
- **Schema changes to the graph or queue** — there's no migration system in
  v1. If a future version changes the schema, plan to either nuke `data/` and
  re-run the pipeline (audio is downloaded fresh; everything else regenerates)
  or write a one-off migration script.

### Troubleshooting cheatsheet

| Symptom | Likely cause | Fix |
|---|---|---|
| `feed backfill` exits with "credentials not configured" | `[ingest.podcastindex]` empty | Sign up at podcastindex.org, paste key+secret into `config.toml` |
| `feed add <apple-url>` says "Could not resolve" | iTunes Lookup returned no results | Check the URL has `id<digits>` in the path; try the show's RSS URL directly |
| All jobs are `BUDGET_PAUSED` on day 1 of the month | Cap was set lower than a single summarize call costs | Raise `monthly_cap_usd` |
| Dashboard shows a job in `URL_PENDING` for hours | yt-dlp background task crashed silently | Check daemon logs; the job will sit until manually advanced |
| `inbox watch` doesn't pick up files | Files are still being written (size changes) | Watcher waits `file_settle_seconds` for stable size — bump if your source app writes slowly |
| First request after model change is expensive | Prompt cache invalidated by model swap | Expected — subsequent requests will hit the cache |
| Whisper transcribes very slowly | Falling back to CPU when CUDA was expected | Verify with `python -c "import torch; print(torch.cuda.is_available())"` |
| Tests fail on `feedparser`-related tests | feedparser/sgmllib3k not installable | Expected on some Pythons; tests gate with `importorskip` |

## Future work


Architecturally allowed-for but not in v1:

- **Semantic retrieval over the transcript corpus.** Embed transcripts with a
  local sentence-transformer, store in LanceDB, query via `query semantic
  "<text>"`. Push mode: nightly job reads project notes from the vault, embeds
  them as queries, writes back "Related from podcasts" callouts.
- **Speaker prediction tracking.** Diarize, identify speakers, capture
  forward-looking claims, evaluate them when their timeframe elapses, score
  speakers via Brier. Tetlock for podcast pundits.
- **macOS Apple Podcasts library sync.** Read the local SQLite library directly
  to auto-import subscriptions and play positions (currently OPML-only).
- **Auto-style classification.** Wire `pkm/extract/classify.py` to detect
  whether a feed is informational/banter/narrative on first ingest; for now
  defaults to `informational`.
- **HomeKit / iOS shortcut** for adding URLs from phone.
- **"Listen radar"** — daily digest of which queued episodes look most relevant
  to current projects.

## Repo layout

```
podcast-brain/
├── pyproject.toml
├── config.toml             # annotated example
├── pkm/
│   ├── cli.py              # typer entrypoints
│   ├── pipeline.py         # state machine
│   ├── queue.py            # SQLite queue + feeds + claude_calls
│   ├── budget.py           # spend tracker + cap enforcement
│   ├── api.py              # FastAPI dashboard
│   ├── ingest/
│   │   ├── podcastindex.py # PodcastIndex API (HMAC auth)
│   │   ├── itunes.py       # iTunes Search + Lookup, Apple URL resolver
│   │   ├── rss.py          # feedparser + conditional GET
│   │   ├── opml.py         # OPML walker (flat + nested)
│   │   ├── pacer.py        # backlog rate-limited release
│   │   ├── url.py          # yt-dlp wrapper
│   │   └── inbox.py        # watchdog folder watcher
│   ├── transcribe/
│   │   ├── base.py         # WhisperBackend protocol + auto-detect
│   │   ├── faster_whisper.py # CUDA / CPU
│   │   └── mlx_whisper.py  # Apple Silicon
│   ├── extract/
│   │   ├── chunker.py      # sentence-aligned ~3min windows
│   │   ├── base.py         # Extractor protocol
│   │   ├── local.py        # ollama via /api/chat with json_schema
│   │   ├── canonicalize.py # entity dedupe (slug + Levenshtein)
│   │   ├── schemas/        # pydantic models per style
│   │   └── prompts/styles/ # per-style system prompts
│   ├── summarize/
│   │   ├── episode.py      # Sonnet w/ prompt caching
│   │   ├── synthesize.py   # weekly cross-episode digest
│   │   └── prompts/        # per-style + weekly templates
│   └── store/
│       ├── graph.py        # Kuzu schema + upserts
│       └── vault.py        # markdown writer w/ frontmatter + wikilinks
├── data/                   # gitignored: audio, transcripts, graph DB, queue
└── vault/                  # the readable output (own git repo recommended)
```
