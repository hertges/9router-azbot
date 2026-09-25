import os, re, time, threading, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pyrogram.errors import FloodWait

from . import state, uploader, drive, log
from .utils import throttled_edit, fix_unknown_ext, cancel_kb, fmtsz, esc

logger = log.get(__name__)

SKIP_SUFFIXES = (".part", ".aria2", ".tmp", ".crdownload", ".ytdl", ".!qB", ".meta")

# Transient files the upload pipeline itself creates mid-job — a watcher
# scan running at the same time MUST never mistake them for finished
# files and re-upload them as garbage duplicates:
#   "movie.mkv.part001"     — byte-split part (iter_split_parts)
#   "movie.part001.mp4"     — ffmpeg-segment part (>2GB video split)
#   "<file>.srcdl.part" etc — temp thumbnails (named to end in .part)
SPLIT_PART_RE = re.compile(r"\.part\d{3}(\.[A-Za-z0-9]+)?$")

# Small videos/gifs/animated webp ride photo albums as mixed photo+video
# media groups: one SendMultiMedia per 10 entries replaces ten individual
# sends, each a FloodWait candidate on busy chats.
ALBUM_VIDEO_MAX_BYTES = 20 * 1024 * 1024

# Gallery rule (no conversions): a file that meets Telegram's gallery
# criteria (photo, or small video with real duration/dims) rides the
# album; anything else goes alone as a single send.

class LiveDispatcher:
    """Watches task_dir for finished files and uploads each one as soon as
    it stops changing, instead of waiting for the whole job to finish."""

    def __init__(self, client, chat_id, msg_id, task_dir, dest, reply_to=None, task_id=None,
                 max_workers=None, rename=None, folder=None,
                 caption_prefix=None, title=None, total_files=None, total_bytes=None,
                 quiet=False, on_sent=None, topic_root=None, watch=True):
        self.client = client
        self.chat_id = chat_id
        self.msg_id = msg_id
        self.task_dir = task_dir
        self.dest = dest
        # Topic anchoring WITHOUT a visible reply header: reply_to a forum
        # topic's ROOT message id renders the send as a plain post INSIDE
        # that topic (Telegram's documented behavior; this pyrogram fork
        # has no message_thread_id param). In PV there is no topic root —
        # topic_root is None there, so sends are plain unthreaded messages.
        # Passing None entirely (old behavior) drops forum sends into
        # General — which is exactly what the user saw after pass #4
        # removed the anchoring.
        self.reply_to = topic_root if topic_root else reply_to
        self.task_id = task_id
        self.rename = rename
        self.folder = folder
        # caption_prefix — prepended to every uploaded file's caption
        # (e.g. "📸 Posts" on IG archive runs).
        self.caption_prefix = caption_prefix
        # quiet — suppress ALL status-message progress edits from uploads
        # (IG runs their own live view on the job message; double-editing
        # the same message from dispatcher threads fought with it).
        self.quiet = quiet
        # on_sent — called (file path, size) after each successful upload,
        # before deletion (IG job uses it to bump its live counters).
        self.on_sent = on_sent
        # title/total_files/total_bytes — when known up front (Drive folder
        # overview), header_text() renders a live "where is it" line on
        # every progress edit: '📂 Vacation — 37/214 sent • 1.2 GB / 4.5 GB'
        self.title = title
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.seen = set()
        self.sent = 0
        self.bytes_sent = 0
        self.errors = []
        self.quality = None   # last video's "WxH • tier • duration" line, shown in the summary
        # BUG (race, group/topic-only): _watch()'s background scan and
        # finalize()'s forced last-chance scan both read/write `self.seen`
        # with a check-then-act (`if fp in self.seen: ... self.seen.add(fp)`)
        # and neither excluded the other. _watch()'s loop only re-checks
        # `self._stop` BETWEEN scans, never during one — so if a scan is
        # still walking a big directory when finalize() sets the stop flag,
        # `self._watcher.join(timeout=5)` can return (or time out) while
        # that scan is still mid-flight. finalize() then runs its OWN scan
        # concurrently, and both can see the same not-yet-`seen` file at the
        # same instant and both submit it for upload. One upload deletes the
        # file after sending it; the other, racing on the same path, finds
        # it gone — exactly the "[Errno 2] No such file or directory" /
        # "does not represent an existing local file" errors this caused.
        # Only showed up under load (large archives with many files landing
        # quickly, e.g. a busy group's /ig run) — small/fast jobs rarely hit
        # the window. Fixed by serializing every _scan() call on a lock so
        # the watcher's in-flight scan and finalize()'s forced scan can
        # never run at the same time.
        self._scan_lock = threading.Lock()
        # BUG (dead config): config.UPLOAD_WORKERS existed but was never
        # actually wired to anything — this always used its own hardcoded
        # default of 2, disconnected from that setting entirely. Each
        # concurrent upload worker is another file's worth of MTProto
        # transfer buffers, plus its own ffmpeg/ffprobe subprocess calls for
        # thumbnails/metadata, all running at once — on a memory-constrained
        # host (small container plans, e.g. Sevalla's smaller tiers) that
        # concurrency is a direct multiplier on PEAK memory, which is what
        # actually triggers an OOM kill (not the average usage a dashboard
        # graph shows, which can easily miss a spike that lasts under a
        # second). Now actually reads config.UPLOAD_WORKERS, tunable via the
        # UPLOAD_WORKERS env var without a code change.
        if max_workers is None:
            from . import config
            max_workers = config.UPLOAD_WORKERS
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures = []
        self._stop = threading.Event()
        self._album = []                  # buffered (fpath, caption, size) photos
        self._album_lock = threading.Lock()
        # unified progress view state (see _render): (name, done, total)
        self._up_state = None
        self._dl_state = None
        self._view_lock = threading.Lock()
        # watch=False → NO filesystem watcher: the caller submits files
        # explicitly (yt-dlp playlists — the watcher would sweep up yt-dlp's
        # intermediate format files (.f137.mp4/.f140.m4a) and .meta
        # sidecars mid-run, double-sending and phantom-failing). finalize()
        # still flushes anything explicitly submitted.
        self._watcher = threading.Thread(target=self._watch, daemon=True) if watch else None
        if self._watcher:
            self._watcher.start()

    def _watch(self):
        while not self._stop.is_set():
            if self.task_id and state.is_cancelled(self.task_id):
                break
            self._scan()
            self._stop.wait(2)

    def _scan(self, force=False):
        with self._scan_lock:
            now = time.time()
            if not os.path.isdir(self.task_dir):
                return
            for root, _, files in os.walk(self.task_dir):
                for f in files:
                    if f.endswith(SKIP_SUFFIXES) or SPLIT_PART_RE.search(f):
                        # .part/.aria2/etc = still downloading; SPLIT_PART_RE =
                        # transient upload-pipeline files (split parts, temp
                        # thumbs) that exist while uploads are already running.
                        # Drive downloads are delivered via "<name>.part" ->
                        # atomic rename (see drive.download_file_content), so
                        # anything visible under its final name is COMPLETE.
                        continue
                    fp = os.path.join(root, f)
                    if fp in self.seen:
                        continue
                    try:
                        st = os.stat(fp)
                    except FileNotFoundError:
                        continue
                    # The 2s-untouched check exists to avoid grabbing a file
                    # mid-write during periodic scans while the subprocess is
                    # still running. finalize()'s last-chance scan runs *after*
                    # the subprocess has already exited, so every file left on
                    # disk is complete by definition — skipping the age check
                    # there matters a lot for fast/small downloads (a single
                    # photo, say) that finish in well under 2 seconds and would
                    # otherwise be missed entirely, reported as "nothing
                    # downloaded" despite the file sitting right there.
                    if st.st_size > 0 and (force or now - st.st_mtime > 2):
                        self.seen.add(fp)
                        self.submit(fp)

    def submit(self, fpath):
        self._futures.append(self._executor.submit(self._upload_one, fpath))

    def header_text(self):
        """Live position line for multi-file jobs with known totals:
        '📂 Vacation — 37/214 sent • 1.2 GB / 4.5 GB'. Empty when totals
        weren't provided (single files, IG, torrent jobs) — callers just
        omit it, so existing progress texts are unchanged for them."""
        if not self.total_files:
            return ""
        line = (f"📂 {esc(self.title or 'folder')} — "
                f"<b>{self.sent}</b>/{self.total_files} sent")
        if self.total_bytes:
            line += f" • {fmtsz(self.bytes_sent)} / {fmtsz(self.total_bytes)}"
        return line

    def _render(self, speed=""):
        """ONE coherent view of the overlap: folder position + upload line +
        download line. Both progress feeders (upload callbacks AND the Drive
        download callback) render through here — so the message never
        flickers between separate "downloading" and "uploading" texts."""
        lines = []
        hdr = self.header_text()
        if hdr:
            lines.append(hdr)
        if self._up_state:
            name, done, total = self._up_state
            bar = f"{fmtsz(done)} / {fmtsz(total)}" if total else fmtsz(done)
            lines.append(f"⬆️ <code>{esc(name)}</code> — {bar}" + (f" ⚡ {speed}" if speed else ""))
        if self._dl_state:
            name, done, total = self._dl_state
            bar = f"{fmtsz(done)} / {fmtsz(total)}" if total else fmtsz(done)
            lines.append(f"⬇️ <code>{esc(name)}</code> — {bar}")
        return "\n".join(lines) or "☁️ Working…"

    def _set_upload(self, name, done, total, speed=""):
        """Stores upload progress AND returns the rendered text (the upload
        callback composes its own message from the return value — it owns
        the edit cadence)."""
        with self._view_lock:
            self._up_state = (name, int(done), int(total)) if name else None
            return self._render(speed)

    def _set_download(self, name, done, total, force=False):
        with self._view_lock:
            self._dl_state = (name, int(done), int(total)) if name else None
            text = self._render()
        throttled_edit(self.client, self.chat_id, self.msg_id, text,
                       markup=cancel_kb(self.task_id) if self.task_id else None,
                       force=force)

    def _refresh_view(self, force=False):
        """Redraw the current view under the lock — used by the album
        buffer path and finalize() so the header/sent counters stay live."""
        with self._view_lock:
            text = self._render()
        throttled_edit(self.client, self.chat_id, self.msg_id, text,
                       markup=cancel_kb(self.task_id) if self.task_id else None,
                       force=force)

    def _upload_one(self, fpath):
        keep = False   # album-buffered files are deleted by the flush, not here
        try:
            if self.task_id and state.is_cancelled(self.task_id):
                return
            fixed = fix_unknown_ext(fpath)
            if fixed != fpath:
                # The rename above happens while the watcher thread may scan
                # again — without marking the new name as seen, the next scan
                # treats it as a brand-new file and submits the SAME content
                # twice (the second run then fails with FileNotFoundError the
                # moment the first upload deletes the file).
                with self._scan_lock:
                    self.seen.add(fixed)
            fpath = fixed
            # Static .webp: Telegram server-converts small webp documents
            # into STICKERS, and webp can't ride the 10-per-send photo album
            # batching either — together that's why a webp-heavy folder
            # leeches one slow individual send at a time and arrives as
            # sticker cards. Convert static webp to jpg (ffmpeg is a hard
            # dep) so it flows through the normal photo path.
            # Animated webp → .gif (user request): keeps the animation in
            # a universally viewable file, sent as a looping animation.
            # GIFs can't ride photo+video albums either, so it still goes
            # alone — but as an animation, never a sticker.
            if os.path.splitext(fpath)[1].lower() == ".webp":
                if uploader._is_animated_webp(fpath):
                    gif = os.path.splitext(fpath)[0] + ".gif"
                    if os.path.exists(gif):
                        # same-stem sibling (a.gif AND a.webp): never
                        # clobber it with -y — convert beside it.
                        gif = fpath + ".conv.gif"
                    try:
                        r = subprocess.run(
                            ["ffmpeg", "-y", "-loglevel", "error", "-i", fpath, gif],
                            capture_output=True, timeout=120)
                        if r.returncode == 0 and os.path.exists(gif) \
                                and os.path.getsize(gif) > 0:
                            with self._scan_lock:
                                self.seen.add(gif)   # watcher must not re-submit it
                            try:
                                os.remove(fpath)
                            except OSError:
                                pass
                            fpath = gif
                        else:
                            logger.warning(f"webp→gif conversion failed for {fpath} "
                                           f"({(r.stderr or b'').decode(errors='ignore')[-120:]})")
                    except Exception as e:
                        logger.warning(f"webp→gif conversion failed for {fpath}: {e}")
                else:
                    jpg = os.path.splitext(fpath)[0] + ".jpg"
                    if os.path.exists(jpg):
                        # same-stem sibling (a.jpg AND a.webp): never
                        # clobber it with -y — convert beside it.
                        jpg = fpath + ".conv.jpg"
                    try:
                        r = subprocess.run(
                            ["ffmpeg", "-y", "-loglevel", "error", "-i", fpath, jpg],
                            capture_output=True, timeout=60)
                        if r.returncode == 0 and os.path.exists(jpg) \
                                and os.path.getsize(jpg) > 0:
                            with self._scan_lock:
                                self.seen.add(jpg)   # watcher must not re-submit it
                            try:
                                os.remove(fpath)
                            except OSError:
                                pass
                            fpath = jpg
                        else:
                            logger.warning(f"webp→jpg conversion failed for {fpath} "
                                           f"({(r.stderr or b'').decode(errors='ignore')[-120:]})")
                    except Exception as e:
                        logger.warning(f"webp→jpg conversion failed for {fpath}: {e}")
            try:
                fsize = os.path.getsize(fpath)
            except OSError:
                fsize = 0
            if self.dest in ("telegram", "both"):
                # Album path: photos AND small videos (<20MB) go out as
                # Telegram media GROUPS (up to 10 per send — mixed
                # photo+video groups are allowed). One SendMultiMedia per 10
                # files instead of ten individual sends is the difference
                # between a folder leeching in minutes and an hour of
                # 37-second FloodWaits. Anything outside the gallery
                # criteria (gifs, animated webp, HEIC/BMP, big videos,
                # audio, documents) goes alone as a single send
                # (streaming, thumbnails, quality captions).
                # With "send as document"
                # set, everything keeps the individual path too.
                ext_l = os.path.splitext(fpath)[1].lower()
                small_video = False
                vmeta = None
                if ext_l in uploader.VIDEO_EXT and fsize <= ALBUM_VIDEO_MAX_BYTES:
                    # Album video entries NEED real duration/dimensions —
                    # zero-attribute videos in a media group come back as
                    # MEDIA_EMPTY (the "some images sent as singles" bug).
                    vmeta = uploader.probe_meta_if_media(fpath)
                    if vmeta.get("duration"):
                        small_video = True
                if ((ext_l in uploader.PHOTO_EXT or small_video)
                        and not state.get_as_document(self.chat_id)
                        and state.get_media_group(self.chat_id)
                        and self.dest != "drive"):
                    kind = "photo" if ext_l in uploader.PHOTO_EXT else "video"
                    cap = (f"{self.caption_prefix} · {uploader._clean_caption(fpath)}"
                           if self.caption_prefix else None)
                    batch = None
                    with self._album_lock:
                        self._album.append((fpath, cap, fsize, kind, vmeta))
                        if len(self._album) >= 10:
                            batch = self._album[:10]
                            del self._album[:10]
                    keep = True
                    # count+announce as soon as the entry is buffered — the
                    # header counts "sent" from these fields, and a small
                    # album only actually FLUSHES at finalize(): the old
                    # counting-at-flush showed "0/2 sent • 0 B" for the
                    # whole job even though everything was about to go out.
                    self.sent += 1
                    self.bytes_sent += fsize
                    if batch:
                        self._send_album(batch)
                    else:
                        self._refresh_view()
                    return
                # probe once — feeds BOTH the upload attributes/caption and
                # the completion summary's quality line ("no quality shown"
                # fix for IG archives & multi-file jobs). Skipped entirely
                # for non-media files (one less subprocess per photo/doc).
                _meta = uploader.probe_meta_if_media(fpath)
                # caption_prefix: e.g. IG archive runs tag every file with
                # its type ("📸 Posts", "🎬 Reels", …)
                cap = (f"{self.caption_prefix} · {uploader._clean_caption(fpath)}"
                       if self.caption_prefix else None)
                # quiet mode: no per-file progress bars at all — the owning
                # job renders ONE aggregate live view (on_sent fires per file)
                pmsg = None if self.quiet else self.msg_id

                def _label(cur, tot):
                    """UNIFIED view: this file's upload progress (with its
                    own bar from the caller) + any concurrent download's
                    line — one coherent message instead of two fighting."""
                    return self._set_upload(os.path.basename(fpath), cur, tot)

                uploader.upload_to_telegram(self.client, self.chat_id, fpath, pmsg,
                                             reply_to=self.reply_to, task_id=self.task_id,
                                             video_meta=_meta, caption=cap,
                                             progress_label=None if self.quiet else _label)
                self._set_upload(None, 0, 0)   # clear upload line on completion
                if os.path.splitext(fpath)[1].lower() in uploader.VIDEO_EXT and _meta:
                    self.quality = uploader._quality_line(_meta)
            if self.dest in ("drive", "both") and drive.enabled():
                # Drive uploads show their own progress on the task message
                from .utils import progress_line
                import time as _t
                _t0 = _t.time()
                last = [0.0]

                def _dprog(done, total, _n=[0]):
                    if self.quiet:
                        return
                    now = _t.time()
                    if now - last[0] < 1.5 and done != total:
                        return
                    last[0] = now
                    hdr = self.header_text()
                    throttled_edit(self.client, self.chat_id, self.msg_id,
                                   ((hdr + "\n") if hdr else "")
                                   + f"☁️ <b>Uploading to Drive</b>\n"
                                   f"{progress_line('☁️', done, total, elapsed=now - _t0)}",
                                   markup=cancel_kb(self.task_id) if self.task_id else None,
                                   force=(done == total))
                link = drive.upload_file(fpath, task_id=self.task_id,
                                         rename=self.rename, folder=self.folder,
                                         chat_id=self.chat_id, progress_cb=_dprog)
                logger.info(f"uploaded to Drive: {fpath} -> {link}")
            self.sent += 1
            self.bytes_sent += fsize
            if self.on_sent:
                try:
                    self.on_sent(fpath, fsize)
                except Exception:
                    pass
        except state.CancelledError:
            logger.info(f"[{self.task_id}] dispatch upload cancelled")
        except Exception as e:
            logger.warning(f"dispatch upload failed for {fpath}: {e}")
            self.errors.append(f"{os.path.basename(fpath)}: {e}")
        finally:
            if not keep:
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    def _album_sent(self, items):
        """Fires on_sent per delivered album/single item — shared by the
        group path and the single-fallback path below."""
        if not self.on_sent:
            return
        for p, fs in items:
            try:
                self.on_sent(p, fs)
            except Exception:
                pass

    def _send_media_items(self, items):
        """Sends 2-10 buffered entries as ONE Telegram media group (photos
        and small videos mixed). Raises on failure — the caller decides
        whether to split or single-fallback. A FloodWait is waited out and
        retried once HERE so a rate limit never decomposes a group into
        individual sends (that multiplies the flood and is why images
        used to arrive individually with name captions)."""
        import pyrogram
        media = []
        for p, cap, _fs, kind, meta in items:
            if kind == "video":
                meta = meta or {}
                media.append(pyrogram.types.InputMediaVideo(
                    media=p, caption=cap,
                    parse_mode=pyrogram.enums.ParseMode.DISABLED,
                    duration=int(meta.get("duration") or 0),
                    width=int(meta.get("width") or 0),
                    height=int(meta.get("height") or 0),
                    supports_streaming=True))
            else:
                media.append(pyrogram.types.InputMediaPhoto(
                    media=p, caption=cap, parse_mode=pyrogram.enums.ParseMode.DISABLED))
        try:
            self.client.send_media_group(self.chat_id, media,
                                         reply_to_message_id=self.reply_to)
        except FloodWait as e:
            logger.warning(f"album FloodWait {e.value}s — retrying group once")
            time.sleep(e.value + 1)
            self.client.send_media_group(self.chat_id, media,
                                         reply_to_message_id=self.reply_to)

    def _send_album_single(self, p, cap, fs):
        """One buffered entry delivered as a single (trailing leftover, or
        an individual from a group that refused to send together). Errors
        are recorded, the file is always removed — the delete-after-send
        rule holds per item."""
        try:
            uploader.upload_to_telegram(self.client, self.chat_id, p, None,
                                        reply_to=self.reply_to, task_id=self.task_id,
                                        caption=cap)
            self._album_sent([(p, fs)])
        except Exception as e:
            self.errors.append(f"{os.path.basename(p)}: {e}")
            # Un-count: entries are tallied at buffer time, so a failure
            # must give its count back or summaries over-report.
            self.sent -= 1
            self.bytes_sent -= fs
        finally:
            try:
                os.remove(p)
            except OSError:
                pass

    def _send_album(self, batch):
        """One send_media_group call for up to 10 entries — photos and small
        videos mixed (Telegram groups accept both). WZML-X MEDIA_GROUP
        parity: groups stay groups. If a whole group fails (one bad file
        kills all ten), the batch is SPLIT IN HALF and each half is retried
        as its own group — only genuinely unsendable individuals end up as
        singles, so one corrupt file can't flatten the other nine into
        singles anymore. A trailing single entry (leftover from finalize)
        goes through the normal uploader — send_media_group requires 2-10
        items."""
        if len(batch) == 1:
            p, cap, fs, _kind, _meta = batch[0]
            return self._send_album_single(p, cap, fs)
        pending = [list(batch)]
        while pending:
            chunk = pending.pop(0)
            if len(chunk) == 1:
                p, cap, fs, _kind, _meta = chunk[0]
                self._send_album_single(p, cap, fs)
                continue
            try:
                self._send_media_items(chunk)
                self._album_sent([(p, fs) for p, _c, fs, _k, _m in chunk])
                for p, _c, _fs, _k, _m in chunk:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            except Exception as e:
                if len(chunk) == 2:
                    for p, cap, fs, _k, _m in chunk:
                        self._send_album_single(p, cap, fs)
                else:
                    mid = len(chunk) // 2
                    pending.append(chunk[:mid])
                    pending.append(chunk[mid:])
                    logger.warning(f"album send failed ({len(chunk)} items: {e}) — retrying as halves")
        for p, _c, _fs, _k, _m in batch:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    def flush_album(self):
        """Called by finalize(): uploads whatever entries are still buffered
        (a trailing group smaller than 10 would otherwise never send)."""
        with self._album_lock:
            batch = self._album[:]
            self._album.clear()
        if batch:
            self._send_album(batch)

    def stop(self):
        """Signal the watcher to stop without waiting for outstanding
        uploads — used on cancellation so we don't leak a background thread."""
        self._stop.set()

    def finalize(self, timeout=None):
        self._stop.set()
        if self._watcher:
            self._watcher.join(timeout=5)
            self._scan(force=True)  # subprocess has already exited — every remaining file is complete
        # Buffered photos go out BEFORE the wait loop: the loop itself only
        # edits on future completions (and even then throttled by 5s), so a
        # tail photo group used to sit unannounced through the whole wait —
        # the bubble froze on the last "batch k/N — 0 B" line while the
        # group send (and its possible FloodWait retries) ran silently.
        if self._album and not self.quiet:
            with self._album_lock:
                n = len(self._album)
            throttled_edit(self.client, self.chat_id, self.msg_id,
                           f"📤 Sending {n} buffered photo(s)…", force=True)
        self.flush_album()
        # NO hard timeout on the wait: as_completed(timeout=…) raises
        # 'N (of M) futures unfinished' and the caller's cleanup then
        # DELETED the task dir — abandoning a 2652-item archive after the
        # 30-min default (serial Drive uploads legitimately take hours).
        # Instead: wait for every future, keep the user informed with a
        # live '⬆️ Uploading k/N' line, and treat cancellation as the only
        # early exit (workers no-op quickly on cancel, so the queue drains).
        total = len(self._futures)
        if total == 0:
            self._executor.shutdown(wait=True)
            self.flush_album()
            return self.sent, self.errors
        done = 0
        last = 0.0
        import time as _t
        for f in as_completed(list(self._futures)):
            done += 1
            now = _t.time()
            if not self.quiet and (now - last > 5 or done == total):
                last = now
                with self._view_lock:
                    self._up_state = (f"batch {done}/{total}", self.bytes_sent,
                                      self.total_bytes or 0)
                    text = self._render()
                throttled_edit(self.client, self.chat_id, self.msg_id, text,
                               markup=cancel_kb(self.task_id) if self.task_id else None,
                               force=(done == total))
        self._executor.shutdown(wait=True)
        with self._view_lock:
            self._up_state = None
            text = self._render()
        if not self.quiet:
            throttled_edit(self.client, self.chat_id, self.msg_id, text, force=True)
        # any photos still buffered (<10, no further scans) go out now
        self.flush_album()
        return self.sent, self.errors
