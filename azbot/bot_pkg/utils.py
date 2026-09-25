import math, time, threading, os, re, zipfile, uuid, hashlib, html as _html

from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import log

logger = log.get(__name__)

def esc(s):
    """HTML-escape dynamic content (& < >) before embedding it in any
    message/caption. The bot runs with parse_mode=HTML globally (the WZML
    approach): unlike markdown, HTML entities have deterministic bounds —
    arbitrary filenames/URLs/log lines can never corrupt the entity table
    (the source of Telegram's ENTITY_BOUNDS_INVALID crashes). Only & <>
    need escaping; markdown metacharacters are literal in HTML mode."""
    return _html.escape(str(s), quote=False)


# ── Command payload parsing (WZML-X-style raw/batch support) ────────────
# Supported in every download command (/m /l /yt /torrent /ig …):
#   • multiple links separated by whitespace → one job per link (batch)
#   • "link | newname"  → saved/uploaded as newname (extension untouched)
#   • "#folder" anywhere → Drive uploads go into that folder (created on
#     demand inside the account's root); also names a zip when zipping.
def parse_payload(raw):
    """Returns (links, rename, folder).
    links: list of URLs/magnets in input order. rename/folder may be None."""
    if not raw:
        return [], None, None
    raw = raw.strip()
    folder = None
    # URL #fragments (…/video#t=10s) are client-side junk, not Drive
    # folders: mask URLs first, then accept only a STANDALONE #folder
    # token from the remaining text.
    _masked = re.sub(r"(?:magnet:\?xt=urn:btih:[A-Za-z0-9]\S*|https?://\S+)", " ", raw)
    fm = re.search(r"#([^\s|]+)", _masked)
    if fm:
        folder = fm.group(1).strip()
        raw = raw.replace("#" + fm.group(1), " ", 1)

    rename = None
    parts = [p.strip() for p in raw.split("|", 1)]
    if len(parts) == 2 and parts[1] and not parts[1].startswith("http"):
        raw, rename = parts[0], parts[1]

    links = []
    for u in re.findall(r"(?:magnet:\?xt=urn:btih:[A-Za-z0-9]\S*|https?://\S+)", raw):
        if u.startswith("http"):
            u = u.split("#", 1)[0]  # client-side fragment, not part of the file
        links.append(u.rstrip(").,]>\"'"))
    return links, rename, folder

def cancel_kb(task_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop", callback_data=f"cancel:{task_id}")]])

def safe_edit(client_or_query, *args, **kwargs):
    """Like edit_message_text, but treats MESSAGE_NOT_MODIFIED as success —
    it only means the content you wanted is already on screen, which isn't
    an error a user should ever see reported as one."""
    try:
        return client_or_query.edit_message_text(*args, **kwargs)
    except MessageNotModified:
        return None

def safe_delete(client, chat_id, *msg_ids):
    """Best-effort message deletion for the auto-clean setting — missing
    permissions, already-deleted messages, etc. should never break a job."""
    ids = [m for m in dict.fromkeys(msg_ids) if m]  # dedupe, drop None/0
    if not ids:
        return
    try:
        client.delete_messages(chat_id, ids)
    except Exception as e:
        logger.debug(f"auto-clean delete failed for {ids} in {chat_id}: {e}")

def autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest):
    """Called only from a job's success path. Deletes the user's triggering
    message, and the bot's own status message too — unless dest is
    drive-only, in which case that status message is the only visible
    record of the result (it holds the Drive link) and is kept."""
    from . import state
    if not state.get_autoclean(chat_id):
        return
    if dest == "drive":
        safe_delete(client, chat_id, reply_to)
    else:
        safe_delete(client, chat_id, msg_id, reply_to)

_seen_updates = {}
_seen_lock = threading.Lock()
_DEDUP_TTL = 8  # seconds

# Dedicated pool for handler bodies (see guarded). Explicit pool instead of
# asyncio.to_thread's default: capacity must not depend on CPU count, and
# run_in_executor(pool, ...) works on whichever loop pyrogram actually runs.
# Reuse config.HANDLER_WORKERS rather than re-reading the env var here too —
# this used to read os.environ.get("HANDLER_WORKERS", "16") independently,
# which (a) duplicated config.py's own copy of the same setting and could
# drift out of sync with it, and (b) had the same blank-env-var crash bug
# fixed in config.py (int("") raises when the var is set but empty).
from concurrent.futures import ThreadPoolExecutor as _TPE
from . import config as _config
AZ_HANDLER_POOL = _TPE(max_workers=_config.HANDLER_WORKERS,
                       thread_name_prefix="azhandler")

def _dedup_key(update):
    """Unique-enough key for an incoming Message or CallbackQuery, used to
    detect the same update being handed to us twice."""
    try:
        if hasattr(update, "data"):  # CallbackQuery
            return ("cb", update.id)
        return ("msg", update.chat.id, update.id)  # Message
    except Exception:
        return None

def _mark_if_new(key):
    """Returns True the first time `key` is seen within the TTL window,
    False on a repeat. Also does a cheap opportunistic cleanup so this
    dict doesn't grow forever."""
    now = time.time()
    with _seen_lock:
        if len(_seen_updates) > 2000:
            for k, t in list(_seen_updates.items()):
                if now - t > _DEDUP_TTL:
                    del _seen_updates[k]
        last = _seen_updates.get(key)
        if last is not None and now - last < _DEDUP_TTL:
            return False
        _seen_updates[key] = now
        return True

def guarded(fn):
    """Wraps a Pyrogram message/callback handler three ways:

    1. OFF-LOOP EXECUTION (v17): Pyrogram runs handlers on the network
       asyncio loop. A sync handler that blocks there — Drive API calls
       waiting on the global API lock during a big upload, ffprobe,
       cookie-file IO — freezes the ENTIRE bot: commands stop answering,
       the 🛑 Stop button goes dead, and pending EditMessages pile up into
       "Request timed out" retry storms. The handler body now runs in a
       thread-pool worker via asyncio.to_thread, so the loop stays free
       no matter what a handler is waiting on. (Pyrogram client calls
       made from those worker threads are marshalled back onto the loop
       by pyrogram's own sync bridge — same as our job worker threads.)
    2. Any exception the handler doesn't catch itself gets reported into
       the chat instead of silently vanishing into the logs.
    3. Defends against the same update being processed twice — e.g. two
       bot instances polling the same token after a redeploy that didn't
       cleanly stop the old process."""
    import functools, asyncio

    @functools.wraps(fn)
    async def wrapper(client, update, *a, **kw):
        key = _dedup_key(update)
        if key and not _mark_if_new(key):
            logger.warning(f"duplicate update suppressed in {fn.__name__}: {key}")
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(AZ_HANDLER_POOL, fn, client, update, *a, **kw)
        except Exception as e:
            logger.exception(f"handler {fn.__name__} raised")
            try:
                chat_id = update.message.chat.id if hasattr(update, "message") and update.message else update.chat.id
            except Exception:
                chat_id = None
            try:
                # Anchor the error report to whatever triggered this handler
                # (the command message, or the message a callback button is
                # attached to) — WZML-X-style: this Pyrogram fork has no
                # message_thread_id param, so a plain send_message with no
                # reply anchor lands in the group's General topic instead of
                # the topic the error actually happened in.
                anchor_msg_id = update.message.id if hasattr(update, "message") and update.message else update.id
            except Exception:
                anchor_msg_id = None
            if chat_id is not None:
                try:
                    await client.send_message(chat_id, f"❌ Something went wrong (`{fn.__name__}`):\n`{str(e)[:300]}`",
                                               reply_to_message_id=anchor_msg_id)
                except Exception:
                    # anchor_msg_id may itself be gone (deleted message) —
                    # don't let that swallow the crash report entirely.
                    try:
                        await client.send_message(chat_id, f"❌ Something went wrong (`{fn.__name__}`):\n`{str(e)[:300]}`")
                    except Exception:
                        logger.warning(f"couldn't even report error for {fn.__name__} to chat {chat_id}")
            if hasattr(update, "answer"):  # CallbackQuery — clear the loading spinner
                try:
                    await update.answer("❌ Error — see chat for details", show_alert=True)
                except Exception:
                    pass
    return wrapper

def fmtsz(b):
    if not b or b <= 0:
        return "0 B"
    n = ("B", "KB", "MB", "GB", "TB")
    i = min(int(math.floor(math.log(b, 1024))), 4)
    return f"{round(b / math.pow(1024, i), 2)} {n[i]}"

def fmt_time(s):
    s = int(max(0, s)); m, s = divmod(s, 60); h, m = divmod(m, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"


# ── Progress bar renderer (used by every download/upload step) ──────────
def pbar(pct, width=12):
    """▓▓▓░░░ style bar. Concise but readable at a glance."""
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    return "▓" * filled + "░" * (width - filled)


def progress_line(label, done, total, elapsed=None):
    """One-line HTML status:
    ⬇️ Downloading ▓▓▓░░░ 42% • 210 MB / 500 MB • 00:12 left
    """
    pct = (done / total * 100) if total else 0
    parts = [f"{esc(label)} {pbar(pct)} <b>{pct:.0f}%</b>"]
    if total:
        parts.append(f"{fmtsz(done)} / {fmtsz(total)}")
    else:
        parts.append(fmtsz(done))
    if elapsed is not None and done > 0 and total > done:
        speed = done / max(elapsed, 0.001)
        eta = (total - done) / speed
        parts.append(f"{fmt_time(eta)} left")
    elif elapsed is not None:
        parts.append(f"⚡ {fmtsz(done / max(elapsed, 0.001))}/s")
    return " • ".join(parts)

def bar(done, total, L=14):
    if total <= 0:
        return "[" + "░" * L + "] 0.0%"
    pct = min(100.0, 100 * done / total)
    f = int(L * pct / 100)
    blocks = "█" * f + "░" * (L - f)
    return f"[{blocks}] {pct:.1f}%"

# (the old bar(done,total) helper was removed — callers use pbar/progress_line directly)

def smooth_speed(samples):
    if len(samples) < 2:
        return "…"
    dt = samples[-1][1] - samples[0][1]
    if dt <= 0:
        return "…"
    return fmtsz(max(0, (samples[-1][0] - samples[0][0]) / dt)) + "/s"

# ── Raw yt-dlp format selector passthrough ────────────────────────────────
# Power users can append " -f <selector>" (yt-dlp's own -f/--format syntax,
# e.g. "/l https://site/v -f 'bv*[height<=720]+ba'") to any leech/mirror
# command. The selector is passed verbatim to yt-dlp's format option —
# no shell, no interpolation. Not supported for IG/magnet/Drive-native
# routes (those libraries have no format concept); -f only affects the
# yt-dlp path and skips the quality picker when present.

YTDLP_FMT_ARG_RE = re.compile(r"\s+(?:-f|--format)\s+(\"[^\"]+\"|'[^']+'|\S+)\s*$")

def split_ytdlp_format(text):
    """Strips a trailing ' -f <yt-dlp selector>' / '--format <sel>' from a
    command payload. Returns (clean_text, selector_or_None)."""
    m = YTDLP_FMT_ARG_RE.search(text or "")
    if not m:
        return text, None
    sel = m.group(1).strip('"').strip("'").strip()
    if len(sel) > 256:
        return text, None
    return text[:m.start()].rstrip(), sel or None


# ── Forum-topic root resolution (thread anchoring without reply headers) ──
# This pyrogram fork has no message_thread_id parameter, and a NEW message
# without an anchor lands in a forum group's General topic. But every
# message already inside a topic carries the topic's root id in its reply
# header, which this fork exposes on the parsed Message as:
#   reply_to_top_message_id  — set on REPLIES inside a topic = topic root
#   reply_to_message_id      — for a DIRECT topic post this IS the topic
#                              root (Telegram anchors every topic message
#                              to its root message id)
# Anchoring sends to that root places them in the topic as plain posts —
# clients show NO reply header for replies to the topic root. PV messages
# carry no header at all → returns 0 → caller sends unanchored, which is a
# completely plain message. (v1 of this helper read message.raw, which
# pyrogram's parsed Message doesn't have — so it silently returned 0 and
# the archive landed in General. That's the bug this rewrite fixes.)

def topic_root_id(message):
    """0 when this message is not inside a forum topic (PV, normal group,
    or a message with no reply context); otherwise the topic's root
    message id, safe to pass as reply_to_message_id so sends land in the
    topic WITHOUT a visible reply header."""
    try:
        from pyrogram.enums import ChatType
        chat = getattr(message, "chat", None)
        if chat is not None and getattr(chat, "type", None) == ChatType.PRIVATE:
            return 0
        top = getattr(message, "reply_to_top_message_id", None)
        if top:
            return int(top)
        rid = getattr(message, "reply_to_message_id", None)
        if rid:
            return int(rid)
    except Exception:
        pass
    return 0


def file_anchor(message):
    """The reply anchor for FILE sends: the forum topic's root (a reply to
    the root renders as a PLAIN post inside the topic — no reply header),
    or None everywhere else (PV, normal groups) so files are sent as plain
    messages instead of replies to the command. Status bubbles keep
    replying to the command; autoclean keeps using the raw command id."""
    return topic_root_id(message) or None

# Telegram allows ~1 edit/sec per chat; we coalesce rapid progress updates
# so download/upload progress hooks (which fire many times a second) don't
# get us flood-limited.

_lock = threading.Lock()
_last_edit = {}
_pending = {}
_timers = {}
_flood_until = {}   # key -> timestamp until which edits must wait out a FloodWait
MIN_GAP = 2.5

def throttled_edit(client, chat_id, msg_id, text, markup=None, force=False):
    if not msg_id:
        return
    key = (chat_id, msg_id)

    def _do():
        with _lock:
            pending = _pending.pop(key, None)
            _timers.pop(key, None)
            if pending is None:
                return
            _text, _markup = pending
            _last_edit[key] = time.time()
        try:
            client.edit_message_text(chat_id, msg_id, _text, reply_markup=_markup)
        except FloodWait as e:
            # FloodWait on an edit used to cascade: the old code SLEPT here
            # inside the timer thread while newer throttled_edit calls kept
            # scheduling fresh timers — every one of them fired into the
            # same flood (N consecutive "Waiting 5s … EditMessage" warnings,
            # each stalling the shared session and every queued
            # SendMultiMedia behind it). Instead: block THIS message's edits
            # until the flood expires and re-queue the text — one clean edit
            # after the flood, newest text wins.
            with _lock:
                _flood_until[key] = time.time() + e.value + 1
                _pending[key] = (_text, _markup)
                t = threading.Timer(e.value + 1, _do)
                _timers[key] = t
                t.start()
        except MessageNotModified:
            pass
        except Exception as e:
            # Don't let a failed cosmetic edit vanish silently — ENTITY_BOUNDS /
            # entity-parse errors used to surface here as bare "[edit] ..." prints.
            logger.warning(f"edit_message_text failed for ({chat_id}, {msg_id}): {type(e).__name__}: {e}")

    with _lock:
        now = time.time()
        gap = now - _last_edit.get(key, 0)
        flood_left = _flood_until.get(key, 0) - now
        _pending[key] = (text, markup)
        existing = _timers.get(key)
        if existing:
            existing.cancel()
        delay = 0.05 if (force or gap >= MIN_GAP) else (MIN_GAP - gap + 0.05)
        if flood_left > 0:
            # A flood is active for this message — don't fire into it; the
            # edit goes out right after Telegram stops rejecting it.
            delay = max(delay, flood_left + 0.05)
        t = threading.Timer(delay, _do)
        _timers[key] = t
        t.start()

def safe_send(client, chat_id, text, **kwargs):
    try:
        return client.send_message(chat_id, text, **kwargs)
    except FloodWait as e:
        time.sleep(e.value + 1)
        return client.send_message(chat_id, text, **kwargs)

def new_task_id():
    return str(uuid.uuid4())[:8]

# Extensions yt-dlp/gallery-dl sometimes fall back to when they can't
# positively identify a container — worth re-checking via magic bytes so
# the file doesn't end up captioned/uploaded as "*.unknown_video".
_JUNK_EXTS = {"unknown_video", "unknown_audio", "unknown_video_or_audio", "unknown", "na", "bin", ""}

_MAGIC_SNIFFERS = (
    (lambda h: h[4:8] == b"ftyp", "mp4"),
    (lambda h: h[:4] == b"\x1a\x45\xdf\xa3", "mkv"),
    (lambda h: h[:3] == b"\xff\xd8\xff", "jpg"),
    (lambda h: h[:8] == b"\x89PNG\r\n\x1a\n", "png"),
    (lambda h: h[:6] in (b"GIF87a", b"GIF89a"), "gif"),
    (lambda h: h[:4] == b"RIFF" and h[8:12] == b"WEBP", "webp"),
    (lambda h: h[:4] == b"%PDF", "pdf"),
    (lambda h: h[:4] == b"PK\x03\x04", "zip"),
    (lambda h: h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and h[1] & 0xE0 == 0xE0), "mp3"),
    (lambda h: h[:2] == b"\x1f\x8b", "gz"),
    # MPEG-TS: 0x47 sync byte, repeating every 188 bytes — common for raw
    # HLS-style CDN streams that have no ISO/EBML header at all.
    (lambda h: len(h) >= 189 and h[0] == 0x47 and h[188] == 0x47, "ts"),
)

def sniff_ext(fpath):
    """Best-effort container detection from magic bytes. Returns an
    extension (no dot) or None if nothing matched."""
    try:
        with open(fpath, "rb") as f:
            head = f.read(256)
    except OSError:
        return None
    for check, ext in _MAGIC_SNIFFERS:
        if check(head):
            return ext
    return None

# ffprobe's format_name -> a sane file extension. Its output can list
# several comma-separated aliases for one demuxer (e.g. the ISO-BMFF
# family); first plausible one wins.
_FFPROBE_EXT_MAP = {
    "mov": "mp4", "mp4": "mp4", "m4a": "m4a", "3gp": "mp4", "3g2": "mp4", "mj2": "mp4",
    "matroska": "mkv", "webm": "webm",
    "mpegts": "ts", "mpeg": "mpg",
    "avi": "avi", "flv": "flv", "asf": "wmv", "ogg": "ogg", "wav": "wav",
    "flac": "flac", "mp3": "mp3", "aac": "aac",
}

def sniff_ext_ffprobe(fpath):
    """Deeper fallback for when magic-byte sniffing comes up empty —
    covers raw MPEG-TS and anything else without a recognizable header at
    the start of the file, since ffprobe actually demuxes rather than
    pattern-matching a handful of bytes. Needs ffprobe on PATH (it ships
    alongside ffmpeg, which this project already requires)."""
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
             "-of", "default=noprint_wrappers=1:nokey=1", fpath],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip().lower()
    except Exception:
        return None
    for name in out.split(","):
        if name in _FFPROBE_EXT_MAP:
            return _FFPROBE_EXT_MAP[name]
    return None

def fix_unknown_ext(fpath):
    """If fpath's extension looks like a placeholder yt-dlp/gallery-dl
    couldn't resolve, work out the real container and rename to match.
    Tries cheap magic-byte sniffing first, then falls back to ffprobe
    (which correctly identifies raw MPEG-TS and other containers that
    don't have a distinctive header at byte 0). Returns the (possibly
    renamed) path."""
    ext = os.path.splitext(fpath)[1].lstrip(".").lower()
    if ext not in _JUNK_EXTS:
        return fpath
    real_ext = sniff_ext(fpath) or sniff_ext_ffprobe(fpath)
    if not real_ext:
        return fpath
    new_path = os.path.splitext(fpath)[0] + "." + real_ext
    try:
        os.rename(fpath, new_path)
        return new_path
    except OSError:
        return fpath

def split_dash_args(text):
    """WZML-style raw passthrough: everything after a standalone '--' token
    is a verbatim arg list for the underlying downloader binary
    (gallery-dl, wget…). Example:
        /gallery <url> -- --range 1-5 --verbose
    → ('<url>', ['--range', '1-5', '--verbose'])
    Using a bare '--' separator (standard CLI convention) keeps raw flags
    unambiguous against rename (#folder) and | name grammar."""
    parts = (text or "").split()
    if "--" in parts:
        i = parts.index("--")
        return " ".join(parts[:i]), parts[i + 1:]
    return text or "", []


def zip_dir(src, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for root, _, files in os.walk(src):
            for f in files:
                fp = os.path.join(root, f)
                if fp == out:
                    continue
                zf.write(fp, os.path.relpath(fp, src))
    return out

# Streaming granularity for iter_split_parts: pieces are read/written in
# 8MB slices so RAM stays flat no matter how big the part (the old
# implementation read an ENTIRE ≤2GB part into memory in one read() — a
# guaranteed OOM kill on low-RAM hosts for any >2GB file).
_SPLIT_IO_CHUNK = 8 * 1024 * 1024

def iter_split_parts(fpath, chunk_bytes):
    """Lazily yields byte-split part paths, keeping only ONE part on disk
    at a time (the caller uploads then deletes each before asking for the
    next). Streams off the source in 8MB pieces: constant RAM, constant
    extra disk — the old pre-materialized `split_file` spiked both with
    file size (2GB per part in RAM, every part on disk at once)."""
    n = 0
    with open(fpath, "rb") as src:
        while True:
            n += 1
            pp = f"{fpath}.part{n:03d}"
            wrote = 0
            with open(pp, "wb") as dst:
                while wrote < chunk_bytes:
                    piece = src.read(min(_SPLIT_IO_CHUNK, chunk_bytes - wrote))
                    if not piece:
                        break
                    dst.write(piece)
                    wrote += len(piece)
            if wrote == 0:   # exact-multiple edge: no empty trailing part
                try:
                    os.remove(pp)
                except OSError:
                    pass
                return
            yield pp


def file_md5(fpath, chunk=4 * 1024 * 1024):
    """MD5 of a file, streamed (4MB chunks — flat RAM on any size)."""
    h = hashlib.md5()
    with open(fpath, "rb") as fh:
        for piece in iter(lambda: fh.read(chunk), b""):
            h.update(piece)
    return h.hexdigest()


def zip_tail_ok(fpath, tail=131072):
    """True if the file ends with a zip End-Of-Central-Directory record
    (classic or zip64). A split source without it is already truncated —
    every part produced from it will reassemble into an unopenable zip."""
    try:
        size = os.path.getsize(fpath)
        with open(fpath, "rb") as fh:
            fh.seek(max(0, size - tail))
            buf = fh.read(tail)
            # PK\x05\x06 = classic EOCD; PK\x06\x06 = zip64 EOCD record;
            # PK\x06\x07 = zip64 EOCD locator (big >4GB archives carry the
            # zip64 structures; checking only the classic signature
            # false-rejects healthy large zips as "truncated").
            return (b"PK\x05\x06" in buf
                    or b"PK\x06\x06" in buf
                    or b"PK\x06\x07" in buf)
    except OSError:
        return False
