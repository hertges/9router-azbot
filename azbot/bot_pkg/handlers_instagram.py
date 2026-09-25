import os, re, shutil, subprocess, time, threading

from pyrogram import filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import config, state, drive, log
from .core import app
from .utils import new_task_id, throttled_edit, autoclean_if_enabled, guarded, safe_edit, esc, fmt_time, topic_root_id
from .dispatcher import LiveDispatcher
from .handlers_core import Authorized, cancel_kb

logger = log.get(__name__)

POST_RE = re.compile(r"instagram\.com/(p|reel|tv)/")
PROFILE_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]+)/?(?:\?|$)")

ARCHIVE_URLS = {
    # Explicit sub-extractor URLs — verified against gallery-dl 1.32 source:
    # the bare profile URL dispatches ONLY "posts" by default (Dispatch's
    # include list), and the old `-o instagram.highlights=true` options no
    # longer exist. Pointing each type at its own sub-extractor endpoint is
    # the only reliable way to get the right content.
    "posts": "https://www.instagram.com/{u}/posts/",
    "reels": "https://www.instagram.com/{u}/reels/",
    "stories": "https://www.instagram.com/stories/{u}/",
    "highlights": "https://www.instagram.com/{u}/highlights/",
    "tagged": "https://www.instagram.com/{u}/tagged/",
}
ARCHIVE_LABEL = {
    "posts": "🖼 Posts", "reels": "🎬 Reels", "stories": "⏳ Stories",
    "highlights": "⭐ Highlights", "tagged": "🏷 Tagged",
}

# Ported from the older (Bot-API, non-Pyrogram) version of this bot, whose
# gallery-dl invocation was noticeably more reliable at not getting
# rate-limited/blocked by Instagram mid-archive. It ran gallery-dl in-process
# via its Python API with these settings applied through gdl_config.set(...);
# we still shell out to the gallery-dl CLI as a separate subprocess (keeps
# gallery-dl's own memory footprint OS-managed and freed the moment each job
# ends, rather than permanently resident inside this long-running bot
# process — worth preserving given how tight memory already is on small
# hosts), so the same tuning is passed as -o KEY=VALUE overrides instead.
GDL_TUNING_ARGS = [
    "-o", "downloader.retries=6",
    "-o", "downloader.timeout=30",
    "-o", "downloader.part=true",
    "-o", "downloader.part-filter=true",
    "-o", "extractor.retries=6",
    "-o", "extractor.timeout=30",
    "-o", "extractor.instagram.sleep-request=1.5",
    "-o", "extractor.sleep-extractor=1.0",
    "-o", "extractor.instagram.videos=true",
    "-o", 'extractor.instagram.headers={"User-Agent": '
          '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", '
          '"Accept-Language": "en-US,en;q=0.9", '
          '"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}',
]
# Small pacing gap between archive types (posts → reels → stories → …) so a
# multi-type run doesn't look like one continuous burst of requests to
# Instagram — same spirit as the older bot's 1s gap between phases.
GDL_INTER_TYPE_PAUSE_S = 1.0


def dispatch_instagram(client, message, url, dest):
    """Entry point used both by /ig and by the universal /m /l dispatcher."""
    if POST_RE.search(url):
        # gallery-dl (not yt-dlp) here on purpose: yt-dlp only pulls video
        # and errors out with "No video formats found!" on photo-only or
        # carousel posts. gallery-dl handles photos/carousels/videos alike.
        from .handlers_subproc import queue_subprocess_job
        return queue_subprocess_job(client, message, "gallery", url, dest)

    m = PROFILE_RE.search(url)
    if not m:
        return message.reply_text("Couldn't parse that as an Instagram post or profile link.")
    username = m.group(1)
    if username.lower() in ("p", "reel", "tv", "stories", "explore"):
        return message.reply_text("Couldn't parse that as a profile link.")

    token = state.new_ig_picker_session(username, dest)
    message.reply_text(_ig_picker_text(username, dest), reply_markup=_ig_picker_kb(token))


def _ig_picker_text(username, dest):
    return (
        f"📷 <b>@{esc(username)}</b> — pick what to archive (→ {config.DEST_LABEL[dest]}):\n\n"
        f"Tap to select/deselect, then ▶️ Start. Stories are on by default. "
        f"Stories &amp; Highlights need a logged-in cookie profile (<code>/cookie</code>), "
        f"since Instagram doesn't serve those to logged-out requests."
    )


def _ig_picker_kb(token):
    s = state.get_ig_picker_session(token)
    selected = s["selected"] if s else set()

    def btn(t):
        mark = "✅ " if t in selected else "⬜ "
        return InlineKeyboardButton(mark + ARCHIVE_LABEL[t], callback_data=f"igtoggle:{token}:{t}")

    rows = [
        [btn("posts"), btn("reels")],
        [btn("stories"), btn("highlights")],
        [InlineKeyboardButton("🗑 Indexes", callback_data="igidx"), btn("tagged")],
        [InlineKeyboardButton("▶️ Start", callback_data=f"igstart:{token}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"igcancel:{token}")],
    ]
    return InlineKeyboardMarkup(rows)


@app.on_message(filters.command("ig") & Authorized)
@guarded
def cmd_ig(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return message.reply_text(
            "⚠️ <code>/ig &lt;post_url&gt;</code> or <code>/ig &lt;profile_url&gt;</code> "
            "(leeches to Telegram — use <code>/m &lt;url&gt;</code> to mirror to Drive instead)")
    dispatch_instagram(client, message, parts[1].strip(), "telegram")


# ── /igindex — per-user index manager ────────────────────────────────────
# Index ledgers live LOCALLY under data/indexes/<chat>/<user>.<type>.txt —
# never in Drive. Re-running an archive only fetches what's new; deleting an
# index makes the next run re-fetch everything for that user+type.

@app.on_message(filters.command(["igindex", "igindexes"]) & Authorized)
@guarded
def cmd_igindex(client, message):
    reply_to = message.id if message.chat.type != enums.ChatType.PRIVATE else None
    _send_index_manager(message.chat.id, reply_to=reply_to)


def _send_index_manager(chat_id, edit_msg=None, reply_to=None):
    entries = state.list_ig_indexes()
    total_kb = 0
    lines = []
    by_user = {}
    for u, t in entries:
        p = state.index_path(chat_id, u, t)
        try:
            n = sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
            total_kb += os.path.getsize(p) // 1024
        except OSError:
            n = 0
        by_user.setdefault(u, []).append((t, n))
    for u in sorted(by_user):
        types = ", ".join(f"{ARCHIVE_LABEL.get(t, t)}: {n}" for t, n in by_user[u])
        lines.append(f"• <b>{esc(u)}</b> — {types}")
    text = ("🗂 <b>Instagram indexes</b>\n\n"
            + ("\n".join(lines) if lines else "_No indexes yet — they're created on your first /ig archive._")
            + f"\n\nTotal: {len(entries)} ledger(s), ~{total_kb} KB • shared across chats, mirrored to Drive")

    rows = []
    for u in sorted(by_user):
        rows.append([InlineKeyboardButton(f"🗑 {u[:24]}", callback_data=f"igidxuser:{u}")])
    rows.append([InlineKeyboardButton("💥 Wipe ALL my indexes", callback_data="igidxwipe")])
    kb = InlineKeyboardMarkup(rows) if len(entries) else None

    if edit_msg is not None:
        safe_edit(app, chat_id, edit_msg, text, reply_markup=kb)
    else:
        # reply_to anchors this into the right forum topic (WZML-X-style —
        # see the note on _launch_status_and_job in handlers_core.py).
        app.send_message(chat_id, text, reply_markup=kb, reply_to_message_id=reply_to)


@app.on_callback_query(filters.regex(r"^igidx$"))
@guarded
def cb_igidx_open(client, cq):
    cq.answer()
    _send_index_manager(cq.message.chat.id)


@app.on_callback_query(filters.regex(r"^igidxuser:"))
@guarded
def cb_igidx_user(client, cq):
    username = cq.data.split(":", 1)[1]
    entries = [t for u, t in state.list_ig_indexes() if u == username]
    rows = [[InlineKeyboardButton(f"🗑 {ARCHIVE_LABEL.get(t, t)} ({t})",
                                  callback_data=f"igidxt:{username}:{t}")]
            for t in sorted(entries)]
    rows.append([InlineKeyboardButton("🗑 ALL of this user", callback_data=f"igidxt:{username}:all")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="igidxback")])
    safe_edit(cq, f"🗑 Delete indexes for <b>{esc(username)}</b>:",
              reply_markup=InlineKeyboardMarkup(rows))
    cq.answer()


@app.on_callback_query(filters.regex(r"^igidxt:"))
@guarded
def cb_igidx_delete(client, cq):
    payload = cq.data.split(":", 1)[1]
    username, atype = payload.rsplit(":", 1)
    n = state.delete_ig_index(username=None if atype == "all" else username,
                              archive_type=None if atype == "all" else atype)
    label = "ALL types" if atype == "all" else atype
    cq.answer(f"🗑 Deleted {n} ledger(s) ({label}).", show_alert=True)
    _send_index_manager(cq.message.chat.id, edit_msg=cq.message.id)


@app.on_callback_query(filters.regex(r"^igidxwipe$"))
@guarded
def cb_igidx_wipe(client, cq):
    n = state.delete_ig_index()
    cq.answer(f"💥 Wiped {n} ledger(s). Next archives start fresh.", show_alert=True)
    _send_index_manager(cq.message.chat.id, edit_msg=cq.message.id)


@app.on_callback_query(filters.regex(r"^igidxback$"))
@guarded
def cb_igidx_back(client, cq):
    _send_index_manager(cq.message.chat.id, edit_msg=cq.message.id)
    cq.answer()


@app.on_callback_query(filters.regex(r"^igtoggle:"))
@guarded
def cb_ig_toggle(client, cq):
    _, token, archive_type = cq.data.split(":", 2)
    s = state.toggle_ig_picker_type(token, archive_type)
    if not s:
        return cq.answer("This picker expired — send the link again.", show_alert=True)
    safe_edit(cq, _ig_picker_text(s["username"], s["dest"]), reply_markup=_ig_picker_kb(token))
    cq.answer()


@app.on_callback_query(filters.regex(r"^igcancel:"))
@guarded
def cb_ig_cancel(client, cq):
    token = cq.data.split(":", 1)[1]
    state.pop_ig_picker_session(token)
    safe_edit(cq, "Cancelled.")
    cq.answer()


@app.on_callback_query(filters.regex(r"^igstart:"))
@guarded
def cb_ig_start(client, cq):
    token = cq.data.split(":", 1)[1]
    s = state.pop_ig_picker_session(token)
    if not s:
        return cq.answer("This picker expired — send the link again.", show_alert=True)
    if not s["selected"]:
        return cq.answer("Select at least one option first.", show_alert=True)

    username, dest = s["username"], s["dest"]
    types = sorted(s["selected"], key=["posts", "reels", "stories", "highlights", "tagged"].index)
    cq.answer(f"Queued {len(types)} archive(s) for @{username}")
    safe_edit(cq, f"⚡ Queued <b>{esc(', '.join(types))}</b> for @{esc(username)} → {config.DEST_LABEL[dest]}…")

    if not state.ensure_free(config.MIN_FREE_MB):
        return cq.message.reply_text("❌ Disk full.")

    chat_id = cq.message.chat.id
    # One status message + one Cancel button for the WHOLE selection, not
    # one per archive type — previously each type got its own message and
    # its own fully-independent job, which also meant several gallery-dl
    # subprocesses + LiveDispatchers running at once (see the resource/OOM
    # note that used to be here). run_ig_multi_archive_job now runs every
    # selected type sequentially, in a single job, updating this one
    # message throughout and rolling everything into one final summary.
    task_id = new_task_id()
    # Topic anchoring WITHOUT a visible reply header: resolve the forum
    # topic's ROOT id from the picker message (which lives in the topic),
    # then anchor every send (status, files, summary) to it. Telegram
    # renders replies-to-topic-root as plain posts inside that topic —
    # so in topics everything lands IN the thread, and in PV (no topic
    # header) anchor resolves to 0 and every send is completely plain.
    # This replaces pass #4's "no anchoring at all", which dropped the
    # status message and live view into the forum's General topic.
    anchor = topic_root_id(cq.message)
    status = client.send_message(
        chat_id,
        f"⚡ Queued <b>{esc(', '.join(types))}</b> for @{esc(username)} — <code>{esc(task_id)}</code>",
        reply_markup=cancel_kb(task_id), reply_to_message_id=anchor or None)
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, chat_id, status.id, f"ig:{'+'.join(types)}", task_dir)
    # Both scaffolding messages (the edited picker "Queued…" reply and the
    # job status/live-view message) get DELETED once the job completes and
    # the final summary is sent — the chat is left with just the files and
    # the summary. The picker's id is threaded through for that cleanup.
    state.task_queue.put((run_ig_multi_archive_job,
                           (client, chat_id, status.id, username, types, task_id, dest,
                            anchor, cq.message.id)))


def run_ig_multi_archive_job(client, chat_id, msg_id, username, types, task_id, dest,
                             anchor=0, picker_msg_id=None):
    """Archives every selected type for one profile, one type at a time
    (bounded resource use — see cb_ig_start), reporting combined live
    progress and a single combined completion summary on ONE message.
    anchor = forum topic root id (0 for non-topic chats) — every send
    (files, summary) is anchored to it, landing in the thread as a plain
    post instead of a reply or a General-topic orphan."""
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    cookie_file = state.active_cookie_file(chat_id)
    t_start = time.time()
    icon = {"posts": "📸", "stories": "📖", "reels": "🎬",
            "highlights": "⭐", "tagged": "🏷️"}
    per_type = {}       # archive_type -> {downloaded, uploaded, bytes, errors, note}
    total_errors = []
    cancelled = False
    disp = None         # current type's LiveDispatcher (for error-path cleanup)
    cur = [None]        # archive_type currently being fetched
    live_disp = [None]  # its dispatcher (live ↓/☁ counts read straight off it)

    def _live_text(current_type=None, extra=""):
        """ONE live view for the whole run, exactly the format requested:
        the per-type block IS the progress — every line updates in place
        (↓ = downloaded, ⏭ = skipped, ☁ = uploaded, ⏳ = pending, live
        counts during the current type). Totals row underneath. No per-file
        progress bars. ↓/☁ counts come from the dispatcher itself (files it
        picked up off disk / files it finished sending) — not from parsing
        gallery-dl stdout, whose format was the source of the old "0 new
        files but 79 uploaded" contradiction."""
        lines = [f"📷 <b>@{esc(username)}</b> archive"]
        if current_type and extra:
            lines.append(extra)
        lines.append("")
        for t in types:
            st = per_type.get(t)
            ic = icon.get(t, "📷")
            if t == current_type:
                ld = live_disp[0]
                lines.append(f"{ic} <b>{t.title()}</b>: <code>{len(ld.seen) if ld else 0}</code> ↓ • "
                             f"<code>{ld.sent if ld else 0}</code> ☁️")
            elif st:
                if st.get("note") and not st["uploaded"]:
                    lines.append(f"{ic} {t.title()}: {esc(st['note'])}")
                else:
                    lines.append(f"{ic} {t.title()}: <code>{st['downloaded']}</code> ↓ • "
                                 f"<code>{st.get('skipped', 0)}</code> ⏭️ • <code>{st['uploaded']}</code> ☁️")
            else:
                lines.append(f"{ic} {t.title()}: ⏳")
        cur_d = len(live_disp[0].seen) if (current_type and live_disp[0]) else 0
        cur_u = live_disp[0].sent if (current_type and live_disp[0]) else 0
        lines.append("")
        lines.append(f"Σ <code>{sum(s['downloaded'] for s in per_type.values()) + cur_d}</code> ↓ • "
                     f"☁️ <code>{sum(s['uploaded'] for s in per_type.values()) + cur_u}</code> • "
                     f"⏭️ <code>{sum(s.get('skipped', 0) for s in per_type.values())}</code>")
        return "\n".join(lines)

    def _edit_live(current_type=None, extra="", force=False):
        throttled_edit(client, chat_id, msg_id, _live_text(current_type, extra),
                       markup=cancel_kb(task_id), force=force)

    try:
        if not shutil.which("gallery-dl"):
            return throttled_edit(client, chat_id, msg_id,
                                   "❌ <code>gallery-dl</code> isn't installed on this host.", force=True)

        for archive_type in types:
            if state.is_cancelled(task_id):
                cancelled = True
                break

            if archive_type in ("stories", "highlights") and not cookie_file:
                per_type[archive_type] = {"downloaded": 0, "uploaded": 0, "bytes": 0, "errors": [],
                                          "note": "needs a cookie profile"}
                continue

            type_dir = os.path.join(task_dir, archive_type)
            os.makedirs(type_dir, exist_ok=True)

            drive_folder = None
            if dest == "drive" and drive.enabled():
                drive_folder = f"Instagram/{username}/{archive_type}"

            archive_db = state.index_path(chat_id, username, archive_type)
            if not os.path.exists(archive_db):
                try:
                    from . import datastore
                    datastore.pull_index(chat_id, username, archive_type, archive_db)
                except Exception as e:
                    logger.warning(f"[{task_id}] index restore from Drive skipped: {e}")

            url = ARCHIVE_URLS[archive_type].format(u=username)
            args = ["gallery-dl", "--dest", type_dir, "--download-archive", archive_db]
            args += GDL_TUNING_ARGS
            if cookie_file:
                args += ["--cookies", cookie_file]
            args.append(url)

            # Files land in the thread as plain posts: anchored to the
            # topic ROOT (no visible reply header), never to the bot's own
            # status message (no threaded replies). PV: anchor=0 → plain.
            # max_workers=2: gallery-dl DOWNLOADS stay sequential (Instagram
            # rate limits — parallel fetches are how accounts get blocked),
            # but UPLOADS are independent of that and can run 2-wide (IG
            # items are mostly small images; big reels stay RAM-gated).
            disp = LiveDispatcher(client, chat_id, msg_id, type_dir, dest, reply_to=anchor or None,
                                   task_id=task_id, folder=drive_folder,
                                   caption_prefix=f"{icon.get(archive_type, '📷')} {archive_type.title()}",
                                   quiet=True, max_workers=2,
                                   on_sent=lambda fp, fs: _edit_live(cur[0]))

            logger.info(f"[{task_id}] /ig {archive_type} for @{username} starting (dest={dest})")
            cur[0] = archive_type
            live_disp[0] = disp
            per_type[archive_type] = {"downloaded": 0, "uploaded": 0, "bytes": 0,
                                      "errors": [], "skipped": 0}
            _edit_live(archive_type, force=True)
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     universal_newlines=True, bufsize=1)
            state.attach_proc(task_id, proc)

            last_activity = [time.time()]
            stalled = threading.Event()

            def _watchdog(proc=proc, last_activity=last_activity, stalled=stalled):
                while proc.poll() is None:
                    if state.is_cancelled(task_id):
                        return
                    if time.time() - last_activity[0] > config.STALL_TIMEOUT_S:
                        stalled.set()
                        logger.warning(f"[{task_id}] /ig stalled — no output for {config.STALL_TIMEOUT_S}s, stopping")
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

            full_log = []
            last_count = [0]
            try:
                for line in iter(proc.stdout.readline, ""):
                    last_activity[0] = time.time()
                    if state.is_cancelled(task_id):
                        break
                    full_log.append(line.rstrip())
                    # live view refresh driven by the DISPATCHER's seen-count
                    # (files landed on disk), not by stdout parsing
                    if len(disp.seen) != last_count[0]:
                        last_count[0] = len(disp.seen)
                        _edit_live(archive_type)
            except (AttributeError, ValueError):
                pass  # stdout closed by /cancel teardown — expected
            proc.wait()

            if stalled.is_set():
                disp.stop()
                mins = config.STALL_TIMEOUT_S // 60
                per_type[archive_type] = {"downloaded": len(disp.seen), "uploaded": 0, "bytes": 0, "errors": [],
                                          "skipped": 0, "note": f"stalled — no progress for {mins}m"}
                continue

            if state.is_cancelled(task_id):
                disp.stop()
                cancelled = True
                break

            _edit_live(archive_type, extra="⬆️ uploading new files…", force=True)
            sent, errors = disp.finalize(timeout=1800)

            if state.is_cancelled(task_id):
                cancelled = True
                break

            # "downloaded" = files the dispatcher actually picked up off disk
            # (gallery-dl also downloads already-archived items' metadata without
            # producing files, which stdout counting over-reported).
            downloaded = len(disp.seen)
            skipped = max(downloaded - sent, 0)
            note = None
            if sent == 0 and downloaded == 0:
                tail = "\n".join(full_log[-10:])
                note = "nothing new"
                if "error" in tail.lower() or "403" in tail or "login" in tail.lower():
                    note = "fetch failed — may need a fresh cookie"
            elif sent == 0 and downloaded > 0:
                note = f"{downloaded} file(s) failed to send"
            per_type[archive_type] = {"downloaded": downloaded, "uploaded": sent,
                                      "bytes": getattr(disp, "bytes_sent", 0),
                                      "errors": errors, "skipped": skipped, "note": note}
            total_errors.extend(errors)
            cur[0] = None
            live_disp[0] = None
            _edit_live(force=True)

            try:
                from . import datastore
                datastore.push_index(chat_id, username, archive_type)
            except Exception as e:
                logger.warning(f"index Drive-mirror failed: {e}")

            # Small gap before the next type (ported from the older bot) —
            # otherwise switching straight from one type's requests into the
            # next looks like one continuous burst to Instagram, raising the
            # odds of a rate-limit/block partway through a multi-type run.
            if archive_type != types[-1]:
                time.sleep(GDL_INTER_TYPE_PAUSE_S)

        # ── ONE completion summary — same block the live view showed, ────
        # finalized. Sent as a NEW plain message (no reply) per request;
        # the status message is left showing the last live view.
        elapsed = int(time.time() - t_start)
        total_downloaded = sum(st["downloaded"] for st in per_type.values())
        total_uploaded = sum(st["uploaded"] for st in per_type.values())
        total_bytes = sum(st.get("bytes", 0) for st in per_type.values())
        total_skipped = sum(st.get("skipped", 0) for st in per_type.values())

        if cancelled:
            header = "🛑 <b>Cancelled</b>"
        elif total_uploaded == 0 and not any(st.get("note") for st in per_type.values() if st.get("note") != "needs a cookie profile"):
            header = f"ℹ️ <b>Nothing new for @{esc(username)}</b>"
        else:
            header = f"✅ <b>@{esc(username)} archive complete!</b>"

        lines = [header, ""]
        if total_downloaded or total_uploaded or total_bytes:
            from .utils import fmtsz
            lines += [
                f"📥 <code>{total_downloaded}</code> downloaded",
                f"☁️ <code>{total_uploaded}</code> uploaded",
            ]
            if total_bytes:
                lines.append(f"📦 <code>{fmtsz(total_bytes)}</code>")
            lines.append(f"⏭️ <code>{total_skipped}</code> skipped")
            if total_errors:
                lines.append(f"⚠️ <code>{len(total_errors)}</code> failed")
            lines.append(f"⏱ {fmt_time(elapsed)}")
            lines.append("")
        for t in types:
            st = per_type.get(t)
            if not st:
                continue
            if st.get("note") and not st["uploaded"]:
                lines.append(f"{icon.get(t,'📷')} {t.title()}: {esc(st['note'])}")
            else:
                lines.append(f"{icon.get(t,'📷')} {t.title()}: <code>{st['downloaded']}</code> ↓ • "
                             f"<code>{st.get('skipped', 0)}</code> ⏭️ • <code>{st['uploaded']}</code> ☁️")
        lines.append(f"→ {config.DEST_LABEL[dest]}")

        try:
            client.send_message(chat_id, "\n".join(lines), reply_to_message_id=anchor or None)
        except Exception as e:
            logger.warning(f"summary send failed, falling back to status edit: {e}")
            throttled_edit(client, chat_id, msg_id, "\n".join(lines), force=True)
        else:
            # job done — the two scaffolding messages (Queued… reply and the
            # live-view status) are no longer needed; the summary above is
            # the permanent record. Left in place on the error path so
            # failures stay visible.
            for dmid in {i for i in (msg_id, picker_msg_id) if i}:
                try:
                    client.delete_messages(chat_id, [dmid])
                except Exception:
                    pass
        autoclean_if_enabled(client, chat_id, msg_id, None, dest)

    except Exception as e:
        logger.exception(f"[{task_id}] /ig job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        if disp is not None:
            # no-op when finalize() already stopped it; on an error exit the
            # watcher thread would otherwise spin forever over a deleted dir
            disp.stop()
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)
