"""Zip leech/mirror as standalone commands.

/zipl  — reply to a file or a message with files, or pass links: everything
         is downloaded, zipped into ONE archive and leeched to this chat.
/zipm  — same, but the archive is mirrored to Drive (#folder works).
/unzipl / unzipm — reply to a .zip/.rar/.7z archive (or pass a link to
         one): it is extracted and every inner file is sent to this chat.

Both support the shared payload grammar:
    /zipl url1 url2 | myname #folder
"""
import os, shutil, subprocess, time, zipfile

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import config, state, log, uploader, drive
from .state import CancelledError, set_unzip_multi, get_unzip_multi, \
    add_unzip_multi_part, pop_unzip_multi
from .core import app
from .utils import (new_task_id, throttled_edit, fmtsz, guarded, esc,
                    autoclean_if_enabled, zip_dir, parse_payload, progress_line,
                    smooth_speed, file_anchor, file_md5, zip_tail_ok)
from .handlers_core import Authorized, cancel_kb, _gdrive_file_id
from .handlers_drive import FOLDER_MIME
# v15 bug: `drive` was referenced by /zipm but never imported → NameError

logger = log.get(__name__)

ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tbz2",
                ".gz", ".bz2", ".xz")

# ── multipart archive support (WZML-X parity) ─────────────────────────────
# Volume schemes, all handled by 7z when every part sits in one directory
# and the FIRST volume is fed to it:
#   X.7z.001 / X.zip.001 / X.zip.001 …  (archive-ext + numeric triple)
#   X.001 / X.002 …                     (bare numeric triple)
#   X.part1.rar …                       (rar style — part1 first)
#   X.z01 … X.zip                       (winzip split — the .zip is the anchor)
#   X.r00 … X.rar                       (old winrar split — the .rar is the anchor)
import re as _re

_VOL_PATTERNS = (
    (_re.compile(r"\.part(\d+)\.rar$", _re.I), "part"),
    (_re.compile(r"\.part(\d{3})$", _re.I), "part"),
    (_re.compile(r"\.(\d{3})$", _re.I), "num"),
    (_re.compile(r"\.(z\d{2})$", _re.I), "z"),
    (_re.compile(r"\.(r\d{2})$", _re.I), "r"),
)

def _volume_key(fname):
    """(base, kind, numstr) when fname is a multi-volume PART, else None
    for plain archives. base = the shared name prefix all siblings carry."""
    low = os.path.basename(fname).lower()
    for pat, kind in _VOL_PATTERNS:
        m = pat.search(low)
        if m:
            return (os.path.basename(fname)[:m.start()], kind, m.group(1))
    return None

def _vol_sort_key(name):
    """Download/display order: the anchor volume (.001 / part1 / .zip / .rar)
    sorts first, then its numbered siblings."""
    low = os.path.basename(name).lower()
    v = _volume_key(name)
    if not v:
        return (0, 0, low)   # plain archive / anchor sorts first
    base, kind, num = v
    if kind == "part":
        return (1, int(num), low)
    if kind == "num":
        return (1, int(num), low)
    if kind == "z":
        return (0, 0 if low.endswith(".zip") else int(num[1:]), low)
    return (0, 0 if low.endswith(".rar") else int(num[1:]), low)

def _pick_anchor(names):
    """Returns (anchor_volume, ordered_part_names) for a set of sibling
    volume names. Raises ValueError when the FIRST volume (.001 / part1 /
    the .zip / the .rar) is missing from the set."""
    names = sorted(set(names), key=_vol_sort_key)
    first = names[0]
    v = _volume_key(first)
    if not v:
        return first, names          # .zip/.rar anchor already first
    base, kind, num = v
    if kind == "num" and num != "001":
        first = next((n for n in names if n.lower().endswith(".001")), None)
    elif kind == "part":
        # both .partN.rar and bare .partNNN schemes: the anchor is simply
        # the LOWEST-numbered part present (7z chains the rest by name)
        cands = [(int(_volume_key(n)[2]), n) for n in names
                 if (_volume_key(n) or ("", None, ""))[0] == base
                 and (_volume_key(n) or ("", None, ""))[1] == "part"]
        first = min(cands)[1] if cands else None
    elif kind == "z":
        first = next((n for n in names if n.lower().endswith(".zip")), None)
    elif kind == "r":
        first = next((n for n in names if n.lower().endswith(".rar")), None)
    if not first:
        if kind == "part":
            missing = f"{base}.part1(.rar)"
        else:
            missing = {"num": base + ".001", "z": base + ".zip",
                       "r": base + ".rar"}[kind]
        raise ValueError(f"First volume is missing: {missing}")
    return first, names


def _extract_multipart(anchor, dest_dir, report=None, cancelled=None):
    """Multi-volume extraction: 7z with the FIRST volume; it chains the
    siblings automatically (zipfile can't do spanned archives).

    ".partNNN" is NOT a scheme 7z chains (it handles .7z.001/.zip.001,
    .part1.rar, .z01). Those names here come from our own >2GB byte-split
    uploads — plain byte slices. 7z on the first slice dies with
    "Cannot open the file as archive" (the zip index lives in the LAST
    slice, not the first), so raw slices are concatenated in numeric order
    first. The join is then validated BEFORE 7z runs: a .zip must end with
    its EOCD record — a missing index means the set is short or a slice is
    truncated, which 7z would only report as the cryptic "Is not archive".
    When the index is missing, `zip -FF` repairs the central directory from
    the local file headers (best-effort recovery of everything up to the
    truncation point). Genuinely spanned sets never match .partNNN so they
    keep the direct 7z path.

    RAM discipline (this container has been OOM-killed before): EVERY
    subprocess streams its output to a log FILE — capture_output would
    buffer the full progress spam in RAM — and every phase (reassembly,
    repair, extraction) reports progress and honors cancellation."""
    if not shutil.which("7z"):
        raise RuntimeError("Multi-part archives need 7z (apt install p7zip-full).")
    os.makedirs(dest_dir, exist_ok=True)
    run_dir = os.path.dirname(anchor)
    m = _re.search(r"^(?P<base>.+)\.part(?P<num>\d+)$", os.path.basename(anchor), _re.I)
    src = anchor
    temps = []

    def _stop():
        if cancelled and cancelled():
            raise CancelledError("cancelled by user")

    if m:
        pad = len(m.group("num"))
        siblings = []
        for n in os.listdir(run_dir):
            sm = _re.match(r"^(?P<base>.+)\.part(?P<num>\d+)$", n, _re.I)
            if sm and sm.group("base").lower() == m.group("base").lower():
                siblings.append((int(sm.group("num")), os.path.join(run_dir, n)))
        siblings.sort()
        nums = [num for num, _ in siblings]
        gaps = [f"{m.group('base')}.part{n:0{pad}d}"
                for n in range(nums[0], nums[-1] + 1) if n not in set(nums)]
        if gaps:
            raise RuntimeError(
                f"Missing part(s): {', '.join(gaps)} — found only "
                + ", ".join(f"{os.path.basename(p)} ({fmtsz(os.path.getsize(p))})"
                            for _, p in siblings))
        if len(siblings) == 1:
            src = siblings[0][1]   # nothing to join — 7z gets the one slice
        else:
            total_join = sum(os.path.getsize(p) for _, p in siblings)
            # Join needs total_join free, the zip -FF repair another copy,
            # 7z room for the OUTPUT. Fail here with numbers instead of
            # dying midway on a small disk.
            _free_b = state.disk_free_mb() * 1024 * 1024
            if _free_b <= total_join + config.MIN_FREE_MB * 1024 * 1024:
                raise RuntimeError(
                    f"Not enough free disk to reassemble ({fmtsz(total_join)} of parts, "
                    f"{fmtsz(_free_b)} free) — free space with /clean or handle the "
                    f"parts on a bigger machine.")
            src = os.path.join(run_dir, f"__concat_{m.group('base')}"[:150])
            temps.append(src)
            copied = 0
            with open(src, "wb") as out:
                for _, p in siblings:
                    with open(p, "rb") as fh:
                        while True:
                            _stop()
                            piece = fh.read(1024 * 1024 * 8)
                            if not piece:
                                break
                            out.write(piece)
                            copied += len(piece)
                            if report:
                                report(f"🧩 Reassembling — {fmtsz(copied)} / {fmtsz(total_join)}")
        # Index check on the JOINED bytes: a .zip reassembles ONLY if the
        # end-of-archive record is present. Without it the set is short, a
        # slice is truncated, or the source zip itself was already cut short
        # when split — all invisible to size checks.
        if m.group("base").lower().endswith(".zip") and not zip_tail_ok(src):
            fixed = None
            why = None
            # zip -FF's RAM is entry-count proportional and outside our
            # control — stream its output to a log file and require enough
            # free RAM that the child can't tip the container into the OOM
            # killer (250MB-class boxes: the bot itself eats most of it).
            if not shutil.which("zip"):
                why = "the `zip` binary is missing"
            elif state.ram_free_mb() < 80:
                why = f"low RAM ({state.ram_free_mb()}MB free, repair needs ≥80MB)"
            else:
                need = os.path.getsize(src)
                if state.disk_free_mb() * 1024 * 1024 <= need + config.MIN_FREE_MB * 1024 * 1024:
                    why = "not enough free disk for the rebuilt copy"
            if why is None:
                fixed = os.path.join(run_dir, f"__fixed_{m.group('base')}"[:150])
                temps.append(fixed)
                if report:
                    report("🛠 Rebuilding zip index (zip -FF) — no % for this phase, "
                           "can take minutes…", force=True)
                t0 = time.time()
                with open(fixed + ".log", "wb") as lf:
                    proc = subprocess.Popen(
                        ["zip", "-q", "-FF", src, "--out", fixed],
                        stdout=lf, stderr=subprocess.STDOUT,
                        # zip -FF asks interactive questions ("Is this a
                        # single-disk archive?"). DEVNULL = instant EOF =
                        # it gives up and writes an EMPTY-but-valid zip
                        # (just an EOCD), which sailed through every
                        # check and surfaced as "Archive was empty".
                        # Answer y to every question instead.
                        stdin=subprocess.PIPE)
                    while proc.poll() is None:
                        _stop()
                        if time.time() - t0 > 3600:
                            proc.terminate()
                            raise RuntimeError("zip -FF repair timed out")
                        try:
                            proc.stdin.write(b"y\n")
                            proc.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
                        time.sleep(1)
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                # Accept the repair ONLY if it recovered actual entries:
                # an EOCD-only shell (22 bytes) has a valid index and
                # would otherwise pass straight through to 7z and come
                # out as "Archive was empty".
                entries = 0
                try:
                    with zipfile.ZipFile(fixed) as zf:
                        entries = len(zf.namelist())
                except Exception as e:
                    logger.warning(f"zip -FF output unreadable: {e}")
                if proc.returncode != 0 or entries == 0 or not zip_tail_ok(fixed):
                    logger.warning(f"zip -FF repair produced {entries} entries "
                                   f"(rc={proc.returncode}) — log: " + fixed + ".log")
                    fixed = None
                    if why is None:
                        why = (f"repair recovered only {entries} entr"
                               f"{'y' if entries == 1 else 'ies'} — the tail is "
                               f"damaged beyond local-header reconstruction")
            if fixed:
                logger.warning(f"[multipart] {m.group('base')} had no index — "
                               f"recovered with zip -FF")
                src = fixed
            else:
                sizes = ", ".join(f"{os.path.basename(p)}={fmtsz(os.path.getsize(p))}"
                                  for _, p in siblings)
                extra = f" (auto-repair skipped: {why})" if why else ""
                raise RuntimeError(
                    f"The reassembled zip has no end-of-archive index — the split "
                    f"continues past {os.path.basename(siblings[-1][1])}, a part is "
                    f"corrupt, or the source zip itself was already incomplete when "
                    f"split. Fetch/send {m.group('base')}.part{nums[-1] + 1:0{pad}d} "
                    f"onward if it exists. Parts joined: {sizes}.{extra}")
    # 7z with streamed progress: -bsp1 emits "%"-carrying progress on stdout;
    # read it in small chunks (never capture_output — that buffers EVERYTHING
    # in RAM and OOM-killed the container), mirror to a log file, and push a
    # throttled percentage edit to the status message.
    logp = os.path.join(run_dir, "__7z.log")
    # stdin=DEVNULL: an inherited terminal lets 7z's password prompt
    # block until the 4h timeout on protected archives — with DEVNULL it
    # fails fast and the tail below says why.
    proc = subprocess.Popen(
        ["7z", "x", "-y", f"-o{dest_dir}", "-bsp1", "-bso0", src],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    last_edit = 0.0
    t0 = time.time()
    try:
        with open(logp, "wb") as lf:
            while True:
                _stop()
                chunk = proc.stdout.read(1024)
                if not chunk:
                    break
                lf.write(chunk)
                now = time.time()
                if report and now - last_edit >= 5:
                    pct = _re.findall(rb"(\d{1,3})%", chunk)
                    if pct:
                        last_edit = now
                        report(f"📦 Extracting — {pct[-1].decode()}%")
                if time.time() - t0 > 14400:
                    proc.terminate()
                    raise RuntimeError("7z extraction timed out")
            proc.wait()
        if proc.returncode != 0:
            with open(logp, "rb") as fh:
                fh.seek(max(0, os.path.getsize(logp) - 2048))
                tail = fh.read().decode("utf-8", "ignore")
            raise RuntimeError(f"7z failed: {tail.strip()[:300]}")
        return os.listdir(dest_dir)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        for t in temps:
            try:
                os.remove(t)
            except OSError:
                pass

# ── multipart archive support ─────────────────────────────────────────────
# Three common volume schemes, all handled by 7z when the parts sit in one
# directory and the FIRST volume is passed to it:
#   .7z.001 / .zip.001 …      (numbered volumes, numeric ext)
#   .part1.rar / .part01.rar  (rar style, partN)
#   .z01 + .zip / .r00 + .rar (oldwinzip/winrar split style)
# NOTE: an older duplicate of the volume helpers lived here
# (_SPLIT_NUM_RE/_split_part_suffix/_is_volume). Zero callers, and it
# disagreed with the live _VOL_PATTERNS set — deleted so future edits
# can't "fix" the wrong copy. Live logic: _VOL_PATTERNS/_volume_key/
# _vol_sort_key/_pick_anchor above.
def _is_archive(name):
    low = name.lower()
    return any(low.endswith(e) for e in ARCHIVE_EXTS)


def _safe_extract_zip(src, dest_dir, max_entries=100000):
    """ZipSlip-proof extraction: every member must resolve INSIDE dest_dir
    (absolute paths and ../ escape → hard error, not a silent overwrite
    of code/credentials), plus entry-count and uncompressed-size caps so
    a zip bomb can't fill the disk."""
    base = os.path.realpath(dest_dir)
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise RuntimeError(f"archive blocked: {len(infos)} entries (>{max_entries})")
        total = sum(i.file_size for i in infos)
        free_b = state.disk_free_mb() * 1024 * 1024
        if total + config.MIN_FREE_MB * 1024 * 1024 > free_b:
            raise RuntimeError(f"archive blocked: unpacks to {fmtsz(total)} with {fmtsz(free_b)} free")
        for info in infos:
            target = os.path.realpath(os.path.join(base, info.filename.replace("\\", "/").lstrip("/")))
            if target != base and not target.startswith(base + os.sep):
                raise RuntimeError(f"archive blocked: unsafe entry {info.filename[:80]}")
        zf.extractall(dest_dir)
        return zf.namelist()

def _extract(src, dest_dir):
    """zipfile for plain .zip (fast, no deps), 7z for everything else —
    7z also handles RAR/rar5 and multipart volumes (p7zip-full's RAR
    plugin). Returns list of extracted top-level entries."""
    os.makedirs(dest_dir, exist_ok=True)
    if zipfile.is_zipfile(src):
        return _safe_extract_zip(src, dest_dir)
    if shutil.which("7z"):
        # stdout (per-file listing) goes to DEVNULL — capture_output would
        # buffer the whole listing in RAM and OOM-kill on big archives;
        # errors are tiny and come on stderr.
        r = subprocess.run(["7z", "x", "-y", f"-o{dest_dir}", "-bso0", src],
                           stderr=subprocess.PIPE, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"7z failed: {r.stderr.decode(errors='ignore').strip()[:200]}")
        return os.listdir(dest_dir)
    raise RuntimeError("No extractor available (install p7zip-full)")


def handle_multi_collect(client, message, _target=None):
    """Collects one part file for the /unzipmulti session. Wired from BOTH
    auto_leech (text messages — ignores these silently) and a dedicated
    filters.document handler below (file messages never reach auto_leech,
    since its filter is text-only — that was the "doesn't detect anything"
    bug)."""
    if not (message.document or message.photo or message.video):
        return
    chat_id = message.chat.id
    sess = get_unzip_multi(chat_id)
    if not sess:
        return
    n = add_unzip_multi_part(chat_id, message)
    total_bytes = 0
    for m in sess["msgs"]:
        d = getattr(m, "document", None)
        if d:
            total_bytes += int(d.file_size or 0)
    try:
        app.edit_message_text(
            chat_id, sess["status_id"],
            f"📦 <b>Unzip-multi</b> — collecting…\n\n"
            f"📥 <b>{n}</b> part(s) collected • {fmtsz(total_bytes)}\n\n"
            f"<code>{esc(message.document.file_name or 'part')}</code>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done — extract", callback_data="uzmdone"),
                InlineKeyboardButton("✖ Cancel", callback_data="uzmcancel"),
            ]]))
    except Exception:
        pass  # count is still tracked even if the edit is throttled


@app.on_message(filters.document & Authorized)
@guarded
def doc_multi_collect(client, message):
    """Documents sent during an active /unzipmulti session are collected
    here (auto_leech's filters.text can't see file messages)."""
    handle_multi_collect(client, message)


@app.on_callback_query(filters.regex(r"^uzmcancel$"))
@guarded
def cb_unzip_multi_cancel(client, cq):
    chat_id = cq.message.chat.id
    sess = pop_unzip_multi(chat_id)
    state.set_awaiting_input(chat_id, "unzipmulti_collect", None)  # clear
    if not sess:
        return cq.answer("No active session.", show_alert=True)
    cq.answer("Cancelled")
    try:
        app.edit_message_text(chat_id, sess["status_id"], "🛑 Session cancelled — nothing extracted.")
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^uzmdone$"))
@guarded
def cb_unzip_multi_done(client, cq):
    chat_id = cq.message.chat.id
    sess = pop_unzip_multi(chat_id)
    state.set_awaiting_input(chat_id, "unzipmulti_collect", None)  # clear
    if not sess:
        return cq.answer("No active session.", show_alert=True)
    msgs = sess.get("msgs") or []
    if not msgs:
        return cq.answer("No parts were sent.", show_alert=True)
    cq.answer("Starting extraction…")
    task_id = new_task_id()
    dest = sess["dest"]
    status = cq.message.reply_text(
        f"📦 Extracting {len(msgs)} part(s) → {config.DEST_LABEL[dest]}",
        reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, chat_id, status.id, "unzip", task_dir)
    state.task_queue.put((run_unzip_local_job,
                           (client, chat_id, status.id, msgs, task_id, cq.message.id,
                            dest, sess.get("folder"), file_anchor(cq.message))))


@app.on_message(filters.command(["unzip", "unzipl", "unzipm", "unzipmulti"]) & Authorized)
@guarded
def cmd_unzip(client, message):
    """Unpack archives: /unzip + /unzipl → inner files to this chat.
    /unzipm → inner files are mirrored to Drive (#folder works), one
    Drive file per archive entry, links collected in the summary.

    /unzipmulti → explicit MULTIPART session: send/forward the part files
    one by one (each is collected and counted live), then tap ✅ Done to
    extract; ✖ Cancel aborts. For parts that arrive as separate messages
    the history scan can't fully see."""
    dest = "drive" if message.command[0] == "unzipm" else "telegram"
    if dest == "drive" and not drive.enabled():
        return message.reply_text("☁️ Drive isn't configured — use <code>/unzip</code> to get the files here.")

    # /unzipmulti [#folder] — explicit collection session
    if message.command[0] == "unzipmulti":
        parts = message.text.split(maxsplit=1)
        folder = parts[1].strip().lstrip("#") if len(parts) > 1 else None
        set_unzip_multi(message.chat.id, dest, folder, 0)
        state.set_awaiting_input(message.chat.id, "unzipmulti_collect", None)
        return message.reply_text(
            "📦 <b>Unzip-multi session started</b>\n\n"
            "Send or forward the archive PARTS one by one — each document is "
            "collected and counted. When all parts are in, tap ✅ <b>Done</b> "
            "to extract; ✖ <b>Cancel</b> aborts.\n\n"
            + (f"📁 Drive folder: <code>{esc(folder)}</code>" if folder
               else f"→ 📱 Telegram ({config.DEST_LABEL[dest]})"),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done — extract", callback_data="uzmdone"),
                InlineKeyboardButton("✖ Cancel", callback_data="uzmcancel"),
            ]]))

    from .utils import parse_payload
    links, rename, folder = parse_payload(message.text.split(maxsplit=1)[1]
                                          if len(message.text.split(maxsplit=1)) > 1 else "")
    reply = message.reply_to_message

    # (d) Drive FOLDER link → the whole volume set lives in that folder
    if links and len(links) == 1 and "drive.google.com" in links[0]:
        fid = _gdrive_file_id(links[0])
        if fid:
            meta = drive.get_file_any(fid)
            if meta and meta.get("mimeType") == FOLDER_MIME:
                return _queue_unzip_drive(client, message, fid, meta.get("name", "folder"),
                                          dest, folder)

    # (a) reply to one part — if it came in a media GROUP (album forward),
    # every sibling document joins the volume set. If NOT grouped, the last
    # 50 messages of this chat are scanned for sibling parts sharing the
    # same volume base — parts sent as separate messages work too.
    part_msgs = []
    if reply and reply.document:
        docs = [reply]
        try:
            if reply.media_group_id:
                grp = client.get_media_group(message.chat.id, reply.media_group_id)
                docs = [m for m in grp if m.document]
        except Exception:
            pass
        vol_docs = [d for d in docs if _volume_key(d.document.file_name or "")]
        if vol_docs:
            if len(docs) > 1:
                # Album of mixed files: the DOMINANT volume base wins across
                # ALL schemes (.001/.partNNN/.partN.rar/.z01). A stray
                # (readme.txt, second archive) sorts before volumes in
                # _pick_anchor and would steal it — skip strays, don't fail.
                from collections import Counter
                bases = Counter(_volume_key(d.document.file_name or "")[0].lower()
                                for d in vol_docs)
                anchor_base = bases.most_common(1)[0][0]
                skipped = [d.document.file_name or "?" for d in docs
                           if (_volume_key(d.document.file_name or "") or ("?",))[0].lower() != anchor_base]
                if skipped:
                    logger.info(f"unzip reply: ignoring non-set file(s): {skipped}")
                part_msgs = [d for d in vol_docs
                             if _volume_key(d.document.file_name or "")[0].lower() == anchor_base]
            else:
                # Ungrouped single part: sweep recent history for siblings
                # sharing its base — works for .001, .partNNN, .partN.rar
                # AND .z01 sets alike (a lone .z01 used to die with
                # "First volume is missing" without ever looking).
                base = _volume_key(reply.document.file_name or "")[0]
                siblings = []
                try:
                    for m in client.get_chat_history(message.chat.id, limit=50):
                        if m.document and m.document.file_name:
                            k2 = _volume_key(m.document.file_name)
                            if k2 and k2[0].lower() == base.lower():
                                siblings.append(m)
                except Exception as e:
                    logger.warning(f"multipart history scan failed: {e}")
                part_msgs = siblings if len(siblings) >= 2 else [reply]
        elif _is_archive(reply.document.file_name or ""):
            return _queue_unzip_local(client, message, reply, dest, folder)

    if part_msgs:
        return _queue_unzip_local(client, message, part_msgs, dest, folder)
    if reply and reply.document and _is_archive(reply.document.file_name or ""):
        return _queue_unzip_local(client, message, reply, dest, folder)

    # (c) link batch where the last path segments look like volumes
    if len(links) >= 2:
        segs = [u.split("?")[0].rsplit("/", 1)[-1] for u in links]
        if any(_volume_key(s) for s in segs):
            return _queue_unzip_urls(client, message, links, dest, folder)

    if links:
        for url in links:
            task_id = new_task_id()
            status = message.reply_text(f"🔎 Queued <code>/unzip</code> — <code>{esc(task_id)}</code>",
                                        reply_markup=cancel_kb(task_id))
            task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
            state.register_job(task_id, message.chat.id, status.id, "unzip", task_dir)
            state.task_queue.put((run_unzip_url_job,
                                   (client, message.chat.id, status.id, url, task_id, message.id,
                                    dest, folder, file_anchor(message))))
        return

    return message.reply_text(
        "⚠️ Reply to an archive file (<code>.zip .rar .7z .tar…</code>), reply to its "
        "FIRST part (multipart: <code>.001</code>/<code>.part1.rar</code>/<code>.zip</code> of a "
        "<code>.z01</code> set), pass every part link, or pass a Drive folder link.\n\n"
        "For multipart sets sent as SEPARATE messages: reply to any one part — siblings "
        "are found automatically if they're within the last ~50 messages of this chat "
        "(or send them together as one album).")


def _queue_unzip_local(client, message, part_msgs, dest="telegram", folder=None):
    task_id = new_task_id()
    status = message.reply_text(f"🔎 Queued <code>/unzip</code> — <code>{esc(task_id)}</code>",
                                reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "unzip", task_dir)
    state.task_queue.put((run_unzip_local_job,
                           (client, message.chat.id, status.id, part_msgs, task_id, message.id,
                            dest, folder, file_anchor(message))))


def _queue_unzip_urls(client, message, urls, dest="telegram", folder=None):
    task_id = new_task_id()
    status = message.reply_text(f"🔎 Queued <code>/unzip</code> ({len(urls)} parts) — "
                                f"<code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "unzip", task_dir)
    state.task_queue.put((run_unzip_urls_job,
                           (client, message.chat.id, status.id, urls, task_id, message.id,
                            dest, folder, file_anchor(message))))


def _queue_unzip_drive(client, message, folder_id, folder_name, dest, folder):
    task_id = new_task_id()
    status = message.reply_text(f"🔎 Queued <code>/unzip</code> (Drive folder "
                                f"<code>{esc(folder_name[:40])}</code>) — <code>{esc(task_id)}</code>",
                                reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "unzip", task_dir)
    state.task_queue.put((run_unzip_drive_job,
                           (client, message.chat.id, status.id, folder_id, task_id, message.id,
                            dest, folder, file_anchor(message))))


def _finish_unzip(client, chat_id, msg_id, task_id, src_path, task_dir, reply_to,
                  dest="telegram", folder=None, force_7z=False, file_anchor=None):
    """Extract + send every inner file — to this chat (telegram) or
    mirrored to Drive (drive: one Drive file per entry, links collected).
    force_7z=True (multipart) skips the zipfile attempt: spanned volumes
    need 7z, which chains the sibling parts automatically."""
    try:
        throttled_edit(client, chat_id, msg_id, "📦 Extracting…", markup=cancel_kb(task_id), force=True)
        out_dir = os.path.join(task_dir, "extracted")

        def _report(text, force=False):
            # Progress edits must NEVER kill the extraction — on this
            # RAM-starved container even starting the edit timer thread can
            # fail ("can't start new thread"), and that traceback used to
            # abort a healthy 7z run mid-extraction.
            try:
                throttled_edit(client, chat_id, msg_id, text,
                               markup=cancel_kb(task_id) if task_id else None, force=force)
            except Exception as e:
                logger.warning(f"status update skipped: {type(e).__name__}: {e}")

        def _cancelled():
            return bool(task_id and state.is_cancelled(task_id))

        if force_7z:
            _extract_multipart(src_path, out_dir, report=_report, cancelled=_cancelled)
        else:
            _extract(src_path, out_dir)
        files = [os.path.join(r, f) for r, _, fs in os.walk(out_dir) for f in fs
                 if os.path.getsize(os.path.join(r, f)) > 0]
        if not files:
            return throttled_edit(client, chat_id, msg_id, "❌ Archive was empty.", force=True)

        sent, failed, drive_links, drive_ids = 0, [], [], []
        if dest == "drive":
            throttled_edit(client, chat_id, msg_id,
                           f"☁️ Mirroring {len(files)} file(s) to Drive…", force=True)
            for idx, fp in enumerate(files, 1):
                if state.is_cancelled(task_id):
                    break
                throttled_edit(client, chat_id, msg_id,
                               f"☁️ Uploading {idx}/{len(files)} to Drive…\n"
                               f"<code>{esc(os.path.basename(fp))}</code>",
                               markup=cancel_kb(task_id), force=True)
                try:
                    info = drive.upload_file_full(fp, task_id=task_id, folder=folder,
                                                  chat_id=chat_id)
                    if info:
                        sent += 1
                        drive_links.append(info["link"])
                        drive_ids.append(info["id"])
                    else:
                        # upload_file_full returns None when the account
                        # can't be resolved — say so instead of a bare
                        # "1 failed" that explains nothing
                        failed.append(f"{os.path.basename(fp)}: Drive upload returned nothing "
                                      f"(account not configured for this chat?)")
                except Exception as e:
                    failed.append(f"{os.path.basename(fp)}: {e}")
                finally:
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
            summary = f"✅ Unpacked — {sent}/{len(files)} file(s) → ☁️ Drive"
            if folder:
                summary += f" 📁<code>{esc(folder)}</code>"
            if failed:
                summary += f"\n⚠️ {len(failed)} failed"
                for fline in failed[:3]:
                    summary += f"\n• <code>{esc(fline[:120])}</code>"
            if drive_links:
                summary += "\n" + "\n".join(drive_links[:10])
                if len(drive_links) > 10:
                    summary += f"\n… +{len(drive_links) - 10} more"
            # per-file management card: rename/delete/send-to-telegram for
            # the LAST uploaded file (matches the single-file /m summary —
            # its buttons operated on the finished upload)
            kb = None
            if drive_ids:
                last_id = drive_ids[-1]
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📥 Send to Telegram", callback_data=f"qsnd:{last_id}"),
                    InlineKeyboardButton("🔗 Get link", callback_data=f"qlink:{last_id}"),
                ], [
                    InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{last_id}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{last_id}"),
                ]])
            throttled_edit(client, chat_id, msg_id, summary, markup=kb, force=True)
            autoclean_if_enabled(client, chat_id, msg_id, reply_to, "drive")
            return

        throttled_edit(client, chat_id, msg_id,
                       f"📤 Sending {len(files)} file(s)…", force=True)
        for fp in files:
            if state.is_cancelled(task_id):
                break
            try:
                uploader.upload_to_telegram(client, chat_id, fp, msg_id,
                                            reply_to=file_anchor, task_id=task_id)
                sent += 1
            except Exception as e:
                # One flaky file must not abort the rest (mirrors the
                # Drive branch above, which collects failed[] the same way).
                failed.append(f"{os.path.basename(fp)}: {e}")
            finally:
                try:
                    os.remove(fp)
                except OSError:
                    pass
        summary = f"✅ Unpacked — {sent}/{len(files)} file(s)"
        if failed:
            summary += f"\n⚠️ {len(failed)} failed"
            for fline in failed[:3]:
                summary += f"\n• <code>{esc(fline[:120])}</code>"
        throttled_edit(client, chat_id, msg_id, summary, force=True)
        autoclean_if_enabled(client, chat_id, msg_id, reply_to, "telegram")
    except state.CancelledError:
        raise
    except RuntimeError:
        raise   # EOCD/source diagnosis is refined by multipart callers
    except Exception as e:
        logger.exception("unzip job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)


def run_unzip_local_job(client, chat_id, msg_id, part_msgs, task_id, reply_to,
                        dest="telegram", folder=None, file_anchor=None):
    """MULTIPART from Telegram: every part message in the set is downloaded
    into one directory (7z chains them), then the anchor volume extracts.
    part_msgs accepts a single Message (plain archive) OR a list of them —
    the unpack-from-reply path passed a bare Message and crashed with
    "'Message' object is not iterable"."""
    from pyrogram.types import Message as _PyroMsg
    if isinstance(part_msgs, _PyroMsg):
        part_msgs = [part_msgs]
    part_msgs = [m for m in part_msgs if getattr(m, "document", None)]
    if not part_msgs:
        return throttled_edit(client, chat_id, msg_id,
                              "❌ Couldn't fetch the archive from that reply.", force=True)
    # Split-set filter: if ANY file looks like a split part (name.partN),
    # this session is a multipart set — the DOMINANT base wins and the rest
    # (a readme.txt dropped in the middle, a second archive, a renamed
    # stray) is skipped, not an error. A stray can't join the set, and
    # letting it reach _pick_anchor would steal the anchor (plain files sort
    # before volumes) and break extraction. With no part-style name at all
    # the legacy plain-archive path applies unchanged.
    part_info = []
    for m in part_msgs:
        nm = m.document.file_name or ""
        pm = _re.match(r"^(.+)\.part(\d+)$", nm, _re.I)
        part_info.append((m, nm, pm))
    skipped = []
    if any(pm for _m, _nm, pm in part_info):
        from collections import Counter
        counts = Counter(pm.group(1).lower() for _m, _nm, pm in part_info if pm)
        anchor_base = counts.most_common(1)[0][0]
        skipped = [nm for _m, nm, pm in part_info
                   if pm is None or pm.group(1).lower() != anchor_base]
        part_msgs = [m for m, nm, pm in part_info
                     if pm and pm.group(1).lower() == anchor_base]
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        names = [(m.document.file_name or f"part{i:03d}") for i, m in enumerate(part_msgs, 1)]
        try:
            anchor_name, ordered = _pick_anchor(names)
        except ValueError as e:
            return throttled_edit(client, chat_id, msg_id, f"❌ {esc(str(e))}", force=True)
        if skipped:
            logger.info(f"[{task_id}] ignoring non-set file(s): {skipped}")
        # Non-archive guard: bare .partNNN slices of a NON-archive
        # (file.bin, file.img, movie.mkv…) can never "extract" — joining
        # them just rebuilds the original file and 7z then fails
        # cryptically after gigabytes of downloading. Fail fast with the
        # right instruction instead.
        _am0 = _re.match(r"^(.+)\.part(\d+)$", anchor_name, _re.I)
        if _am0 and not _am0.group(1).lower().endswith(ARCHIVE_EXTS):
            return throttled_edit(
                client, chat_id, msg_id,
                f"❌ <code>{esc(anchor_name)}</code> is a raw slice of "
                f"<code>{esc(_am0.group(1))}</code>, which is not an archive "
                f"(<code>.zip .rar .7z .tar…</code>) — there is nothing to extract. "
                f"Mirror it instead: reply to the part(s) with <code>/m</code>.",
                force=True)
        # The bot's own split captions carry the true part count
        # ("file.zip.part001/004") — if fewer parts were selected than that,
        # abort BEFORE pulling 2GB slices down and name what's missing.
        cap_total = 0
        for m in part_msgs:
            cm = _re.search(r"\.part\d{3}/(\d{3})", getattr(m, "caption", "") or "")
            if cm:
                cap_total = max(cap_total, int(cm.group(1)))
        if cap_total and cap_total > len(ordered):
            am = _re.match(r"^(.+)\.part(\d+)$", anchor_name, _re.I)
            if am:
                base, pad = am.group(1), len(am.group(2))
                have = {int(x.group(1)) for x in
                        (_re.match(r"^(.+)\.part(\d+)$", n, _re.I) for n in names)
                        if x and x.group(1).lower() == base.lower()}
                missing = ", ".join(f"{base}.part{n:0{pad}d}"
                                    for n in range(1, cap_total + 1) if n not in have)
                return throttled_edit(client, chat_id, msg_id,
                                      f"❌ Set is incomplete: caption says {cap_total} parts "
                                      f"but only {len(ordered)} are here. Missing: {missing}. "
                                      f"Add them and retry.", force=True)
        name_to_msg = {}
        for nm, m in zip(names, part_msgs):
            name_to_msg[nm.lower()] = m

        total = len(ordered)
        sizes = {}
        md5s = {}
        for nm, m in zip(names, part_msgs):
            doc = getattr(m, "document", None)
            if doc:
                sizes[nm.lower()] = int(doc.file_size or 0)
            cm = _re.search(r"MD5 ([0-9a-f]{32})", getattr(m, "caption", "") or "")
            if cm:
                md5s[nm.lower()] = cm.group(1)
        done_bytes = 0
        plan_bytes = sum(sizes.values())
        # Disk pre-check BEFORE pulling gigabytes: the parts need
        # plan_bytes on disk, the join another copy, 7z room for output.
        if plan_bytes:
            _free_b = state.disk_free_mb() * 1024 * 1024
            if _free_b <= plan_bytes + config.MIN_FREE_MB * 1024 * 1024:
                return throttled_edit(
                    client, chat_id, msg_id,
                    f"❌ Not enough free disk: parts need <code>{fmtsz(plan_bytes)}</code>, "
                    f"only <code>{fmtsz(_free_b)}</code> free "
                    f"(plus reassembly needs headroom). Free space with /clean, "
                    f"or mirror the parts as-is with <code>/m</code>.",
                    force=True)

        def _tg_progress(offset):
            """Pyrogram download callback for the CURRENT part: renders
            aggregate bar (all parts) + per-part bar + speeds."""
            samples = []
            t0 = [time.time()]

            def cb(cur, tot):
                if state.is_cancelled(task_id):
                    raise CancelledError("cancelled by user")
                now = time.time()
                if now - t0[0] < 1.0 and cur != tot:
                    return
                t0[0] = now
                samples.append((cur, now))
                if len(samples) > 6:
                    samples.pop(0)
                agg_done = done_bytes + min(cur, tot)
                text = (f"⬇️ Part {i}/{total}\n"
                        f"{progress_line('📥', agg_done, plan_bytes)}\n"
                        f"<code>{esc(nm)}</code> — {fmtsz(cur)} / {fmtsz(tot)}\n"
                        f"⚡ {smooth_speed(samples)}")
                throttled_edit(client, chat_id, msg_id, text, markup=cancel_kb(task_id))
            return cb

        for i, nm in enumerate(ordered, 1):
            if state.is_cancelled(task_id):
                return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
            m = name_to_msg.get(nm.lower())
            part_size = sizes.get(nm.lower(), 0)
            throttled_edit(client, chat_id, msg_id,
                           f"⬇️ Part {i}/{total}\n{progress_line('📥', done_bytes, plan_bytes)}\n"
                           f"<code>{esc(nm)}</code>",
                           markup=cancel_kb(task_id), force=True)
            # Verify the part actually arrived whole (size + MD5 when the
            # caption carries the split-time fingerprint), RETRY once on
            # mismatch. A truncated or altered part poisons the reassembly —
            # a zip missing its tail can't be opened at all ("Is not
            # archive"), with no hint WHICH part to resend.
            fp = None
            got = 0
            last_err = ""
            want_md5 = md5s.get(nm.lower())
            for attempt in (1, 2):
                try:
                    fp = client.download_media(m, file_name=os.path.join(task_dir, nm),
                                                progress=_tg_progress(i))
                except Exception as e:
                    # A RAISED download (FloodWait, reset, expired file
                    # ref) used to escape with the status frozen on this
                    # part — wait out floods, name anything else.
                    fp = None
                    if type(e).__name__ == "FloodWait" and getattr(e, "value", 0):
                        logger.warning(f"[{task_id}] part {nm} FloodWait {e.value}s")
                        time.sleep(e.value + 1)
                        last_err = f"FloodWait ({e.value}s)"
                        continue
                    last_err = f"{type(e).__name__}: {e}"
                    logger.warning(f"[{task_id}] part {nm} download raised {last_err}")
                    continue
                got = os.path.getsize(fp) if fp else 0
                ok_size = fp and (not part_size or got == part_size)
                got_md5 = file_md5(fp) if (fp and want_md5) else None
                ok_md5 = (not want_md5) or got_md5 == want_md5
                if fp and ok_size and ok_md5:
                    break
                logger.warning(f"[{task_id}] part {nm} download attempt {attempt} bad: "
                               f"size {fmtsz(got)}/{fmtsz(part_size) or '?'}"
                               + (f", md5 {'match' if got_md5 == want_md5 else 'MISMATCH'}"
                                  if want_md5 else ""))
                fp = None
            if not fp:
                why = (f"MD5 mismatch — the bytes Telegram returned differ from what "
                       f"the bot split" if want_md5 else
                       (f"{last_err}" if last_err else
                        f"{fmtsz(got)} of {fmtsz(part_size)}"))
                return throttled_edit(client, chat_id, msg_id,
                                      f"❌ Part <code>{esc(nm)}</code> kept arriving broken "
                                      f"({why}). Retry the job — if it repeats on every "
                                      f"attempt, the copy in the chat itself is damaged.",
                                      force=True)
            done_bytes += part_size

        anchor_path = os.path.join(task_dir, anchor_name)
        if not os.path.exists(anchor_path):
            anchor_path = next((os.path.join(task_dir, n) for n in os.listdir(task_dir)
                                if n.lower() == anchor_name.lower()), anchor_path)
        try:
            _finish_unzip(client, chat_id, msg_id, task_id, anchor_path, task_dir, reply_to,
                          dest=dest, folder=folder, force_7z=True, file_anchor=file_anchor)
        except RuntimeError as e:
            msg = str(e)
            if "end-of-archive index" in msg and md5s and len(md5s) == len(names):
                # Every part was already verified byte-exact against its
                # split-time MD5 — the set is complete and faithful, so the
                # index was missing from the SOURCE zip itself: it was
                # truncated before the bot ever split it. Replace the generic
                # message with the definitive diagnosis.
                return throttled_edit(
                    client, chat_id, msg_id,
                    "❌ All parts verified byte-exact against their split-time checksums "
                    "— the set is complete, but the SOURCE zip was already truncated "
                    "when the bot split it (its own end-of-archive index was missing). "
                    "These parts can never reassemble into a working zip — re-fetch "
                    "the original file.", force=True)
            raise
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


def run_unzip_urls_job(client, chat_id, msg_id, urls, task_id, reply_to,
                       dest="telegram", folder=None, file_anchor=None):
    """MULTIPART from links: each part URL downloads into the same directory
    (aria2c), then the anchor volume extracts."""
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        from . import engine
        segs = {u: u.split("?")[0].rsplit("/", 1)[-1] for u in urls}
        # volume links only — a stray non-volume link (readme page etc.)
        # would otherwise win the anchor sort and 7z would choke on it
        vols = {u: s for u, s in segs.items() if _volume_key(s)}
        if len(vols) < 2:
            return throttled_edit(client, chat_id, msg_id,
                                  "❌ Pass at least TWO part links "
                                  "(<code>.001</code>+<code>.002</code>, <code>.part1.rar</code>…).",
                                  force=True)
        try:
            anchor_name, ordered_names = _pick_anchor(list(vols.values()))
        except ValueError as e:
            return throttled_edit(client, chat_id, msg_id, f"❌ {esc(str(e))}", force=True)
        # Same filename on two mirrors collapses in a plain dict (one
        # URL lost → same bytes twice or KeyError). Keep a queue per name.
        urls_by_name = {}
        for u, s in vols.items():
            urls_by_name.setdefault(s, []).append(u)

        for i, nm in enumerate(ordered_names, 1):
            if state.is_cancelled(task_id):
                return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
            queue = urls_by_name.get(nm) or []
            if not queue:
                return throttled_edit(client, chat_id, msg_id,
                                      f"❌ No link left for part <code>{esc(nm)}</code> — "
                                      f"pass distinct part links.", force=True)
            u = queue.pop(0)
            engine.download_direct(u, chat_id, msg_id, task_id, client, task_dir,
                                   label=f"📦 Part {i}/{len(ordered_names)}")

        anchor_path = os.path.join(task_dir, anchor_name)
        if not os.path.exists(anchor_path):
            cand = [n for n in os.listdir(task_dir) if n.lower() == anchor_name.lower()]
            if not cand:
                return throttled_edit(client, chat_id, msg_id,
                                      "❌ Anchor volume didn't download — check the part links.", force=True)
            anchor_path = os.path.join(task_dir, cand[0])
        _finish_unzip(client, chat_id, msg_id, task_id, anchor_path, task_dir, reply_to,
                      dest=dest, folder=folder, force_7z=True, file_anchor=file_anchor)
    except engine.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except engine.DownloadError as e:
        throttled_edit(client, chat_id, msg_id, f"❌ Couldn't download:\n<code>{esc(str(e)[:250])}</code>", force=True)
    except Exception as e:
        logger.exception("unzip-urls job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


def run_unzip_drive_job(client, chat_id, msg_id, drive_folder_id, task_id, reply_to,
                        dest="telegram", folder=None, file_anchor=None):
    """MULTIPART from a Drive folder: every volume file in that folder is
    downloaded (16MB resumable chunks), then the anchor volume extracts."""
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        files = drive.list_folder_any(drive_folder_id)
        parts = [f for f in files if f.get("mimeType") != FOLDER_MIME
                 and f.get("name") and _volume_key(f["name"])]
        if not parts:
            return throttled_edit(client, chat_id, msg_id,
                                  "❌ No multi-volume parts found in that Drive folder "
                                  "(<code>.001…</code> / <code>.partN.rar</code> / <code>.z01…</code>).",
                                  force=True)
        names = [f["name"] for f in parts]
        try:
            anchor_name, ordered_names = _pick_anchor(names)
        except ValueError as e:
            return throttled_edit(client, chat_id, msg_id, f"❌ {esc(str(e))}", force=True)
        by_name = {f["name"]: f for f in parts}

        for i, nm in enumerate(ordered_names, 1):
            if state.is_cancelled(task_id):
                return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
            f = by_name[nm]
            throttled_edit(client, chat_id, msg_id,
                           f"⬇️ Part {i}/{len(ordered_names)} from Drive…\n<code>{esc(nm)}</code>",
                           markup=cancel_kb(task_id), force=True)
            local = os.path.join(task_dir, nm)
            drive.download_file_content(f["id"], local,
                                        task_id=task_id, mime_type=f.get("mimeType"))
            want = int(f.get("size") or 0)
            got = os.path.getsize(local)
            if want and got != want:
                return throttled_edit(client, chat_id, msg_id,
                                      f"❌ Part <code>{esc(nm)}</code> downloaded broken: "
                                      f"{fmtsz(got)} of {fmtsz(want)} — retry the job.",
                                      force=True)

        anchor_path = os.path.join(task_dir, anchor_name)
        if not os.path.exists(anchor_path):
            cand = [n for n in os.listdir(task_dir) if n.lower() == anchor_name.lower()]
            if not cand:
                return throttled_edit(client, chat_id, msg_id,
                                      "❌ Anchor volume didn't download.", force=True)
            anchor_path = os.path.join(task_dir, cand[0])
        _finish_unzip(client, chat_id, msg_id, task_id, anchor_path, task_dir, reply_to,
                      dest=dest, folder=folder, force_7z=True, file_anchor=file_anchor)
    except Exception as e:
        logger.exception("unzip-drive job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


def run_unzip_url_job(client, chat_id, msg_id, url, task_id, reply_to,
                      dest="telegram", folder=None, file_anchor=None):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        throttled_edit(client, chat_id, msg_id, "⬇️ Downloading archive…", markup=cancel_kb(task_id), force=True)
        from . import engine
        fpath = engine.download_direct(url, chat_id, msg_id, task_id, client, task_dir)
        if not _is_archive(fpath) and not zipfile.is_zipfile(fpath):
            return throttled_edit(client, chat_id, msg_id,
                                   "❌ That link isn't an archive I can unpack.", force=True)
        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
        _finish_unzip(client, chat_id, msg_id, task_id, fpath, task_dir, reply_to,
                      dest=dest, folder=folder, file_anchor=file_anchor)
    except engine.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except engine.DownloadError as e:
        throttled_edit(client, chat_id, msg_id, f"❌ Couldn't download:\n<code>{esc(str(e)[:250])}</code>", force=True)
    except Exception as e:
        logger.exception("unzip-url job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


# ── /zip — gather files (reply media, multiple replies, or links) → one zip ──

@app.on_message(filters.command(["zipl", "zipm"]) & Authorized)
@guarded
def cmd_zip(client, message):
    """Zip-leech (/zipl → Telegram) / zip-mirror (/zipm → Drive)."""
    dest = "drive" if message.command[0] == "zipm" else "telegram"
    if dest == "drive" and not drive.enabled():
        return message.reply_text("☁️ Drive isn't configured — use <code>/zipl</code> to get the archive here.")

    raw = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    links, rename, folder = parse_payload(raw)

    sources = []          # either ("msg", Message) or ("url", str)
    reply = message.reply_to_message
    if reply and reply.media and not (reply.text or reply.caption):
        sources.append(("msg", reply))
    elif reply:
        m = config.URL_RE.search(reply.text or reply.caption or "")
        if m:
            links.append(m.group(0).rstrip(").,]>\"'"))
    for u in links:
        sources.append(("url", u))

    if not sources:
        return message.reply_text(
            "⚠️ Reply to one or more files with <code>/zip</code>, or pass link(s):\n"
            "<code>/zip &lt;link1&gt; &lt;link2&gt; | name.zip</code>")

    if len(links) > 1 and rename:
        rename = None  # ambiguous for batch — keep auto names
    task_id = new_task_id()
    tag = "→☁️ Drive" if dest == "drive" else "→📱 Telegram"
    label = f"<code>{esc(rename)}</code>" if rename else f"— <code>{esc(task_id)}</code>"
    status = message.reply_text(f"📦 Queued <code>/{esc(message.command[0])}</code> {tag} {label}",
                                reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "zip", task_dir)
    state.task_queue.put((run_zip_job,
                           (client, message.chat.id, status.id, sources, task_id, message.id,
                            dest, rename, folder, file_anchor(message))))


def run_zip_job(client, chat_id, msg_id, sources, task_id, reply_to, dest, rename, folder,
                file_anchor=None):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    work = os.path.join(task_dir, "files")
    os.makedirs(work, exist_ok=True)
    try:
        got, failed = [], []
        total_src = len(sources)
        for i, (kind, obj) in enumerate(sources, 1):
            if state.is_cancelled(task_id):
                break
            throttled_edit(client, chat_id, msg_id,
                           f"⬇️ Gathering source {i}/{total_src}…", markup=cancel_kb(task_id))
            try:
                if kind == "msg":
                    fp = client.download_media(obj, file_name=os.path.join(work, ""))
                    if fp:
                        got.append(fp)
                else:
                    from . import engine
                    sub = os.path.join(work, f"src{i:02d}")
                    os.makedirs(sub, exist_ok=True)
                    if engine.is_direct_file_link(obj) or engine.probe_is_direct_file(obj):
                        fp = engine.download_direct(obj, chat_id, msg_id, task_id, client, sub)
                    else:
                        fp, _ = engine.download(obj, chat_id, msg_id, task_id, client, sub)
                    if fp:
                        got.append(fp)
                    else:
                        failed.append(f"{str(obj)[:60]}: empty download")
            except Exception as e:
                logger.warning(f"[{task_id}] zip source failed ({kind}): {e}")
                failed.append(str(obj)[:60])

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
        if not got:
            return throttled_edit(client, chat_id, msg_id,
                                   "❌ Nothing could be gathered for zipping.", force=True)

        zip_name = None
        if rename:
            zip_name = rename if rename.lower().endswith(".zip") else rename + ".zip"
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base = folder or "bundle"
            zip_name = f"{base}-{stamp}.zip"
        zpath = os.path.join(task_dir, zip_name)
        throttled_edit(client, chat_id, msg_id, "📦 Zipping…", force=True)
        zip_dir(work, zpath)

        size = os.path.getsize(zpath)
        throttled_edit(client, chat_id, msg_id,
                       f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", force=True)
        if dest == "drive":
            info = drive.upload_file_full(zpath, task_id=task_id, rename=rename,
                                          folder=folder, chat_id=chat_id)
            summary = f"✅ Zipped <code>{len(got)}</code> file(s) → ☁️ Drive"
            kb = None
            if info:
                summary += f"\n{info['link']}"
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔗 Get link", callback_data=f"qlink:{info['id']}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{info['id']}"),
                ]])
            throttled_edit(client, chat_id, msg_id, summary, markup=kb, force=True)
        else:
            uploader.upload_to_telegram(client, chat_id, zpath, msg_id,
                                        reply_to=file_anchor,
                                        task_id=task_id)
            summary = (f"✅ Zipped <code>{len(got)}</code> file(s) — <code>{fmtsz(size)}</code>")
            if failed:
                summary += (f"\n⚠️ {len(failed)} source(s) failed:"
                            f"\n<code>{esc(chr(10).join(failed[:5]))}</code>")
            throttled_edit(client, chat_id, msg_id, summary, force=True)
        autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)
    except state.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except Exception as e:
        logger.exception("zip job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)
