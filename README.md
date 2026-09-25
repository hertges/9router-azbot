# 9router-azbot

9Router AI gateway + AzBot Telegram leech bot in **one container**, sized for a
single lowest-spec Sevalla instance (0.5 vCPU / 1 GB).

- **9Router** runs in the foreground and owns `$PORT` (Sevalla health probe).
  Base image `decolua/9router:0.5.86` — untouched, no Node build on deploy.
- **AzBot** (`azbot/`, fixed Sep-12 build, same code as `hertges/AzBot`)
  runs beside it under a restart supervisor, on its own Python 3.12 venv
  (see "AzBot adaptations" below). Bot code is unmodified.

## Layout

- `Dockerfile` — `FROM decolua/9router:0.5.86` + AzBot Python layer.
- `entrypoint.sh` — starts AzBot loop in background, 9Router in foreground.
- `check_imports.py` — build-time check (run as `node` user): proves the
  venv interpreter is usable at runtime + reports tgcrypto fast/fallback.
- `azbot/` — AzBot source (`bot.py`, `bot_pkg/`, `requirements.txt`),
  byte-identical to `hertges/AzBot` except `requirements.txt` (below).
- `.env.example` — env vars to paste into the Sevalla dashboard.

## AzBot adaptations (packaging only — zero bot-code changes)

`bot.py` and every module under `bot_pkg/` are byte-identical to the
standalone `hertges/AzBot` repo. Only the packaging around the bot changed,
for one reason: the 9router base ships system Python 3.14, which pyrogram
2.x cannot even import on (`asyncio.get_event_loop` removal, inside
pyrogram's own `sync.py` — not AzBot code). So:

1. **`azbot/requirements.txt`** — `tgcrypto` pin removed. Its last release
   (1.2.5) has no cp312-musl wheels, so a hard pin fails the pip install
   and kills the whole image build. The Dockerfile tries a best-effort
   source build and continues without it; the bot's own `crypto_guard.py`
   self-tests at every startup and falls back to pure-Python crypto
   automatically. Worst case = ~2-3x slower transfers, never a crash.
   Check `/stats` in the bot for the active crypto mode.
2. **Python 3.12 venv via `uv`** (`/app/azbot-venv`) — AzBot's pinned
   runtime, managed by `uv` without touching system python. `uv` installs
   to world-readable `/opt/uv-python` (its `/root` default is unreadable
   by the `node` runtime user — that was the `Permission denied`
   crash-loop). The entrypoint supervisor runs
   `/app/azbot-venv/bin/python /app/azbot/bot.py`.
3. **Supervisor loop in `entrypoint.sh`** — restarts AzBot on crash/exit
   (15s delay); exits(1) until `API_ID`/`API_HASH`/`BOT_TOKEN` are set.
   AzBot `data/` is symlinked to `$DATA_DIR/azbot` for disk persistence.

## Deploy on Sevalla

1. Dashboard → **Applications → Create → Application** from this repo.
   Build strategy: **Dockerfile**, path `Dockerfile`, context repo root.
2. Instance: smallest available (Hobby / 0.5 CPU · 1 GB).
3. Disk optional — skipped per your choice. Without it `/app/data`
   is ephemeral: redeploy wipes 9Router DB + AzBot settings/cookies.
   Env-based auth (BOT_TOKEN, Drive) survives; chat settings don't.
4. Set env vars from `.env.example`:
   required: `API_ID`, `API_HASH`, `BOT_TOKEN`, `INITIAL_PASSWORD`.
   Do NOT set `PORT` — Sevalla injects it.
5. Deploy. 9Router dashboard opens at your `*.sevalla.app` URL.

## Low-RAM tuning (already defaulted in image)

`QUEUE_WORKERS=1`, `UPLOAD_WORKERS=1`, `DRIVE_UPLOAD_CONCURRENCY=1`,
`HANDLER_WORKERS=8`. Raise only with RAM headroom — peak memory is what
gets a small box OOM-killed.

## Verify

- Sevalla logs show 9Router listening + `[azbot]` supervisor lines.
  Build log prints `TGCRYPTO=fast` or `TGCRYPTO=missing-pure-python-fallback`.
- 9Router: open the app URL, log in with `INITIAL_PASSWORD`.
- AzBot: send `/stats` to your bot — build stamp confirms the new code.

## Updating

- 9Router: bump the `FROM decolua/9router:X.Y.Z` tag, redeploy.
- AzBot: to update, copy fresh files from `hertges/AzBot` into `azbot/`
  (keep this repo's `azbot/requirements.txt` tgcrypto change — see above).
  Never commit `.env`, `data/`, `downloads/`, `*.session` — all gitignored.
