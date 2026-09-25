# 9router-azbot

9Router AI gateway + AzBot Telegram leech bot in **one container**, sized for a
single lowest-spec Sevalla instance (0.5 vCPU / 1 GB).

- **9Router** runs in the foreground and owns `$PORT` (Sevalla health probe).
  Prebuilt from `decolua/9router:0.5.86` — no Node build on deploy.
- **AzBot** (`azbot/`, fixed Sep-12 build) runs beside it under a restart
  supervisor. No code changes: env-var driven, state symlinked to disk.

## Layout

- `Dockerfile` — `FROM decolua/9router:0.5.86` + Python/AzBot layer.
- `entrypoint.sh` — starts AzBot loop in background, 9Router in foreground.
- `azbot/` — AzBot source (`bot.py`, `bot_pkg/`, `requirements.txt`).
- `sevalla.env.example` — env vars to paste into the Sevalla dashboard.

## Deploy on Sevalla

1. Dashboard → **Applications → Create → Application** from this repo.
   Build strategy: **Dockerfile**, path `Dockerfile`, context repo root.
2. Instance: smallest available (Hobby / 0.5 CPU · 1 GB).
3. Disk optional — skipped per your choice. Without it `/app/data`
   is ephemeral: redeploy wipes 9Router DB + AzBot settings/cookies.
   Env-based auth (BOT_TOKEN, Drive) survives; chat settings don't.
4. Set env vars from `sevalla.env.example`:
   required: `API_ID`, `API_HASH`, `BOT_TOKEN`, `INITIAL_PASSWORD`.
   Do NOT set `PORT` — Sevalla injects it.
5. Deploy. 9Router dashboard opens at your `*.sevalla.app` URL.

## Low-RAM tuning (already defaulted in image)

`QUEUE_WORKERS=1`, `UPLOAD_WORKERS=1`, `DRIVE_UPLOAD_CONCURRENCY=1`,
`HANDLER_WORKERS=8`. Raise only with RAM headroom — peak memory is what
gets a small box OOM-killed.

## Verify

- Sevalla logs show 9Router listening + `[azbot]` supervisor lines.
- 9Router: open the app URL, log in with `INITIAL_PASSWORD`.
- AzBot: send `/stats` to your bot — build stamp confirms the new code.

## Updating

- 9Router: bump the `FROM decolua/9router:X.Y.Z` tag, redeploy.
- AzBot: replace files under `azbot/` (never commit `.env`, `data/`,
  `downloads/`, `*.session` — all gitignored).
