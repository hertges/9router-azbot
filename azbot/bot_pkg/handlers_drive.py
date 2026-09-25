import os, threading

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import config, drive, state, log, uploader
from .core import app
from .utils import fmtsz, guarded, safe_edit, esc, new_task_id, throttled_edit, cancel_kb, file_anchor
from .handlers_core import Authorized

logger = log.get(__name__)

def _safe_drive_name(name):
    """Bare basename for a Drive filename (slashes/`..` confined).
    Returns '' when nothing usable remains — callers fall back."""
    if not name:
        return ""
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    return "" if base in ("", ".", "..") else base

PAGE_SIZE = 10
FOLDER_MIME = "application/vnd.google-apps.folder"


def _need_drive(message_or_query):
    if drive.enabled():
        return False
    text = "☁️ Drive isn't configured on this bot (no GCP credentials in <code>.env</code>)."
    if hasattr(message_or_query, "data"):
        message_or_query.answer(text, show_alert=True)
    else:
        message_or_query.reply_text(text)
    return True


def _acct(chat_id):
    return state.get_drive_account(chat_id)


def _get_listing(chat_id, folder_id):
    files = state.get_cached_listing(chat_id, folder_id)
    if files is None:
        files = drive.list_folder_full(folder_id, account=_acct(chat_id))
        state.set_cached_listing(chat_id, folder_id, files)
    return files


def _file_row(f):
    is_folder = f.get("mimeType") == FOLDER_MIME
    icon = "📁" if is_folder else "📄"
    size = f" · {fmtsz(int(f['size']))}" if f.get("size") else ""
    name = f.get("name", "?")
    label = f"{icon} {name[:28]}{'…' if len(name) > 28 else ''}{size}"
    # Folders open the detail card (with Rename/Delete/Link/Open buttons);
    # a long-press-free single tap used to jump straight inside, hiding
    # those actions. Now BOTH files and folders show the detail view.
    return [InlineKeyboardButton(label, callback_data=f"drvopen:{f['id']}")]


def _render(chat_id, page=0):
    nav = state.get_drive_nav(chat_id)
    folder_id, folder_name = nav[-1]
    files = _get_listing(chat_id, folder_id)

    total_pages = max(1, -(-len(files) // PAGE_SIZE))  # ceil div
    page = max(0, min(page, total_pages - 1))
    page_files = files[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

    breadcrumb = " › ".join(n for _, n in nav[-3:])
    n = len(files)
    # Folder overview in the header: item count, subfolders, total size —
    # so a 5000-file folder is understandable at a glance, not just "5000
    # items — page 3 of 500".
    count_line = "<i>(empty)</i>" if n == 0 else f"{n} item{'s' if n != 1 else ''}"
    if total_pages > 1:
        count_line += f" — page {page + 1} of {total_pages}"
    acct = _acct(chat_id)
    head = f"☁️ <b>Drive</b> <i>[{esc(acct)}]</i>" if acct else "☁️ <b>Drive</b>"
    text = f"{head}\n📍 {breadcrumb}\n\n{count_line}"
    if n:
        total_bytes = sum(int(f.get("size") or 0) for f in files)
        n_dirs = sum(1 for f in files if f.get("mimeType") == FOLDER_MIME)
        bits = [fmtsz(total_bytes)]
        if n_dirs:
            bits.append(f"{n_dirs} 📁")
        text += f"\n{' • '.join(bits)}"

    rows = []
    if len(nav) > 1:
        rows.append([InlineKeyboardButton(f"⬆️ Back to {nav[-2][1][:20]}", callback_data="drvup")])
    rows += [_file_row(f) for f in page_files]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"drvpg:{page - 1}"))
    # tapping the page index → jump-to-page input (reply with a number)
    nav_row.append(InlineKeyboardButton(f"⏭ {page + 1}/{total_pages}", callback_data="drvjumppg"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"drvpg:{page + 1}"))
    if len(nav_row) > 1:
        rows.append(nav_row)

    bottom = [InlineKeyboardButton("📁 New folder", callback_data="drvnewfolder"),
              InlineKeyboardButton("🔄 Refresh", callback_data="drvrefresh")]
    rows.append(bottom)

    return text, InlineKeyboardMarkup(rows)


def _show(chat_id, msg_id, page=0, cq=None):
    text, kb = _render(chat_id, page)
    if cq:
        safe_edit(cq, text, reply_markup=kb, disable_web_page_preview=True)
    else:
        # plain edit path — used after an early cq.answer(), where the
        # callback query can no longer carry the edit
        try:
            app.edit_message_text(chat_id, msg_id, text, reply_markup=kb,
                                  disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"drive edit failed: {e}")


@app.on_message(filters.command("drive") & Authorized)
@guarded
def cmd_drive(client, message):
    if _need_drive(message):
        return
    state.reset_drive_nav(message.chat.id)
    state.invalidate_listing(message.chat.id)
    # Acknowledge INSTANTLY, then fill in. Listing a big folder (or waiting
    # for the Drive API lock during an upload) can take seconds — v15/v16
    # sent nothing until the listing finished, which looked exactly like
    # "the command gives no answer".
    notice = message.reply_text("☁️ Opening Drive…")
    try:
        text, kb = _render(message.chat.id, 0)
        app.edit_message_text(message.chat.id, notice.id, text, reply_markup=kb,
                              disable_web_page_preview=True)
    except Exception as e:
        logger.exception("drive render failed")
        throttled_edit(client, message.chat.id, notice.id,
                       f"❌ Drive listing failed: <code>{esc(str(e)[:200])}</code>",
                       force=True)


@app.on_message(filters.command("drivesearch") & Authorized)
@guarded
def cmd_drivesearch(client, message):
    if _need_drive(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return message.reply_text("⚠️ <code>/drivesearch &lt;query&gt;</code>")
    q = parts[1].strip()
    files = drive.search_files(q, account=_acct(message.chat.id))
    if not files:
        return message.reply_text(f"☁️ No results for <code>{esc(q)}</code>.")
    rows = [_file_row(f) for f in files[:PAGE_SIZE]]
    message.reply_text(f"☁️ <b>Search:</b> {esc(q)}", reply_markup=InlineKeyboardMarkup(rows),
                       disable_web_page_preview=True)


# ── File / folder detail view — get link, send to Telegram, rename, delete ──

@app.on_callback_query(filters.regex(r"^drvnoop$"))
@guarded
def cb_drive_noop(client, cq):
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvjumppg$"))
@guarded
def cb_drive_jump_start(client, cq):
    """Tapping the page index starts a jump-to-page input: the next plain
    text message from this chat is the page number to open."""
    chat_id = cq.message.chat.id
    folder_id = state.get_drive_nav(chat_id)[-1][0]
    files = _get_listing(chat_id, folder_id)
    total_pages = max(1, -(-len(files) // PAGE_SIZE))
    if total_pages <= 1:
        return cq.answer("Only one page here.")
    state.set_awaiting_input(chat_id, "drive_page", cq.message.id)
    cq.answer(f"Send a page number (1–{total_pages})…", show_alert=True)


def handle_drive_page_jump(client, message, browser_msg_id):
    """Consumed from auto_leech's pending-input hook: jump the browser
    message to the page the user typed."""
    try:
        page = int((message.text or "").strip())
    except ValueError:
        # not a number — restore the input so they can try again
        state.set_awaiting_input(message.chat.id, "drive_page", browser_msg_id)
        return message.reply_text("⚠️ Send just a number, e.g. <code>17</code>")
    _show(message.chat.id, browser_msg_id, page - 1)


@app.on_callback_query(filters.regex(r"^drvpg:"))
@guarded
def cb_drive_page(client, cq):
    cq.answer()
    page = int(cq.data.split(":", 1)[1])
    _show(cq.message.chat.id, cq.message.id, page)


@app.on_callback_query(filters.regex(r"^drvcd:"))
@guarded
def cb_drive_cd(client, cq):
    cq.answer()
    folder_id = cq.data.split(":", 1)[1]
    files = _get_listing(cq.message.chat.id, state.get_drive_nav(cq.message.chat.id)[-1][0])
    name = next((f["name"] for f in files if f["id"] == folder_id), folder_id)
    state.push_drive_nav(cq.message.chat.id, folder_id, name)
    _show(cq.message.chat.id, cq.message.id, 0)


@app.on_callback_query(filters.regex(r"^drvup$"))
@guarded
def cb_drive_up(client, cq):
    cq.answer()
    state.pop_drive_nav(cq.message.chat.id)
    _show(cq.message.chat.id, cq.message.id, 0)


@app.on_callback_query(filters.regex(r"^drvrefresh$"))
@guarded
def cb_drive_refresh(client, cq):
    cq.answer("Refreshed")
    state.invalidate_listing(cq.message.chat.id)
    _show(cq.message.chat.id, cq.message.id, 0)


def _item_view(f, item_id, listing=None):
    """Shared detail card for a Drive file OR folder. For folders, the
    pre-fetched listing (when the caller has it cached) provides the
    number of direct children and their combined size — Drive's API only
    reports folder size indirectly, and a '📁 folder — 2652 items •
    18.4 GB' line beats a bare 'folder'."""
    is_folder = f.get("mimeType") == FOLDER_MIME
    size = fmtsz(int(f["size"])) if f.get("size") else "—"
    icon = "📁" if is_folder else "📄"
    name = esc(f.get("name", "?"))

    folder_info = ""
    if is_folder and listing is not None:
        children = [c for c in listing if c.get("id") != item_id]
        n_items = len(children)
        total_bytes = sum(int(c.get("size") or 0) for c in children)
        n_dirs = sum(1 for c in children if c.get("mimeType") == FOLDER_MIME)
        bits = [f"{n_items} item{'s' if n_items != 1 else ''}"]
        if n_dirs:
            bits.append(f"{n_dirs} folder{'s' if n_dirs != 1 else ''}")
        bits.append(fmtsz(total_bytes))
        folder_info = " • ".join(bits)

    rows = [[InlineKeyboardButton("🔗 Get link", callback_data=f"drvlink:{item_id}")]]
    if not is_folder and f.get("size"):
        rows.insert(0, [InlineKeyboardButton("📥 Send to Telegram", callback_data=f"drvsnd:{item_id}")])
    rows.append([
        InlineKeyboardButton("✏️ Rename", callback_data=f"drvren:{item_id}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"drvdel:{item_id}"),
    ])
    if is_folder:
        # browse straight into this folder from the detail card
        rows.append([InlineKeyboardButton("📂 Open folder", callback_data=f"drvgointo:{item_id}"),
                     InlineKeyboardButton("⬅️ Back", callback_data="drvback")])
    else:
        rows.append([InlineKeyboardButton("🔗 Open in Drive", url=f.get("webViewLink", "https://drive.google.com")),
                     InlineKeyboardButton("⬅️ Back", callback_data="drvback")])
    text = (f"{icon} <b>{name}</b>\n📦 {size}"
            + (f"\n📂 {folder_info}" if is_folder and folder_info else "")
            + ("\n📁 folder" if is_folder else ""))
    return text, InlineKeyboardMarkup(rows)


@app.on_callback_query(filters.regex(r"^drvgointo:"))
@guarded
def cb_drive_gointo(client, cq):
    """From the folder detail card, jump into that folder in the browser.
    Answers the callback FIRST: the listing can take seconds under load
    (Drive lock contention with an in-flight upload), and answering late
    hits Telegram's callback window → QUERY_ID_INVALID."""
    cq.answer()
    folder_id = cq.data.split(":", 1)[1]
    chat_id = cq.message.chat.id
    files = _get_listing(chat_id, state.get_drive_nav(chat_id)[-1][0])
    name = next((f["name"] for f in files if f["id"] == folder_id), folder_id)
    state.push_drive_nav(chat_id, folder_id, name)
    _show(chat_id, cq.message.id, 0)


@app.on_callback_query(filters.regex(r"^drvopen:"))
@guarded
def cb_drive_open(client, cq):
    item_id = cq.data.split(":", 1)[1]
    f = drive.get_file(item_id, account=_acct(cq.message.chat.id))
    if not f:
        return cq.answer("Not found — it may have already been deleted.", show_alert=True)
    chat_id = cq.message.chat.id
    listing = _get_listing(chat_id, state.get_drive_nav(chat_id)[-1][0])
    text, kb = _item_view(f, item_id, listing=listing)
    safe_edit(cq, text, reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvlink:"))
@guarded
def cb_drive_link(client, cq):
    item_id = cq.data.split(":", 1)[1]
    f = drive.get_file(item_id, account=_acct(cq.message.chat.id))
    if not f:
        return cq.answer("Not found.", show_alert=True)
    try:
        # reply_to_message_id anchors into the same forum topic as the
        # callback — a bare send_message with no anchor lands in the
        # group's General topic instead of the one this button was tapped
        # in (this Pyrogram fork has no message_thread_id param).
        app.send_message(cq.message.chat.id,
                         f"🔗 <b>{esc(f.get('name', '?'))}</b>\n{f.get('webViewLink', '')}",
                         reply_to_message_id=cq.message.id,
                         disable_web_page_preview=False)
        cq.answer("Link sent below ✓")
    except Exception as e:
        logger.warning(f"drvlink failed: {e}")
        cq.answer("❌ Couldn't fetch the link.", show_alert=True)


@app.on_callback_query(filters.regex(r"^drvsnd:"))
@guarded
def cb_drive_send(client, cq):
    """Download the Drive item locally, then upload into this chat."""
    chat_id = cq.message.chat.id
    item_id = cq.data.split(":", 1)[1]
    f = drive.get_file(item_id, account=_acct(cq.message.chat.id))
    if not f or FOLDER_MIME == f.get("mimeType"):
        return cq.answer("Folders can't be sent directly — open them instead.", show_alert=True)
    cq.answer("Downloading…")
    task_id = new_task_id()
    status = cq.message.reply_text(
        f"📥 Fetching from Drive…\n<code>{esc(f.get('name', 'file'))}</code>",
        reply_markup=cancel_kb(task_id))

    def run():
        import shutil as _sh
        task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        try:
            # Drive names may contain slashes — confine to a bare
            # basename or one hostile name escapes the per-job dir.
            dest = os.path.join(task_dir, _safe_drive_name(f.get("name")) or f"{task_id}.bin")
            # use the RETURNED path: Google-native docs (Sheets etc.) get an
            # extension appended during export — os.path.getsize(dest) on the
            # original name raised FileNotFoundError for exactly those files.
            dest = drive.download_file_content(item_id, dest, task_id=task_id)
            size = os.path.getsize(dest)
            throttled_edit(client, chat_id, status.id,
                           f"☁️ Uploading… (<code>{fmtsz(size)}</code>)",
                           markup=cancel_kb(task_id), force=True)
            uploader.upload_to_telegram(client, chat_id, dest, status.id,
                                        reply_to=file_anchor(cq.message), task_id=task_id)
            throttled_edit(client, chat_id, status.id,
                           f"✅ Sent — <code>{fmtsz(size)}</code>", force=True)
        except state.CancelledError:
            throttled_edit(client, chat_id, status.id, "🛑 Cancelled.", force=True)
        except Exception as e:
            logger.exception("drvsnd job failed")
            throttled_edit(client, chat_id, status.id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
        finally:
            state.drop_job(task_id)
            _sh.rmtree(os.path.join(config.DOWNLOAD_DIR, task_id), ignore_errors=True)

    state.register_job(task_id, chat_id, status.id, "drive-send", os.path.join(config.DOWNLOAD_DIR, task_id))
    threading.Thread(target=run, daemon=True).start()


@app.on_callback_query(filters.regex(r"^drvback$"))
@guarded
def cb_drive_back(client, cq):
    _show(cq.message.chat.id, cq.message.id, 0, cq=cq)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvdel:"))
@guarded
def cb_drive_delete_confirm(client, cq):
    item_id = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"drvdelok:{item_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="drvback"),
    ]])
    safe_edit(cq, "🗑 Delete this from Drive? This can't be undone.", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvdelok:"))
@guarded
def cb_drive_delete(client, cq):
    item_id = cq.data.split(":", 1)[1]
    try:
        ok = drive.delete_file(item_id, account=_acct(cq.message.chat.id))
    except Exception as e:
        logger.warning(f"drive delete failed for {item_id}: {e}")
        ok = False
    cq.answer("🗑 Deleted." if ok else "❌ Failed to delete.", show_alert=not ok)
    state.invalidate_listing(cq.message.chat.id)
    _show(cq.message.chat.id, cq.message.id, 0, cq=cq)


@app.on_callback_query(filters.regex(r"^drvren:"))
@guarded
def cb_drive_rename_prompt(client, cq):
    item_id = cq.data.split(":", 1)[1]
    state.set_awaiting_input(cq.message.chat.id, ("drive_rename", _acct(cq.message.chat.id)), item_id)
    safe_edit(cq, "✏️ Reply with the new name (or wait 2 minutes to cancel).")
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvnewfolder$"))
@guarded
def cb_drive_new_folder_prompt(client, cq):
    folder_id = state.get_drive_nav(cq.message.chat.id)[-1][0]
    state.set_awaiting_input(cq.message.chat.id, "drive_newfolder", folder_id)
    safe_edit(cq, "📁 Reply with the new folder's name (or wait 2 minutes to cancel).")
    cq.answer()


def handle_drive_rename(client, message, acct, file_id):
    """Called from handlers_core.auto_leech when a chat has a pending
    drive-rename waiting on the next text message."""
    new_name = (message.text or "").strip()
    if not new_name:
        return message.reply_text("⚠️ Empty name, rename cancelled.")
    try:
        ok = drive.rename_file(file_id, new_name, account=acct) if drive.get_service(acct) else False
    except Exception as e:
        logger.warning(f"drive rename failed for {file_id}: {e}")
        ok = False
    state.invalidate_listing(message.chat.id)
    if ok:
        message.reply_text(f"✅ Renamed to <code>{esc(new_name)}</code>.")
    else:
        message.reply_text("❌ Rename failed — the file may have been deleted.")


def handle_drive_new_folder(client, message, parent_id):
    """Called from handlers_core.auto_leech for a pending 'new folder' prompt."""
    name = (message.text or "").strip()
    if not name:
        return message.reply_text("⚠️ Empty name, folder not created.")
    acct = _acct(message.chat.id)
    try:
        fid = drive.create_folder(name, parent_id, account=acct) if drive.get_service(acct) else None
    except Exception as e:
        logger.warning(f"drive create_folder failed: {e}")
        fid = None
    state.invalidate_listing(message.chat.id)
    if fid:
        message.reply_text(f"✅ Created folder <code>{esc(name)}</code>. Use /drive to see it.")
    else:
        message.reply_text("❌ Couldn't create the folder.")


# ── "Quick actions" on a mirror-completion message ──────────────────────

@app.on_callback_query(filters.regex(r"^qdel:"))
@guarded
def cb_quick_delete_confirm(client, cq):
    file_id = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"qdelok:{file_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="qcancel"),
    ]])
    safe_edit(cq, "🗑 Delete this file from Drive? This can't be undone.", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^qdelok:"))
@guarded
def cb_quick_delete(client, cq):
    file_id = cq.data.split(":", 1)[1]
    try:
        ok = drive.delete_file(file_id, account=_acct(cq.message.chat.id))
    except Exception as e:
        logger.warning(f"quick delete failed for {file_id}: {e}")
        ok = False
    state.invalidate_listing(cq.message.chat.id)
    safe_edit(cq, "🗑 Deleted." if ok else "❌ Failed to delete — it may already be gone.")
    cq.answer()


@app.on_callback_query(filters.regex(r"^qlink:"))
@guarded
def cb_quick_link(client, cq):
    """Get-link button on a completed mirror message."""
    file_id = cq.data.split(":", 1)[1]
    f = drive.get_file(file_id, account=_acct(cq.message.chat.id))
    if not f:
        return cq.answer("File gone.", show_alert=True)
    try:
        app.send_message(cq.message.chat.id,
                         f"🔗 <b>{esc(f.get('name', '?'))}</b>\n{f.get('webViewLink', '')}",
                         reply_to_message_id=cq.message.id,
                         disable_web_page_preview=False)
        cq.answer("Link sent below ✓")
    except Exception:
        cq.answer("❌ Couldn't fetch the link.", show_alert=True)


@app.on_callback_query(filters.regex(r"^qsnd:"))
@guarded
def cb_quick_send(client, cq):
    """Send-to-Telegram button on a completed mirror message."""
    chat_id = cq.message.chat.id
    file_id = cq.data.split(":", 1)[1]
    f = drive.get_file(file_id, account=_acct(cq.message.chat.id))
    if not f or f.get("mimeType") == FOLDER_MIME:
        return cq.answer("Can't send that type.", show_alert=True)
    cq.answer("Fetching…")
    task_id = new_task_id()
    status = cq.message.reply_text("📥 Fetching from Drive…", reply_markup=cancel_kb(task_id))

    def run():
        import shutil as _sh
        task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        try:
            dest = os.path.join(task_dir, _safe_drive_name(f.get("name")) or f"{task_id}.bin")
            dest = drive.download_file_content(file_id, dest, task_id=task_id)
            throttled_edit(client, chat_id, status.id,
                           f"☁️ Uploading… (<code>{fmtsz(os.path.getsize(dest))}</code>)",
                           markup=cancel_kb(task_id), force=True)
            # Anchor to the topic root (file_anchor): the status bubble is a
            # reply, but files land as plain posts in the topic — no
            # reply-to-the-bot's-own-status header on delivered files.
            uploader.upload_to_telegram(client, chat_id, dest, status.id,
                                        reply_to=file_anchor(cq.message), task_id=task_id)
            throttled_edit(client, chat_id, status.id,
                           f"✅ Sent — <code>{fmtsz(os.path.getsize(dest))}</code>", force=True)
        except state.CancelledError:
            throttled_edit(client, chat_id, status.id, "🛑 Cancelled.", force=True)
        except Exception as e:
            logger.exception("qsnd job failed")
            throttled_edit(client, chat_id, status.id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
        finally:
            state.drop_job(task_id)
            _sh.rmtree(task_dir, ignore_errors=True)

    state.register_job(task_id, chat_id, status.id, "drive-send",
                       os.path.join(config.DOWNLOAD_DIR, task_id))   # v15 bug: bare `task_dir` → NameError on every tap
    threading.Thread(target=run, daemon=True).start()


@app.on_callback_query(filters.regex(r"^qren:"))
@guarded
def cb_quick_rename_prompt(client, cq):
    file_id = cq.data.split(":", 1)[1]
    state.set_awaiting_input(cq.message.chat.id, ("drive_rename", _acct(cq.message.chat.id)), file_id)
    safe_edit(cq, "✏️ Reply with the new name (or wait 2 minutes to cancel).")
    cq.answer()


@app.on_callback_query(filters.regex(r"^qcancel$"))
@guarded
def cb_quick_cancel(client, cq):
    safe_edit(cq, "Cancelled.")
    cq.answer()
