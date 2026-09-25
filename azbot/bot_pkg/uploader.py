import os, time, shutil, re

from pyrogram.errors import FloodWait
from pyrogram.enums import ParseMode

from . import config, state, log
from .utils import fmtsz, fmt_time, smooth_speed, throttled_edit, iter_split_parts, cancel_kb, progress_line, file_md5, zip_tail_ok

logger = log.get(__name__)

CancelledError = state.CancelledError

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus"}
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
# .webp is deliberately NOT in PHOTO_EXT: Telegram turns small webp uploads
# into STICKERS. Send webp as an animation if it's animated, otherwise as a
# plain document — either way it stays a real file, not a sticker.
STICKERISH_EXT = {".webp", ".tgs"}

def _is_animated_webp(fpath):
    """WEBP with an ANIM chunk = animated (send as animation); otherwise
    static. Reads just the first 64 bytes."""
    try:
        with open(fpath, "rb") as f:
            head = f.read(64)
        return b"ANIM" in head
    except OSError:
        return False

def _progress_cb(client, chat_id, msg_id, label, task_id=None):
    samples = []
    last_edit = [0.0]
    # label may be a plain string OR a callable(current, total) -> text.
    # The dispatcher passes a callable that renders the UNIFIED view
    # (folder header + upload line + any concurrent download line) so
    # both sides of an overlap stay visible in one coherent message.
    def cb(current, total):
        if task_id and state.is_cancelled(task_id):
            raise CancelledError("upload cancelled by user")
        now = time.time()
        if now - last_edit[0] < 1.5 and current != total:
            return
        last_edit[0] = now
        samples.append((current, now))
        if len(samples) > 6:
            samples.pop(0)
        if callable(label):
            text = label(current, total)
        else:
            text = (f"{label}\n"
                    f"{progress_line('☁️', current, total)}\n"
                    f"⚡ {smooth_speed(samples)}")
        markup = cancel_kb(task_id) if task_id else None
        throttled_edit(client, chat_id, msg_id, text, markup=markup, force=(current == total))
    return cb

def _clean_caption(fpath):
    """File names (especially from generic-extractor/gallery-dl output)
    can contain brackets, asterisks, underscores etc. that Telegram's
    default markdown parser misreads as formatting — that's what produced
    the garbled reply + phantom link-preview bubble. Captions are sent
    with parse_mode disabled below, but keep this human-readable too.
    Also strips yt-dlp's trailing ' [id]' suffix — generic-extractor ids
    are 32-char URL hashes, not human content ("IPZZ-541 FHD CH.mp4
    [1ariUO9…ld]" reads terribly as a caption)."""
    name = os.path.splitext(os.path.basename(fpath))[0]
    name = re.sub(r"\s*\[[0-9A-Za-z_-]{16,64}\]$", "", name)   # yt-dlp [id]
    name = name.replace("_", " ").strip()
    return name[:1020] or "file"


def _quality_line(video_meta):
    """'1920×1080 • 12:34' for the completion summary / caption — the
    'no quality is shown to me' fix: resolution comes from the same ffprobe
    pass that feeds the upload attributes, so it's always the file's REAL
    dimensions, never guessed from the URL or format table."""
    if not video_meta:
        return ""
    w, h = int(video_meta.get("width") or 0), int(video_meta.get("height") or 0)
    dur = int(video_meta.get("duration") or 0)
    parts = []
    if w and h:
        parts.append(f"{w}×{h}")
        # friendly tier label on top of raw pixels
        if h >= 2160: parts.append("4K")
        elif h >= 1440: parts.append("2K")
        elif h >= 1080: parts.append("1080p")
        elif h >= 720: parts.append("720p")
        elif h >= 480: parts.append("480p")
    if dur:
        parts.append(fmt_time(dur))
    return " • ".join(parts)

def _normalize_thumb_jpeg(src_path):
    """Any image bytes on disk → guaranteed-decodable ≤320px JPEG at
    src_path+".norm.part000.jpg". Telegram rejects thumbnails that aren't
    real JPEGs (webp/avif/truncated downloads included) and then just
    shows no thumb at all — this conversion is what makes every thumbnail
    path reliable. Returns the converted path or None.

    The ".partNNN" segment in the name is deliberate: LiveDispatcher's
    scanner (SPLIT_PART_RE) skips it, so the temp file is never mistaken
    for an uploadable artifact — while the file STILL ends in ".jpg",
    because ffmpeg picks its output muxer from the extension and dies
    with "Unable to choose an output format" on a bare ".part" name
    (exactly the bug that produced black thumbnails)."""
    import subprocess
    out = src_path + ".norm.part000.jpg"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-threads", "2", "-i", src_path,
             "-vf", "scale='min(720,iw)':-2", "-q:v", "2", "-frames:v", "1", out],
            capture_output=True, timeout=30)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        _cleanup(out)
    except Exception as e:
        logger.warning(f"thumb normalize failed for {src_path}: {e}")
    return None


def _thumb_from_url(url, fpath):
    """Downloads a source video's own thumbnail and converts it into a
    usable Telegram thumbnail. v15 wrote the raw response bytes to .jpg and
    hoped — webp/avif payloads or truncated downloads silently produced
    'no thumbnail'. Now size-checked AND ffmpeg-normalized. Temp files end
    in ".part" so the dispatcher's scanner skips them (they exist in the
    task dir while other uploads may already be running)."""
    try:
        import requests as _rq
        _r = _rq.get(url, timeout=15)
        if not (_r.ok and len(_r.content) > 2000):
            return None
        # ".srcdl.part" ends in .part (dispatcher skips it); this one is
        # only ever READ by ffmpeg as an input (muxer inference doesn't
        # apply to inputs), and its converted output gets the real name.
        raw = fpath + ".srcdl.part"
        with open(raw, "wb") as fh:
            fh.write(_r.content)
        norm = _normalize_thumb_jpeg(raw)
        _cleanup(raw)
        return norm
    except Exception as e:
        logger.debug(f"source thumbnail fetch failed: {e}")
        return None


def _send_one(client, chat_id, fpath, msg_id, caption=None, reply_to=None, task_id=None,
              video_meta=None, thumb_url=None, progress_label=None):
    auto_thumbs = []   # temp frame-grab files, deleted after upload
    ext = os.path.splitext(fpath)[1].lower()
    as_doc = state.get_as_document(chat_id)
    # "no quality is shown to me" fix: native video/audio uploads get a
    # real resolution/duration line appended to their caption. Base caption
    # is capped at 990 chars so cap + quality line always stays under
    # Telegram's 1024-char caption limit (long gallery-dl names + prefix
    # captions could otherwise overflow and reject the send).
    cap = (caption or _clean_caption(fpath))[:990]
    meta = video_meta or {}
    if not as_doc:
        _q = _quality_line(meta)
        if _q and (ext in VIDEO_EXT or ext in AUDIO_EXT):
            cap = f"{cap}\n🎬 {_q}"
    lbl = progress_label or "☁️ Uploading…"
    kwargs = dict(
        chat_id=chat_id,
        caption=cap,
        parse_mode=ParseMode.DISABLED,  # captions are arbitrary file names, not markdown
        progress=_progress_cb(client, chat_id, msg_id, lbl, task_id=task_id),
        reply_to_message_id=reply_to,
    )
    # WZML-X passes the media's real duration/dimensions to Telegram so the
    # player shows them and streaming thumbnails work. Values come from an
    # ffprobe of the final file (see engine.probe_video_meta) — a missing
    # duration is exactly why uploads used to display as "0 seconds".
    duration = int(meta.get("duration") or 0) or None
    width = int(meta.get("width") or 0) or None
    height = int(meta.get("height") or 0) or None
    # Natural thumbnails only: the source video's own cover (YouTube etc.),
    # else an automatic ffmpeg frame grab from the file itself. No user-set
    # images — the custom-thumbnail feature was deliberately removed.
    # Audio gets none: Telegram's default waveform card is correct there.
    thumb = None
    if ext in VIDEO_EXT:
        if thumb_url:
            _t = _thumb_from_url(thumb_url, fpath)
            if _t:
                auto_thumbs.append(_t)
                thumb = _t
        if not thumb and shutil.which("ffmpeg"):
            thumb = _auto_video_thumb(fpath, duration)
            if thumb:
                auto_thumbs.append(thumb)   # deleted after upload
    # ── WZML-X-style striped upload (v18) ────────────────────────────────
    # Big media goes over MULTIPLE parallel MTProto media sessions
    # (bot-token-only — no premium/user session involved); anything smaller,
    # photos/webp, or any failure uses stock pyrogram unchanged.
    # Worst case == old path, never worse.
    from . import fast_upload
    size_now = os.path.getsize(fpath)
    # RAM gate: the striped path's 2 media sessions + pipelined buffers are
    # cheap normally, but on the OOM-prone container they stack on top of
    # whatever else is running (a Drive download's chunk buffers, ffmpeg).
    # Under memory pressure, auto-fall back to the stock single-session
    # path — the profile that never restarted the bot. Worst case == old
    # path, never worse.
    ram_ok = state.ram_free_mb() >= 150
    # Byte-split parts are integrity-critical: the set only reassembles if
    # EVERY byte is exact, and bot-split sets kept coming back without their
    # end-of-archive index while 3rd-party multipart sets (which never touch
    # this uploader) unzip fine — so parts ride the stock single-session
    # path: slower, but the battle-tested byte-exact profile.
    is_part = bool(re.search(r"\.part\d{3}$", fpath, re.I))
    use_striped = (ram_ok and not is_part and size_now > fast_upload.BIG_FILE
                   and ext not in PHOTO_EXT and ext != ".webp")
    if not ram_ok and size_now > fast_upload.BIG_FILE and ext not in PHOTO_EXT:
        logger.info(f"striped upload skipped — low RAM "
                    f"({state.ram_free_mb()}MB free), using stock path")
    if use_striped:
        if as_doc:
            kind = "document"
        elif ext in VIDEO_EXT:
            kind = "video"
        elif ext in AUDIO_EXT:
            kind = "audio"
        else:
            kind = "document"
    else:
        kind = None

    for attempt in range(3):
        try:
            if kind and attempt == 0:
                # fast path first; ANY problem -> stock path below
                try:
                    fast_upload.run_parallel_send(
                        client, chat_id, fpath,
                        kind="document" if as_doc else kind,
                        caption=cap,
                        duration=duration, width=width, height=height,
                        performer="AzLeechBot" if kind == "audio" else None,
                        thumb_path=thumb,
                        reply_to=reply_to,
                        task_id=task_id,
                        cancel_check=(lambda: bool(task_id and state.is_cancelled(task_id))),
                        progress_cb=_progress_cb(client, chat_id, msg_id,
                                                 lbl, task_id=task_id),
                    )
                    logger.info(f"uploaded to Telegram (striped): {fpath} "
                                f"({fmtsz(os.path.getsize(fpath))}) -> chat {chat_id}")
                    for t in auto_thumbs:
                        _cleanup(t)
                    return True
                except CancelledError:
                    raise
                except Exception as fe:
                    logger.warning(f"striped upload unavailable for {fpath}: {fe} — using stock path")

            as_doc_now = state.get_as_document(chat_id)
            if as_doc_now:
                r = client.send_document(document=fpath, **kwargs)
            elif ext in VIDEO_EXT:
                r = client.send_video(video=fpath, supports_streaming=True,
                                       duration=duration, width=width, height=height,
                                       thumb=thumb, **kwargs)
            elif ext in AUDIO_EXT:
                r = client.send_audio(audio=fpath, duration=duration, performer="AzLeechBot",
                                       thumb=thumb, **kwargs)
            elif ext in PHOTO_EXT:
                # Gallery-first: EVERY photo goes as a photo (any size —
                # small ones render as photo messages, never sticker
                # cards), unless the user forced as-document. A file
                # Telegram genuinely refuses as a photo is retried as a
                # document by the fallback below.
                if not as_doc:
                    r = client.send_photo(photo=fpath, **kwargs)
                else:
                    r = client.send_document(document=fpath, **kwargs)
            elif ext == ".gif":
                # Native looping GIF — autoplays in clients (the standard
                # Telegram GIF experience, not a sticker card).
                # Duration/dims arrive via video_meta (see
                # MEDIA_PROBE_EXTS); thumb is None here (frame-grab thumbs
                # are built for videos only).
                r = client.send_animation(animation=fpath, duration=duration,
                                          width=width, height=height,
                                          thumb=thumb, **kwargs)
            elif ext == ".webp":
                # ALWAYS a plain document — even ANIMATED webp sent via
                # send_animation renders as an inline looping bubble that
                # looks exactly like a sticker in most clients, and leech
                # users expect real files. (Static webp was already doc.)
                r = client.send_document(document=fpath, **kwargs)
            else:
                r = client.send_document(document=fpath, **kwargs)
            logger.info(f"uploaded to Telegram: {fpath} ({fmtsz(os.path.getsize(fpath))}) -> chat {chat_id}")
            for t in auto_thumbs:
                _cleanup(t)
            return r
        except CancelledError:
            for t in auto_thumbs:
                _cleanup(t)
            logger.info(f"[{task_id}] upload cancelled by user")
            raise
        except FloodWait as e:
            logger.warning(f"FloodWait {e.value}s while sending {fpath}")
            time.sleep(e.value + 1)
        except Exception as e:
            name = type(e).__name__
            # PHOTO_INVALID_DIMENSIONS (and friends) mean Telegram refuses
            # this image as a photo — usually extreme dimensions or an odd
            # aspect ratio from web-scraped images. Retry ONCE as a plain
            # document, which always works and preserves the file exactly.
            if ext in PHOTO_EXT and attempt == 0 and (
                    "PHOTO_INVALID_DIMENSIONS" in str(e) or
                    "PHOTO_EXT" in name or "Invalid" in name or "Dimensions" in name):
                logger.warning(f"{name} for {fpath} — falling back to document")
                try:
                    r = client.send_document(document=fpath, **kwargs)
                    logger.info(f"uploaded as document after photo-dimension error: {fpath}")
                    return r
                except Exception as e2:
                    raise e2  # the fallback's error, not the masked original
            logger.warning(f"send attempt {attempt + 1}/3 failed for {fpath}: {e}")
            if attempt == 2:
                for t in auto_thumbs:
                    _cleanup(t)
                raise
            time.sleep(2)

    # The loop only falls through when ALL 3 attempts ended in FloodWait:
    # nothing was ever sent. Raise instead of silently returning None —
    # callers treat a normal return as "delivered" and delete the file —
    # and don't leak the temp thumbnail files either.
    for t in auto_thumbs:
        _cleanup(t)
    raise RuntimeError("upload kept hitting FloodWait — file was NOT sent")

def upload_to_telegram(client, chat_id, fpath, msg_id, reply_to=None, task_id=None,
                       video_meta=None, thumb_url=None, caption=None, progress_label=None):
    """Uploads fpath natively over MTProto (Pyrogram). Files above Telegram's
    ~2GB cap are split — WZML-X style: VIDEOS get ffmpeg-segmented into
    playable parts (each with correct duration/metadata), everything else is
    byte-split. Parts are uploaded and deleted one by one (low RAM/disk).

    caption — overrides the default "cleaned filename" caption (dispatcher
    uses it to tag IG files with their type). progress_label — str or
    zero-arg callable rendered on every progress edit (dispatcher passes a
    callable for the live 'sent X/N' header)."""
    size = os.path.getsize(fpath)
    if video_meta is None:
        # callers that don't know the media shape (dispatcher, zip, drive-send)
        # still get correct duration/dimensions + quality captions for free
        video_meta = probe_meta_if_media(fpath)
    if size <= config.TG_MAX_FILE_BYTES:
        return [_send_one(client, chat_id, fpath, msg_id, reply_to=reply_to, task_id=task_id,
                          video_meta=video_meta, thumb_url=thumb_url,
                          caption=caption, progress_label=progress_label)]

    ext = os.path.splitext(fpath)[1].lower()
    if ext in VIDEO_EXT and shutil.which("ffmpeg"):
        return _split_upload_video(client, chat_id, fpath, msg_id, reply_to, task_id)

    logger.info(f"{fpath} is {fmtsz(size)}, splitting for upload")
    # Split-time integrity gate: a valid zip ALWAYS ends with its central-
    # directory index. If the source doesn't, the download that produced it
    # was truncated — sending parts of it produces a set that can NEVER
    # reassemble into a working zip. Fail the leech with the reason instead
    # of burning 20 minutes of upload on a dead set.
    if ext == ".zip" and not zip_tail_ok(fpath):
        logger.warning(f"{fpath} has no zip end-of-archive index — source looks truncated")
        raise RuntimeError(
            "Source zip is truncated: its end-of-archive index is missing, so the "
            "download that produced it was cut off before finishing. Splitting it "
            "would only produce parts that can never reassemble into a working zip. "
            "Re-fetch the source (or repair it locally with `zip -FF`) and try again.")
    throttled_edit(client, chat_id, msg_id,
                    f"✂️ File is {fmtsz(size)}, splitting into parts (Telegram's "
                    f"~2GB per-file limit applies to every client, Pyrogram included)…",
                    markup=cancel_kb(task_id) if task_id else None, force=True)
    # Parts are produced lazily — ONE on disk at a time, streamed off the
    # source in 8MB pieces — so neither RAM nor disk spikes with file size.
    total = -(-size // config.TG_MAX_FILE_BYTES)
    results = []
    for i, p in enumerate(iter_split_parts(fpath, config.TG_MAX_FILE_BYTES), 1):
        if task_id and state.is_cancelled(task_id):
            _cleanup(p)
            raise CancelledError("upload cancelled by user")
        # Fingerprint each part AT SPLIT TIME. The unzip job re-checks it
        # after download — a mismatch pinpoints which side (upload vs
        # download) altered the bytes, which is otherwise undetectable:
        # sizes survive corruption intact.
        want_md5 = file_md5(p)
        caption = f"{_clean_caption(fpath)}.part{i:03d}/{total:03d}\nMD5 {want_md5}"
        results.append(_send_one(client, chat_id, p, msg_id, caption=caption, reply_to=reply_to,
                                 task_id=task_id))
        _cleanup(p)   # delete each part right after its upload
    try:
        os.remove(fpath)
    except OSError:
        pass
    return results


def _split_upload_video(client, chat_id, fpath, msg_id, reply_to, task_id):
    """WZML-X parity: split a >2GB VIDEO with ffmpeg stream-copy into
    equal-duration segments that remain playable files with real metadata
    (not raw byte slices). Each segment uploads then deletes immediately."""
    import subprocess as sp
    meta = engine_probe_meta(fpath)
    duration = float(meta.get("duration") or 0)
    if duration <= 0:
        # can't determine duration — fall back to byte split (lazily, one
        # part on disk at a time — see iter_split_parts)
        size = os.path.getsize(fpath)
        total = -(-size // config.TG_MAX_FILE_BYTES)
        out = []
        for i, p in enumerate(iter_split_parts(fpath, config.TG_MAX_FILE_BYTES), 1):
            caption = f"{_clean_caption(fpath)}.part{i:03d}/{total:03d}"
            out.append(_send_one(client, chat_id, p, msg_id, caption=caption,
                                 reply_to=reply_to, task_id=task_id))
            _cleanup(p)
        try:
            os.remove(fpath)
        except OSError:
            pass
        return out

    n_parts = -(-os.path.getsize(fpath) // config.TG_MAX_FILE_BYTES)   # ceil
    seg_len = duration / n_parts
    base = os.path.splitext(fpath)[0]
    ext = os.path.splitext(fpath)[1] or ".mp4"
    total = n_parts
    results = []
    seg_errors = []
    throttled_edit(client, chat_id, msg_id,
                    f"✂️ Video is {fmtsz(os.path.getsize(fpath))} — segmenting into "
                    f"{total} playable parts…", force=True)
    vmeta = {"duration": int(seg_len), "width": int(meta.get("width") or 0),
             "height": int(meta.get("height") or 0)}
    for i in range(1, total + 1):
        if task_id and state.is_cancelled(task_id):
            raise CancelledError("upload cancelled by user")
        start = (i - 1) * seg_len
        part = f"{base}.part{i:03d}{ext}"
        args = ["ffmpeg", "-y", "-loglevel", "error", "-threads", "2",
                "-ss", f"{start:.3f}", "-i", fpath, "-t", f"{seg_len:.3f}",
                "-map", "0", "-c", "copy", "-movflags", "+faststart", part]
        r = sp.run(args, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
            err = (r.stderr[-200:] if r.stderr else "empty output").strip()
            logger.warning(f"segment {i}/{total} failed: {err}")
            seg_errors.append(f"part{i}: {err[:120]}")
            continue
        caption = f"{_clean_caption(fpath)}.part{i:03d}/{total:03d}"
        results.append(_send_one(client, chat_id, part, msg_id, caption=caption,
                                 reply_to=reply_to, task_id=task_id, video_meta=vmeta))
        _cleanup(part)   # delete right after upload — our own rule
    if not results:
        # Total failure: keep the source and say so — the old code deleted
        # it and returned [], which the caller counted as sent.
        raise RuntimeError(f"video split failed ({total} segments): " + "; ".join(seg_errors[:3]))
    if seg_errors:
        logger.warning(f"video split partial ({len(results)}/{total} sent): " + "; ".join(seg_errors[:3]))
    try:
        os.remove(fpath)
    except OSError:
        pass
    return results


def engine_probe_meta(fpath):
    """Lazy import to avoid a circular import at module load."""
    from . import engine
    return engine.probe_video_meta(fpath)


def probe_meta(fpath):
    """ffprobe wrapper that never throws and never shells out needlessly:
    {} for missing tools or non-media files (cheap header read only)."""
    try:
        if not shutil.which("ffprobe"):
            return {}
        return engine_probe_meta(fpath)
    except Exception:
        return {}


MEDIA_PROBE_EXTS = VIDEO_EXT | AUDIO_EXT | {".gif"}

def probe_meta_if_media(fpath):
    """probe_meta, but skips the ffprobe subprocess entirely for files that
    can't be video/audio (photos, docs, archives gain nothing from it).
    Each avoided call is one fewer process spawn — measurable on low-spec
    hosts and multi-thousand-file Instagram archives."""
    if os.path.splitext(fpath)[1].lower() in MEDIA_PROBE_EXTS:
        return probe_meta(fpath)
    return {}


def _image_dimensions_fast(fpath):
    """(width, height) from image file HEADERS — pure Python, microseconds,
    zero subprocesses. Covers JPEG (SOF0–SOF3 scan), PNG (IHDR), GIF and
    BMP. Returns (0, 0) for anything unparseable/truncated (treated as
    small → document, the safe direction). This is what lets _send_one
    enforce the ≥480px photo rule on hundred-image folders without the
    ffprobe-per-photo cost."""
    import struct
    try:
        with open(fpath, "rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return 0, 0
    try:
        if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h)
        if len(head) >= 10 and head[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", head[6:10])
            return int(w), int(h)
        if len(head) >= 26 and head[:2] == b"BM":
            w, h = struct.unpack("<ii", head[18:26])
            return int(abs(w)), int(abs(h))
        if len(head) >= 4 and head[:2] == b"\xff\xd8":
            i, n = 2, len(head)
            while i + 4 < n:
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker = head[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    h, w = struct.unpack(">HH", head[i + 5:i + 9])
                    return int(w), int(h)
                if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = struct.unpack(">H", head[i + 2:i + 4])[0]
                if seg_len < 2:
                    break
                i += 2 + seg_len
    except Exception:
        pass
    return 0, 0


def _image_dimensions(fpath):
    """(width, height) via ffprobe — used to decide photo vs document."""
    import subprocess
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", fpath],
        capture_output=True, text=True, timeout=20)
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def _auto_video_thumb(fpath, duration):
    """Grabs a real frame from the video (~1/3 in, avoids black intros) as a
    320px-wide JPEG for Telegram's thumbnail. Returns temp path or None.
    The caller deletes it after upload — nothing is stored anywhere.
    Named "<video>.thumb.part000.jpg": the ".partNNN" segment makes
    LiveDispatcher's scanner skip it, while the ".jpg" ending keeps
    ffmpeg's output-muxer inference working (a bare ".part" suffix broke
    the grab — that's the black-thumbnail regression)."""
    import subprocess
    try:
        pos = max(1.0, (duration or 0.0) / 3.0)
        out = fpath + ".thumb.part000.jpg"
        # Low-RAM guard: decoding a large video (4K/HEVC) spikes ffmpeg's
        # RSS by hundreds of MB — on this host that spike, stacked on top
        # of the download subprocess + MTProto buffers, is what triggers
        # the OOM kill. The thumbnail is cosmetic; skipping it under
        # memory pressure beats losing the whole container.
        if state.ram_free_mb() < 200:
            logger.debug(f"thumb grab skipped — low RAM ({state.ram_free_mb()}MB free)")
            return None
        # 720px @ q2: Telegram officially caps thumbs at 320px but accepts
        # larger JPEGs and downsizes internally — downscaling in Telegram
        # from a 720px source stays much sharper than feeding it a 320px
        # JPEG re-encoded twice (the old tiny+default-quality grab looked
        # mushy on phones). q2 = near-lossless JPEG.
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-threads", "2",
             "-ss", f"{pos:.2f}", "-i", fpath,
             "-frames:v", "1", "-vf", "scale='min(720,iw)':-2",
             "-q:v", "2", out],
            capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception as e:
        logger.warning(f"auto thumb grab failed for {fpath}: {e}")
    return None


def _cleanup(p):
    try:
        os.remove(p)
    except OSError:
        pass
