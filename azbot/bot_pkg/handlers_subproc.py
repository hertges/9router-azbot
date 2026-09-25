import os, re, time, shutil, subprocess, threading

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import config, state, drive, uploader, log
from .core import app
from .utils import bar, throttled_edit, new_task_id, zip_dir, fmtsz, autoclean_if_enabled, guarded, esc, split_dash_args, file_anchor
from .dispatcher import LiveDispatcher
from .handlers_core import Authorized, cancel_kb

logger = log.get(__name__)

ARIA2_PROGRESS_RE = re.compile(r"\[#\w+\s+([\d.]+\w+)/([\d.]+\w+)\((\d+)%\).*?DL:([\d.]+\w+)")
ETA_RE = re.compile(r"ETA:(\S+)")


def _build_args(cmd, url, task_dir, cookie_file, extra_args=None):
    if cmd == "torrent":
        return ["aria2c", "--seed-time=0", "--file-allocation=none",
                "--summary-interval=1", "--max-connection-per-server=8",
                "-d", task_dir, url]
    if cmd == "gallery":
        args = ["gallery-dl", "--dest", task_dir]
        if cookie_file:
            args += ["--cookies", cookie_file]
        # per-URL download archive (same ledger mechanism the IG archiver
        # uses): gallery-dl skips items it already fetched, so re-running
        # the same gallery URL only pulls NEW content. The ledger lives in
        # data/indexes/ (g_<md5-of-url>.txt) and mirrors to the Drive
        # datastore after the job — survives redeploys like the IG ones.
        args.append(url)
        if extra_args:
            args += list(extra_args)
        return args
    if cmd == "clone":
        args = ["wget", "--mirror", "--convert-links", "--adjust-extension",
                "--page-requisites", "--no-parent", "-e", "robots=off",
                "-P", task_dir, url]
        if extra_args:
            args += list(extra_args)
        return args
    return []


def _parse_aria2_error(lines):
    for line in lines:
        m = re.search(r"status=(\d{3})", line)
        if m:
            return f"HTTP {m.group(1)}"
        if "[ERROR]" in line or "Exception:" in line:
            return line.strip()[:180]
    return None


@app.on_message(filters.command(["gallery", "g", "galleryl", "gm", "gallerym",
                                  "galleryz", "galleryzm", "clone", "clonem"]) & Authorized)
@guarded
def cmd_subprocess(client, message):
    """gallery-dl front-ends:
        /gallery /g /galleryl  — files to this chat (default)
        /gm /gallerym          — mirror files to Drive (#folder works)
        /galleryz              — one zip archive to this chat
        /galleryzm             — one zip archive to Drive (#folder works)
        /clone /clonem         — wget mirror to chat / Drive
    Raw binary flags after a '--' separator are passed verbatim:
        /gallery <url> -- --range 1-5 --verbose"""
    from .utils import parse_payload
    cmd = message.command[0]
    raw = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    raw, extra_args = split_dash_args(raw)
    links, rename, folder = parse_payload(raw)
    if not links:
        return message.reply_text(
            f"⚠️ Usage: <code>/{esc(cmd)} &lt;url&gt;</code> [links…] [#folder]"
            f" <code>--</code> <code>[raw flags]</code>")

    if cmd in ("gallery", "g", "galleryl"):
        tool, dest, zip_output = "gallery", "telegram", False
    elif cmd in ("gm", "gallerym"):
        tool, dest, zip_output = "gallery", "drive", False
        if not drive.enabled():
            return message.reply_text("☁️ Drive isn't configured — use <code>/gallery</code> instead.")
    elif cmd == "galleryz":
        tool, dest, zip_output = "gallery", "telegram", True
    elif cmd == "galleryzm":
        tool, dest, zip_output = "gallery", "drive", True
        if not drive.enabled():
            return message.reply_text("☁️ Drive isn't configured — use <code>/galleryz</code> instead.")
    elif cmd == "clonem":
        tool, dest, zip_output = "clone", "drive", False
        if not drive.enabled():
            return message.reply_text("☁️ Drive isn't configured — use <code>/clone</code> instead.")
    else:  # clone
        tool, dest, zip_output = "clone", "telegram", False

    for url in links:
        queue_subprocess_job(client, message, tool, url, dest=dest,
                             zip_output=zip_output, extra_args=extra_args, folder=folder)


def queue_subprocess_job(client, message, cmd, url, dest="telegram", zip_output=False,
                         extra_args=None, folder=None, anchor=None):
    if cmd == "gallery" and dest == "drive" and not drive.enabled():
        return message.reply_text("❌ Drive isn't configured on this bot.")
    if not state.ensure_free(config.MIN_FREE_MB):
        return message.reply_text("❌ Disk full, try again later.")
    task_id = new_task_id()
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    tag = f"📦→{config.DEST_LABEL[dest]}" if zip_output else f"→{config.DEST_LABEL[dest]}"
    status = message.reply_text(
        f"⚡ Queued <code>/{esc(cmd)}</code> {tag} — <code>{esc(task_id)}</code>",
        reply_markup=cancel_kb(task_id),
        disable_web_page_preview=True,
    )
    state.register_job(task_id, message.chat.id, status.id, cmd, task_dir)
    state.register_url(url, task_id)
    state.task_queue.put((run_subprocess_job,
                           (client, message.chat.id, status.id, cmd, url, task_id, message.id,
                            dest, zip_output, extra_args, folder,
                            anchor if anchor is not None else file_anchor(message))))
    return task_id   # callers (e.g. the leech-job IG handoff) show which job took over


def run_subprocess_job(client, chat_id, msg_id, cmd, url, task_id, reply_to,
                       dest="telegram", zip_output=False, extra_args=None, folder=None,
                       file_anchor=None):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    cookie_file = state.active_cookie_file(chat_id)
    # per-URL download-archive ledger (IG-index mechanism, generic): mounted
    # by _build_args for gallery jobs; mirrored to Drive after the run so it
    # survives redeploys — re-running the same URL fetches only NEW items.
    import hashlib as _hl
    archive_db = os.path.join(config.INDEX_DIR,
                              f"g_{_hl.md5(url.encode()).hexdigest()}.txt")
    # Zipped output waits for the whole job then uploads one file, so it
    # doesn't make sense to also live-stream individual files as they land.
    disp = None if zip_output else LiveDispatcher(
        client, chat_id, msg_id, task_dir, dest, reply_to=file_anchor,
        task_id=task_id, folder=folder)
    full_log = []
    try:
        args = _build_args(cmd, url, task_dir, cookie_file, extra_args)
        if cmd == "gallery":
            args += ["--download-archive", archive_db]
        if not shutil.which(args[0]):
            return throttled_edit(client, chat_id, msg_id,
                                   f"❌ <code>{esc(args[0])}</code> isn't installed on this host.", force=True)

        logger.info(f"[{task_id}] /{cmd} starting — {url} (dest={dest}, zip={zip_output})")
        throttled_edit(client, chat_id, msg_id, f"⬇️ <code>/{esc(cmd)}</code>…\n<code>{esc(url[:60])}</code>",
                       markup=cancel_kb(task_id), force=True)
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 universal_newlines=True, bufsize=1)
        state.attach_proc(task_id, proc)

        last_activity = [time.time()]
        stalled = threading.Event()

        def _watchdog():
            while proc.poll() is None:
                if state.is_cancelled(task_id):
                    return
                if time.time() - last_activity[0] > config.STALL_TIMEOUT_S:
                    stalled.set()
                    logger.warning(f"[{task_id}] /{cmd} stalled — no output for {config.STALL_TIMEOUT_S}s, stopping")
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    return
                time.sleep(5)

        threading.Thread(target=_watchdog, daemon=True).start()

        last_edit = 0.0
        last_walk = [0.0]
        try:
            for line in iter(proc.stdout.readline, ""):
                last_activity[0] = time.time()
                if state.is_cancelled(task_id):
                    break
                full_log.append(line.rstrip())
                now = time.time()
                if now - last_edit < 3:
                    continue
                last_edit = now
                m = ARIA2_PROGRESS_RE.search(line)
                if m and cmd == "torrent":
                    done, total, pct, spd = m.groups()
                    eta_m = ETA_RE.search(line)
                    eta = eta_m.group(1) if eta_m else "?"
                    throttled_edit(client, chat_id, msg_id,
                                    f"⬇️ Torrent\n{bar(float(pct), 100)}\n<code>{esc(done)}/{esc(total)}</code> • {esc(spd)}/s • ETA {esc(eta)}",
                                    markup=cancel_kb(task_id))
                elif cmd == "gallery":
                    # gallery-dl emits NO percentage (total unknown), so a
                    # fake bar would lie — show a live count+size tracker
                    # straight from the download dir instead (cheap walk).
                    if time.time() - last_walk[0] < 3:
                        continue
                    last_walk[0] = time.time()
                    # DISK BACKPRESSURE: when Drive-upload (1 concurrent)
                    # lags the downloader, the backlog eats disk. If free
                    # space is critical, PAUSE progress polling (the
                    # process keeps running; this loop just stops
                    # pretending everything is fine) and keep warning until
                    # the upload side drains enough of the backlog. Killing
                    # the job would waste hours of fetch work; waiting is
                    # correct because uploads delete files as they go.
                    waited = 0
                    while (state.disk_free_mb() < config.MIN_FREE_MB
                           and not state.is_cancelled(task_id)):
                        waited += 5
                        # keep the stall watchdog fed while we pause —
                        # gallery-dl blocking on a full pipe is intended
                        # backpressure, not a stall
                        last_activity[0] = time.time()
                        if waited % 30 == 5:
                            throttled_edit(client, chat_id, msg_id,
                                           f"⏸️ Disk almost full ({state.disk_free_mb()} MB free) — "
                                           f"waiting for uploads to drain the backlog…",
                                           markup=cancel_kb(task_id), force=True)
                        time.sleep(5)
                    n_files, n_bytes = 0, 0
                    for r, _, fs in os.walk(task_dir):
                        for f in fs:
                            try:
                                n_bytes += os.path.getsize(os.path.join(r, f))
                                n_files += 1
                            except OSError:
                                pass
                    last = line.strip()[:120]
                    throttled_edit(client, chat_id, msg_id,
                                   f"🖼️ gallery-dl — 📥 <b>{n_files}</b> file(s) • {fmtsz(n_bytes)}"
                                   + (f"\n<code>{esc(last)}</code>" if last else ""),
                                   markup=cancel_kb(task_id))
                elif cmd == "clone":
                    throttled_edit(client, chat_id, msg_id, f"🌐 Mirroring…\n<code>{esc(line.strip()[:120])}</code>",
                                    markup=cancel_kb(task_id))
        except (AttributeError, ValueError):
            pass  # stdout closed by /cancel teardown — expected

        proc.wait()
        logger.info(f"[{task_id}] /{cmd} process exited with code {proc.returncode}")

        if stalled.is_set():
            if disp:
                disp.stop()
            mins = config.STALL_TIMEOUT_S // 60
            return throttled_edit(
                client, chat_id, msg_id,
                f"⏱️ Stopped — no progress for {mins} minutes. The site/tracker may be "
                f"unresponsive, blocking automated access, or (for torrents) has no peers.",
                force=True,
            )

        if state.is_cancelled(task_id):
            if disp:
                disp.stop()
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        if zip_output:
            has_files = any(os.path.getsize(os.path.join(r, f)) > 0
                             for r, _, fs in os.walk(task_dir) for f in fs)
            if not has_files:
                log_tail = "\n".join(full_log[-15:])
                err = _parse_aria2_error(full_log)
                return throttled_edit(
                    client, chat_id, msg_id,
                    "❌ Nothing downloaded.\n" + (f"<code>{esc(err)}</code>\n" if err else "") +
                    f"<pre>{esc(log_tail[-1500:])}</pre>",
                    force=True,
                )
            throttled_edit(client, chat_id, msg_id, "📦 Zipping…", force=True)
            zpath = os.path.join(config.DOWNLOAD_DIR, f"{task_id}.zip")
            try:
                zip_dir(task_dir, zpath)
                zip_size = os.path.getsize(zpath)
                throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (`{fmtsz(zip_size)}`)", markup=cancel_kb(task_id), force=True)
                done_kb = None
                link = None
                if dest == "telegram":
                    uploader.upload_to_telegram(client, chat_id, zpath, msg_id,
                                                reply_to=file_anchor, task_id=task_id)
                elif dest == "drive" and drive.enabled():
                    info = drive.upload_file_full(zpath, task_id=task_id,
                                                  folder=folder, chat_id=chat_id)
                    if info:
                        link = info["link"]
                        done_kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{info['id']}"),
                            InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{info['id']}"),
                        ]])
                else:
                    # Defensive: dest=="drive" should already be blocked upstream
                    # (cmd_mirror_leech checks drive.enabled() before queueing)
                    # but if that guard is ever bypassed, don't silently delete
                    # the finished archive and report "success" with nothing
                    # actually uploaded anywhere.
                    return throttled_edit(
                        client, chat_id, msg_id,
                        "❌ Drive isn't configured on this bot — can't deliver the archive.",
                        force=True,
                    )
                summary = (f"✅ <b>{esc(os.path.basename(zpath))}</b>\n"
                           f"📦 <code>{fmtsz(zip_size)}</code> → {config.DEST_LABEL[dest]}")
                if link:
                    summary += f"\n{link}"
                throttled_edit(client, chat_id, msg_id, summary, markup=done_kb, force=True)
                autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)
                return
            finally:
                # zpath lives OUTSIDE task_dir (so zip_dir can't zip itself);
                # without this finally, any upload error leaked the archive
                # in downloads/ forever (purge_stale only scans dirs).
                try:
                    os.remove(zpath)
                except OSError:
                    pass

        throttled_edit(client, chat_id, msg_id, "☁️ Uploading results…", markup=cancel_kb(task_id), force=True)
        sent, errors = disp.finalize(timeout=1800)

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        if sent == 0:
            err = _parse_aria2_error(full_log)
            log_tail = "\n".join(full_log[-15:])
            return throttled_edit(
                client, chat_id, msg_id,
                "❌ Nothing downloaded.\n" + (f"<code>{esc(err)}</code>\n" if err else "") +
                f"<pre>{esc(log_tail[-1500:])}</pre>",
                force=True,
            )

        summary = f"✅ {sent} file(s) → {config.DEST_LABEL[dest]}"
        if errors:
            summary += f"\n⚠️ {len(errors)} failed."
        throttled_edit(client, chat_id, msg_id, summary, force=True)
        autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)
        if cmd == "gallery" and os.path.exists(archive_db):
            try:
                from . import datastore
                datastore.push_datafile(archive_db, f"indexes/{os.path.basename(archive_db)}")
            except Exception as e:
                logger.warning(f"gallery index Drive-mirror failed: {e}")

    except Exception as e:
        logger.exception(f"[{task_id}] /{cmd} job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        if disp is not None:
            # stop() is a no-op when finalize() already stopped it — but on
            # an error exit (no finalize) the watcher thread would otherwise
            # spin forever over a deleted task_dir.
            disp.stop()
        state.drop_job(task_id)
        state.drop_url_by_task(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)
