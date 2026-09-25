# AzLeechBot (Pyrogram rewrite)

A Telegram bot that watches for links (YouTube, Instagram, TikTok, Twitter/X,
Reddit, and hundreds of other sites via `yt-dlp`) and leeches them straight
into the chat — **no command needed**, just paste a link. Uploads go over
Telegram's MTProto layer via [Pyrogram](https://docs.pyrogram.org), not the
HTTP Bot API, so files up to Telegram's real per-file limit for bots
(~2GB) go through natively — no 50MB wall, no local Bot API server needed.

## v18 changelog

Aligned with WZML-X's actual implementation (source-verified, not guessed):

- **Parallel MTProto uploads (`fast_upload.py`)** — the bot-only port of
  WZML-X's tg_transfer idea: big files are striped across MULTIPLE media
  sessions via raw `upload.SaveBigFilePart`, then sent with
  `InputFileBig`. Stock pyrogram serializes every byte through ONE media
  session (4 workers, queue depth 1) — that was your speed ceiling.
  Bot-token-only, no premium/user session; anything <10MB, photos/webp,
  or ANY failure automatically falls back to the stock path, so worst
  case equals previous behavior. Tune with `UPLOAD_SESSIONS` /
  `UPLOAD_WINDOW` in `.env`.
- **yt-dlp de-pinned (WZML-X parity)**: removed our forced
  `player_client=["tv","android","ios","web"]` and the aria2c
  external-downloader hijack for YouTube. WZML-X forces neither; on 2026
  YouTube those pins produce thin format tables ("only one quality") and
  mid-download stalls. yt-dlp's own defaults handle this now. Added their
  bounded retry sleeps (3s flat) so retries don't look like hangs.
  `YT_DLP_OPTIONS` remains as escape hatch.
- **Picker UX**: a single available quality no longer opens a one-button
  menu — it downloads directly (WZML-X behavior).
- **Instagram archives**: stall watchdog raised to 10 minutes AND the
  status shows a live "waiting on Instagram (rate-limit wait)" heartbeat
  instead of freezing silently — v15-v17 killed healthy archives after
  4 quiet minutes.

## v17 changelog

Round 2 — from live runtime logs:

- **Uploads were extremely slow**: the TgCrypto self-test called function
  names that don't exist in pyrogram (`ige_encrypt`, `ctr256`), so it
  failed on EVERY host where TgCrypto was installed and the guard blocked
  perfectly healthy native crypto, forcing the pure-Python fallback
  (~2-3x slower crypto on every byte). Self-test fixed against pyrogram's
  real API (`ige256_*`, `ctr256_encrypt`). `/stats` now shows your active
  crypto mode so this can never hide again.
- **Dead buttons / commands with no answer / EditMessage timeouts**: all
  handlers ran directly inside pyrogram's tiny default thread pool
  (≈CPU-count threads). A single slow operation (YouTube format probe,
  Drive listing waiting on the upload lock) starved that pool, so new
  messages and button taps queued indefinitely. Handlers now run as
  coroutines offloading into a dedicated 16-thread pool (`HANDLER_WORKERS`)
  — the network loop and button handling stay responsive always.
- **Quality selector showed fewer options than other bots**: (a) buttons
  were deduplicated per resolution, hiding real formats — now one button
  PER format id (exact size/container/codec/fps); (b) if a probe returns
  only low resolutions while a cookie profile is active, the bot
  automatically re-probes WITHOUT cookies (cookies often pin yt-dlp to a
  basic client → 360p-only tables); (c) client list is configurable via
  `YT_PLAYER_CLIENTS` in `.env`.
- **Boot warning** `cannot access local variable 'drive'`: leftover local
  imports shadowed the module-level one in datastore (broke boot-time
  settings/cookie restore). All removed.
- **/clean or the hourly purge loop could delete RUNNING jobs' folders**
  mid-download. Purge now skips dirs owned by active jobs.
- **Stop button gave no feedback**: tapping 🛑 now flips the message to
  "Stop requested…" instantly.
- **/drive silent while listing**: replies "☁️ Opening Drive…" immediately,
  then fills in; failures show the actual error instead of silence.
- Python 3.13 compatibility: pyrogram needs an event loop at Client()
  construction; core.py now creates one when missing.

## v16 changelog

Fixes (all verified by `tests/sim_flows.py` — run it with any Python that has the deps):

- **Random restarts during heavy jobs** (e.g. archives inside group topics):
  the TgCrypto segfault-guard ran *after* Pyrogram was imported, so it could
  never actually block the crashing C extension. It now runs in
  `bot_pkg/__init__.py`, guaranteed to execute before any module can import
  pyrogram/tgcrypto, with a loud warning if that order ever regresses.
- **Bare Instagram links** opened the yt-dlp quality picker (then failed):
  auto-leech checks Instagram hosts FIRST now; Instagram always goes to
  gallery-dl. `/l <insta-link>` also works again instead of NameError-ing.
- **Thumbnails missing**: source-video thumbnails are validated +
  ffmpeg-converted to real JPEGs (webp/avif/truncated downloads used to
  silently produce none). Priority: source cover > automatic frame grab
  (thumbnails are fully automatic; there is no /setthumb).
- **No quality shown**: every native video/audio upload gets a
  `🎬 WxH • tier • duration` caption; completion summaries show the file's
  real resolution/duration (IG archive summaries too).
- Quality-picker buttons crashed when tapped (`_do_picker` scoping bug).
- `/zipm` NameError (missing import); datastore NameErrors silently broke
  settings/cookie/index Drive-mirroring; "Send to Telegram" in the /drive
  browser crashed on tap; duplicate-URL guard could stick after gallery jobs;
  disk-purge race in `ensure_free`.


## Setup

1. Get `API_ID` / `API_HASH` from <https://my.telegram.org/apps> (required
   by Pyrogram even for a bot account).
2. Get `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).
3. Copy `.env.example` to `.env` and fill it in.
4. `pip install -r requirements.txt`
5. `python bot.py`

Or with Docker:

```bash
docker build -t azleechbot .
docker run --env-file .env -v $(pwd)/data:/app/data azleechbot
```

## Mirror vs. leech

Like the original bot, the destination is picked by which command you
use — there's no per-chat setting to toggle:

- **Paste a link with no command** → **leeched** (sent back as a Telegram
  file). This is the default for everything.
- `/l <link>` → same as pasting it, explicit leech to Telegram.
- `/m <link>` → **mirror** to Google Drive instead (requires Drive to be
  configured — see below).
- `/zm <link>` / `/zl <link>` → same as `/m`/`/l`, but zipped into one
  file first.

## Payload grammar (every download command)

- **Batch**: `/l <link1> <link2> <link3>` — one job per link, in order.
  Rename applies only when there's exactly one link (otherwise names would
  collide).
- **Rename**: `/m <link> | myname` — the final file is named `myname`; the
  extension is never touched (`myname.mp4` stays `.mp4`, an extensionless
  name keeps the file's own extension).
- **Drive folder**: `/m <link> #holiday` — uploads into the `holiday`
  folder of the selected Drive account (created on demand). Works with
  `/m`, `/zm`, `/zipd` and plain-paste mirrors.

## Multi-account Drive

Define several accounts in `.env` (`GDrive_work_*`, `GDrive_2_*`, …) and
each chat picks its own in `/settings → ☁️ Drive account`. `/drive`
browsing, `/drivesearch`, `/m` uploads and `#folder` creation all follow
that selection. Instagram indexes stay local regardless.

## Instagram indexing (local, per user/type)

Archive indexes live under `data/indexes/<chat>/<user>.<type>.txt` — one
flat ledger per chat/user/type. **Nothing is written to your Drive.**
`/igindex` lists every index for this chat with entry counts and lets you
delete selectively: one type of one user (just Stories), everything of one
user, or wipe all. Deleting an index makes the next archive re-fetch from
scratch.

## Zip leech / mirror

- `/zipl` — reply to a file (or pass direct links) → one zip leeched here.
  Supports batch links + `| name.zip`.
- `/zipm` — same, but the archive goes to the selected Drive account
  (`#folder` works).
- `/unzipl` — reply to an archive (`.zip .rar .7z .tar …`) or pass its
  link; every inner file is sent to this chat. `/unzipm` is an alias.

## Settings & indexes live in Drive

`settings.json` and every Instagram index are mirrored into a single
**AzBotData** folder on the bot's first Drive account:

    AzBotData/
      settings.json
      indexes/<chat>/<user>.<type>.txt

Local copies remain the working set; changes push up automatically,
fresh deploys pull down on boot, and deleting an index in `/igindex`
deletes it in both places. Rename the folder with `DATA_DRIVE_FOLDER=…`.

## Commands

- `/m` / `/l` / `/zm` / `/zl` `<link>` — see above. Also works as a
  **reply**: reply to a file already in the chat (or to a message
  containing a link) with `/m`/`/l` and no argument, and it'll fetch that
  file/link instead of needing a URL typed inline.
- `/settings` — per-chat toggle for auto-clean: when on, a successful job
  deletes your triggering message and the bot's own status message,
  leaving just the delivered file (or, for Drive-only jobs, the status
  message with the Drive link — that's kept since it'd otherwise be the
  only trace the job ran). Applies to the whole chat/group/channel; a
  forum group's topics share one setting, not one each.
- `/ig <profile_url>` — multi-select picker: tap Posts / Reels / Stories /
  Highlights / Tagged to toggle each on or off (Stories is on by default),
  then ▶️ Start to archive everything selected in one go (leeches to
  Telegram; use `/m <profile_url>` for the same picker mirrored to Drive
  instead). Re-running it only pulls what's new since last time. **The
  tracker index lives in your Google Drive folder** (as
  `igtracker_<chat>_<user>_<type>.txt`), pulled down before each run and
  pushed back up after — nothing is kept on the server. Without Drive
  configured, the tracker falls back to a local file in
  `data/ig_archives/` and stays on the server instead.
- `/torrent <magnet or .torrent url>` — force aria2c (leeches to Telegram;
  use `/m` with a magnet link to mirror one to Drive instead).
- `/gallery <url>` — force gallery-dl for a general art/photo platform
  (leeches to Telegram — see exclusions below).
- `/clone <url>` — mirror a website via `wget` (leeches to Telegram).
- `/drive` — full folder-navigating Drive browser: tap into subfolders,
  breadcrumb + "⬆️ Back", file sizes shown right in the list, "📁 New
  folder", jump-to-any-page pagination (not just forward), and tapping a
  file gives 🔗 Open / ✏️ Rename / 🗑 Delete. A folder can be deleted too
  (with confirmation) — except the configured root, which is never
  offered for deletion. Files sent to Drive via `/m`/`/zm` also get
  Rename/Delete buttons right on the completion message. `/drivesearch
  <query>` — search by name across the whole Drive.
- `/cookie` — tap a profile to switch, or the ⚙️ next to it to rename or
  delete that profile. Reply to a Netscape-format `cookies.txt` with
  `/cookie <name>` to add a new one (needed for private Instagram
  accounts, stories, highlights, age-gated videos).
- `/cancel <task_id>` / 🛑 button — stop a running job. The button now
  stays visible for the entire job (download **and** upload, to Telegram
  or Drive) instead of disappearing after the first progress update.
- `/cancelall` — admin: stop every running job.
- `/stats` — disk usage / active jobs.
- `/clean` — admin: purge stale temp files immediately.
- `/sh` — admin: **live persistent shell terminal**. Opens one real bash
  session in a PTY that stays alive between messages — `cd`, exports and
  environment persist across commands, and because it's a genuine PTY you
  get bash's full interactive line editing: ↑/↓ history recall, ←/→ cursor
  movement, TAB completion. Any non-command text you send goes to the
  shell; output streams into a single message that keeps updating. On-screen
  ⛔ Ctrl+C button (plus `/shc`) interrupts long-running commands without
  killing the session; `/shexit` (or the ⏹ button) ends it; idle sessions
  auto-close after 30 minutes. `/sh <command>` still works as a one-shot
  runner for quick things. As with any remote shell this is exactly as
  powerful as SSH access to the box, gated only by Telegram user ID — only
  set `ADMIN_ID` to an account you trust, and don't expose the bot token
  or `.env` to anyone else.
- `/allow <id>` / `/ban <id>` — admin: access control, if `ADMIN_ID` is set.

Google Drive upload is optional — leave `GCP_CLIENT_ID` etc. blank and
`/m`, `/zm`, `/drive`, and `/drivesearch` will just tell you it's not
configured instead of failing silently.

## Reliability

- **Cancel works at every stage**, not just download — including mid-upload
  to Telegram or Drive — and the 🛑 button no longer drops off the status
  message partway through a job (both were bugs in an earlier version).
- **Stalled jobs stop themselves.** `/torrent`, `/gallery`, and `/clone`
  (and the `/ig` archiver) run a watchdog: if a process produces zero
  output for `STALL_TIMEOUT_S` (default 240s — e.g. a torrent with no
  peers, or a site silently blocking gallery-dl), it's terminated and you
  get a clear "⏱️ Stopped — no progress for N minutes" message instead of
  the job hanging forever with a stale status line.
- **Unexpected errors are never silent.** Every command and button is
  wrapped so that if something throws an exception the bot didn't
  anticipate, you get "❌ Something went wrong…" in chat with the actual
  error, instead of the bot just not responding. Harmless "message not
  modified" edits (e.g. tapping a button that leads to content already on
  screen) are swallowed quietly rather than reported as errors.
- **Duplicate updates are ignored, and duplicate processes refuse to start.**
  Two things guard against doubled replies: an in-memory check drops any
  update that somehow gets delivered twice, and — the more important one —
  the bot now takes an OS-level lock on `data/azleechbot.lock` at startup.
  If a second instance tries to start while one's already running (a
  redeploy that didn't cleanly stop the old container, e.g.), it refuses
  to start and logs exactly why, instead of both silently answering every
  command twice. This works across containers too, as long as they mount
  the same `data/` volume.
- **`/m`/`/l`/`/zm`/`/zl` work as replies.** Reply to a file already in the
  chat, or to a message containing a link, with one of these and no
  argument, and it's used instead of requiring a URL typed inline.

## Logging

Everything of note — job start/finish, uploads, Drive calls, cancellations,
admin actions, auth denials, errors — goes through Python's `logging`,
configured in `bot_pkg/log.py`:

- **Console**: printed live, level controlled by `LOG_LEVEL` in `.env`
  (default `INFO`; set `DEBUG` for more detail, e.g. per-worker job pickup).
- **File**: `data/logs/bot.log`, rotated at 10MB with 3 backups kept, so
  it survives a restart and you can `tail -f data/logs/bot.log` on a
  headless host.

Pyrogram's own connection/session logging is kept at `WARNING` so it
doesn't drown out the bot's own logs — bump `logging.getLogger("pyrogram")`
in `bot_pkg/log.py` if you need to debug Pyrogram itself.

## About the WZML-X comparison

A few things adopted from [WZML-X](https://github.com/SilentDemonSD/WZML-X)
(a much larger fork of `mirror-leech-telegram-bot`) that fit cleanly into
this bot's architecture:

- **HTML parse mode everywhere** — WZML-X's approach. Markdown's delimiter
  auto-balancing was producing entity tables that overrun the message text
  (Telegram error `ENTITY_BOUNDS_INVALID`, "❌ Something went wrong") when
  filenames/log lines/URLs contained backticks or brackets. HTML entities
  have deterministic bounds, so the whole error class is gone.
- **Quality picker with sizes on every media link** — `/m`, `/l` and the
  `/yt`-family all probe the video first and offer real resolutions with
  approximate file sizes (`🎬 1080p (486 MB)`), like WZML-X's yt-dlp flow.
  Picking from formats that actually exist also sidesteps "Requested format
  not available" errors.
- **Media metadata pass before upload** — downloads are remuxed
  (`ffmpeg -c copy`, no recoding) and duration/dimensions are read back
  via ffprobe and passed to Telegram. This fixes videos showing as
  "0 seconds" in the player after a DASH merge drops the header tags.
- Command aliases matching its names: `/y`/`/ytdl` (mirror via yt-dlp),
  `/yl`/`/ytdlleech` (leech via yt-dlp) — on top of this bot's own `/m`/`/l`,
  which already auto-detect yt-dlp links so these need no separate code path.
- `YT_DLP_OPTIONS` env var — raw JSON merged into yt-dlp's options, same
  concept as WZML-X's config key of the same name, for tweaking yt-dlp
  behavior without touching code.
- `/settings` → "Always send as document" — skips Telegram's video
  re-encoding/thumbnail generation, same idea as its `AS_DOCUMENT` setting.
- **yt-dlp is NOT auto-updated** (removed by user request): restart the
  bot (after `pip install -U yt-dlp` or an image rebuild) to pick up a
  newer version. YouTube extraction errors like "The page needs to be
  reloaded" are almost always yt-dlp lagging YouTube's changes — update
  and restart when they appear.
- **`extractor_args` player_client fallback** (`tv`, `android`, `web`) —
  YouTube's default web client increasingly requires a PO token that
  yt-dlp can't always obtain; these clients don't, and this is the
  standard community/WZML-X workaround for that specific failure mode.

What I didn't do: make this bot "identical" to it. WZML-X is a much larger,
differently-architected project — MongoDB-backed, with qBittorrent,
JDownloader, SABnzbd, Mega, and rclone as separate service integrations
plus its own web UI — and it's licensed **AGPL-3.0**. Copying its code
directly would mean this bot inherits that license, which has a real
consequence worth knowing: AGPL's network-use clause means if you run a
modified version as a service, you're required to make the complete
source available to anyone who uses it. That's a legal commitment, not
just a style choice, and not something to take on silently. If there's a
specific feature from it you want (torrent file-selection UI before
download, Mega.nz support, rclone as a destination, qBittorrent instead
of aria2c), tell me which one and I'll build that piece specifically
rather than guessing at the whole thing.

## What's intentionally not here

The old curated "gallery site" allowlist/blocklist is gone entirely —
`/gallery` now runs gallery-dl against whatever URL you give it, and
everything `gallery-dl`/`yt-dlp` support works normally.

## What changed from the original `pyTelegramBotAPI` version

- **Library**: `pyTelegramBotAPI` → Pyrogram + TgCrypto, which is what
  actually gets you past the 50MB limit — Pyrogram talks MTProto directly
  instead of going through the HTTP Bot API.
- **Auto-leech**: any message containing a supported URL (or a magnet
  link) is now picked up automatically with no command at all; the
  original always required `/l`, `/m`, etc.
- **Destination**: purely a function of which command you use (`/m` vs
  `/l`, or no command = leech) — no per-chat "destination" setting to
  toggle, matching how the original's `/m`/`/l` pair worked.
- **Instagram tracker**: reimplemented on top of `gallery-dl --download-
  archive` instead of a hand-rolled Drive-backed index — same "only fetch
  what's new" behavior, less custom code, and the ledger file itself is
  still stored in Drive (downloaded before each run, re-uploaded after),
  matching the original's "no data kept on the server" design.
- Torrent (`aria2c`), website mirroring (`wget`), and the admin shell are
  carried over essentially as-is.

## Performance

- **Downloads use aria2c (16 connections)** instead of a single HTTP
  stream, both for direct file links and for plain-HTTP(S) yt-dlp
  downloads — real multi-connection speed instead of one TCP stream
  trickling in. HLS/DASH fragment downloads stay on yt-dlp's own
  downloader, since aria2c doesn't handle segment decryption/merging.
- **Telegram uploads/downloads use 8 concurrent file-part transmissions**
  (Pyrogram defaults to 1), which is the single biggest lever for
  large-file transfer speed over MTProto.
- **Drive uploads use 50MB chunks** instead of 10MB, cutting per-chunk
  HTTP round-trip overhead on large files.

## Notes

- Telegram's ~2GB per-file cap for bots is a platform limit, not a
  library one — it applies the same whether you use Pyrogram, Telethon,
  or a local Bot API server. Files bigger than that get split into parts
  automatically.
- yt-dlp needs occasional updates as sites change their pages; if a
  previously-working site starts failing, `pip install -U yt-dlp` first.
