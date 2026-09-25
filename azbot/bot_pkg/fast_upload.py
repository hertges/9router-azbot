"""Parallel MTProto uploader — the bot-only version of WZML-X's tg_transfer.

Why this exists (measured, not guessed): pyrogram's stock save_file opens
ONE media session and pushes 512KB parts through FOUR workers sharing a
queue of depth 1 — every byte of your upload serializes through a single
connection with barely any pipelining. WZML-X gets its speed from TWO
things: multiple media sessions AND a deep in-flight window (their "bulk"
profile: 4 clients, window = pipeline*2 ≈ 16 unacked parts). v18 copied
only the sessions half; each part still waited for its ACK, so throughput
barely moved. v19 implements BOTH: N sessions × global in-flight window,
parts fired without waiting for previous ACKs on the same connection.

Stays 100% bot-token-only (no premium/user session — standing principle).

Safety rails:
  • files <10MB and photos/webp go through stock pyrogram (callers enforce)
  • ANY failure falls back to the stock single-session path — worst case
    equals stock behavior, never worse
  • cancellation checked between every part
"""
import asyncio
import math
import os

from pyrogram import raw
from pyrogram.session import Session

from . import log

logger = log.get(__name__)

# Fail fast at startup if this pyrogram's raw layer ever renames a type —
# a typo here used to surface only at the FINAL SendMedia step, after all
# bytes were already uploaded (then the stock path re-sent the whole file).
for _n in ("DocumentAttributeFilename", "DocumentAttributeVideo",
           "DocumentAttributeAudio", "DocumentAttributeAnimated",
           "InputMediaUploadedDocument"):
    assert hasattr(raw.types, _n), f"raw.types.{_n} missing in this pyrogram build"

PART_SIZE = 512 * 1024          # Telegram's fixed part size for big files
BIG_FILE = 10 * 1024 * 1024     # >this => SaveBigFilePart territory


class UploadCancelled(Exception):
    pass


def partition_ranges(total_parts, n_sessions):
    """Contiguous part ranges per session, sized as evenly as possible.
    Pure function — unit-tested."""
    n = max(1, min(n_sessions, total_parts))
    q, r = divmod(total_parts, n)
    counts = [q + 1] * r + [q] * (n - r)
    ranges, start = [], 0
    for c in counts:
        if c:
            ranges.append((start, c))   # (first_part_index, part_count)
            start += c
    return ranges


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


async def _invoke_part(session, file_id, part_index, total_parts, chunk, sem,
                       on_progress):
    """Fires one SaveBigFilePart and releases its window slot afterwards."""
    try:
        await session.invoke(raw.functions.upload.SaveBigFilePart(
            file_id=file_id,
            file_part=part_index,
            file_total_parts=total_parts,
            bytes=chunk,
        ))
    finally:
        sem.release()
    if on_progress:
        on_progress(len(chunk))


async def _pump_range(path, first_part, part_count, file_id, total_parts,
                      session, pending, sem, on_progress, cancel_check):
    """Reads one contiguous range and keeps up to `sem` parts in flight
    WITHOUT waiting for each ACK (deep pipelining — the WZML-X trick).

    NOTE: no `async with session` here — pyrogram 2.0.106's Session has no
    async-context-manager protocol (that's fork-only), so `async with`
    raised TypeError and silently killed the whole striped path on every
    file. The session lifecycle is owned by upload_and_send: started ONCE
    via gather() before the pumps fire, stopped ONCE in its finally —
    each pump just uses its already-started session directly."""
    with open(path, "rb") as fh:
        fh.seek(first_part * PART_SIZE)
        for i in range(part_count):
            if cancel_check():
                raise UploadCancelled("cancelled by user")
            await sem.acquire()
            chunk = fh.read(PART_SIZE)
            if not chunk:
                sem.release()
                break
            # A non-final part MUST be exactly PART_SIZE or Telegram
            # rejects it with FILE_PART_INVALID. Fill short reads
            # (possible only if the writer is still appending to the
            # file — never on a complete one).
            while len(chunk) < PART_SIZE:
                more = fh.read(PART_SIZE - len(chunk))
                if not more:
                    break
                chunk += more
            task = asyncio.ensure_future(_invoke_part(
                session, file_id, first_part + i, total_parts,
                chunk, sem, on_progress))
            pending.add(task)
            task.add_done_callback(pending.discard)


async def upload_and_send(client, chat_id, path, *, kind="document",
                          caption="", duration=None, width=None, height=None,
                          performer=None, title=None, thumb_path=None,
                          reply_to=None, task_id=None, cancel_check=None,
                          progress_cb=None, n_sessions=None, window=None):
    """Striped, pipelined upload + send. Returns SendMedia's raw Updates.
    Raises on failure — callers fall back to stock pyrogram methods."""
    size = os.path.getsize(path)
    if size <= BIG_FILE:
        raise ValueError("too small for the striped path")

    # Throughput ≈ sessions × parts/sec; pipelining depth is the cheap
    # multiplier — in-flight buffers are only 512KB each, so 4×24 ≈ 12MB
    # peak RAM. (The OOM restarts came from Drive chunk buffers and
    # ungated ffmpeg — both gated now — not from these buffers.)
    n_sessions = n_sessions or _env_int("UPLOAD_SESSIONS", 4)
    window = window or _env_int("UPLOAD_WINDOW", 24)

    file_id = client.rnd_id()
    total_parts = math.ceil(size / PART_SIZE)

    # don't spin up more sessions than the file can feed (>=6MB each)
    effective = max(1, min(n_sessions, size // (6 * 1024 * 1024)))
    ranges = partition_ranges(total_parts, effective)

    sem = asyncio.Semaphore(max(1, window))
    pending = set()
    pumps = set()
    sent_bytes = {"n": 0}

    def on_progress(n):
        sent_bytes["n"] += n
        if progress_cb:
            try:
                progress_cb(sent_bytes["n"], size)
            except UploadCancelled:
                raise
            except Exception:
                pass

    cancel = cancel_check or (lambda: False)

    sessions = []
    try:
        for _ in range(effective):
            s = Session(
                client,
                await client.storage.dc_id(),
                await client.storage.auth_key(),
                await client.storage.test_mode(),
                is_media=True,
            )
            sessions.append(s)
        await asyncio.gather(*(s.start() for s in sessions))

        for session, (start, count) in zip(sessions, ranges):
            pump = asyncio.ensure_future(_pump_range(
                path, start, count, file_id, total_parts,
                session, pending, sem, on_progress, cancel))
            pumps.add(pump)
            pump.add_done_callback(pumps.discard)

        # wait for every fired part AND every range-pump. The pumps MUST be
        # awaited too: a pump that dies (disk read error, dropped session)
        # would otherwise fail silently, its remaining parts never uploaded,
        # and Telegram handed an INCOMPLETE file as if everything was fine.
        # NOTE: never rebind `pending`/`pumps` — the callbacks keep adding
        # to THESE set objects, so snapshot them per iteration instead.
        while pending or pumps:
            snap = list(pending) + list(pumps)
            done, _ = await asyncio.wait(snap, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc:
                    raise exc
        if cancel():
            raise UploadCancelled("cancelled by user")
        # Completeness gate: every byte must have actually been fired as a
        # part before we reference the file in SendMedia. A size mismatch
        # means some part silently failed or a short read slipped through —
        # raising here falls back to the stock path instead of delivering
        # a truncated file.
        if sent_bytes["n"] != size:
            raise RuntimeError(
                f"part bytes {sent_bytes['n']} != file size {size} — "
                f"upload incomplete, refusing to send")
    except UploadCancelled:
        raise
    except Exception as e:
        raise RuntimeError(f"striped upload failed: {e}") from e
    finally:
        for t in list(pending) + list(pumps):
            t.cancel()
        for s in sessions:
            try:
                await s.stop()
            except Exception:
                pass

    # ── reference the fully-uploaded file and send the message ──────────
    name = os.path.basename(path)

    def _guess_mime(p):
        import mimetypes
        table = {
            ".mp4": "video/mp4", ".mkv": "video/x-matroska",
            ".mov": "video/quicktime", ".webm": "video/webm",
            ".m4v": "video/x-m4v", ".avi": "video/x-msvideo",
            ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
            ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/opus",
        }
        ext = os.path.splitext(p)[1].lower()
        return table.get(ext) or mimetypes.guess_type(p)[0] \
            or "application/octet-stream"

    mime = _guess_mime(path)
    # NOTE: this fork's raw layer names it DocumentAttributeFilename
    # (lowercase "n"); DocumentAttributeFileName does not exist and blew
    # up at the FINAL SendMedia step — after every byte was already
    # uploaded — so the file was silently re-uploaded whole via the stock
    # path. Double upload = 2x the bytes, 2x the time.
    file_attr = raw.types.DocumentAttributeFilename(file_name=name)

    if kind == "video":
        attributes = [
            raw.types.DocumentAttributeVideo(
                duration=int(duration or 0),
                w=int(width or 0),
                h=int(height or 0),
                supports_streaming=True,
            ),
            file_attr,
        ]
    elif kind == "audio":
        attributes = [
            raw.types.DocumentAttributeAudio(
                duration=int(duration or 0),
                performer=performer,
                title=title or os.path.splitext(name)[0],
            ),
            file_attr,
        ]
    elif kind == "animation":
        attributes = [raw.types.DocumentAttributeAnimated(), file_attr]
    else:
        attributes = [file_attr]

    media = raw.types.InputMediaUploadedDocument(
        file=raw.types.InputFileBig(id=file_id, parts=total_parts, name=name),
        mime_type=mime,
        attributes=attributes,
        force_file=(kind == "document"),
    )

    if thumb_path and os.path.exists(thumb_path):
        try:
            media.thumb = await client.save_file(thumb_path)
        except Exception as e:
            logger.debug(f"thumb attach skipped: {e}")

    peer = await client.resolve_peer(chat_id)
    result = await client.invoke(raw.functions.messages.SendMedia(
        peer=peer,
        media=media,
        message=caption or "",
        random_id=client.rnd_id(),
        reply_to_msg_id=reply_to,
    ))
    logger.info(f"striped upload done: {path} ({size} bytes, "
                f"{effective} sessions, window={window}, {total_parts} parts)")
    return result


def run_parallel_send(client, *args, **kwargs):
    """Bridge for sync code (uploader/_send_one runs on worker threads):
    schedules upload_and_send on THIS client's running loop and waits.
    Mirrors how pyrogram itself bridges sync->async internally."""
    import threading

    box = {}

    def _wait():
        # BUG: the try/except used to wrap ONLY fut.result(), not the
        # run_coroutine_threadsafe(...) call that creates it. Any error
        # raised while SCHEDULING the coroutine (e.g. client.loop missing
        # or not yet running) happened outside that try block, so it just
        # crashed this background thread silently — Python prints the
        # traceback to stderr but nothing is stored in `box`. Back in the
        # caller, "error" was never in box, so run_parallel_send returned
        # None as if the striped upload had SUCCEEDED, even though nothing
        # was ever sent. Wrapping the whole body closes that hole: any
        # failure, at any stage, now correctly reaches the caller and
        # triggers the stock-path fallback.
        try:
            fut = asyncio.run_coroutine_threadsafe(
                upload_and_send(client, *args, **kwargs), client.loop)
            # 6h ceiling (was 2h): on a slow uplink a ~2GB transfer can
            # legitimately run longer than 2h — timing out here used to
            # abandon a healthy upload and re-send the WHOLE file through
            # the slower stock path from scratch.
            box["result"] = fut.result(timeout=21600)
        except Exception as e:
            box["error"] = e

    # threading.Thread has no `thread_name_prefix` kwarg (that's a
    # ThreadPoolExecutor-only parameter) — passing it here raised
    # TypeError on every call, so the striped/parallel upload path always
    # blew up and silently fell back to the slow single-session stock
    # path. Thread's equivalent kwarg is simply `name`.
    t = threading.Thread(target=_wait, daemon=True, name="azfastup")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")
