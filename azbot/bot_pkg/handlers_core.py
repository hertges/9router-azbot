import os, re, shutil, time
from urllib.parse import urlparse

from pyrogram import filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

from . import config, state, engine, uploader, drive, log
from .core import app
from .utils import (new_task_id, throttled_edit, fmtsz, fix_unknown_ext, cancel_kb, bar,
                    smooth_speed, autoclean_if_enabled, guarded, safe_edit, esc, fmt_time,
                    file_anchor)
from .dispatcher import LiveDispatcher

logger = log.get(__name__)

URL_RE = config.URL_RE


def _auth_gate(_, __, message: Message):
    return bool(message.from_user) and state.is_authorized(message.from_user.id)

Authorized = filters.create(_auth_gate)


def _admin_gate(_, __, message: Message):
    return bool(message.from_user) and str(message.from_user.id) == config.ADMIN_ID

AdminOnly = filters.create(_admin_gate)


def _not_command(_, __, message: Message):
    return not (message.text or "").startswith("/")

NotCommand = filters.create(_not_command)


def _host_of(url):
    try:
        h = urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


# ── Basic commands ────────────────────────────────────────────────────

@app.on_message(filters.command("start") & filters.private)
@guarded
def cmd_start(client, message):
    render_help(message.chat.id)


@app.on_message(filters.command("help"))
@guarded
def cmd_help(client, message):
    # /help (unlike /start) isn't private-only, so it can be typed inside a
    # forum-group topic. Thread the reply to message.id — WZML-X-style
    # anchoring — so it lands in the same topic instead of the group's
    # General topic (this Pyrogram fork has no message_thread_id param, so
    # replying to something already in the topic is the only way to land
    # there).
    reply_to = message.id if message.chat.type != enums.ChatType.PRIVATE else None
    render_help(message.chat.id, reply_to=reply_to)


def render_help(chat_id, msg_id=None, reply_to=None):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Downloads", callback_data="help:dl"),
         InlineKeyboardButton("🗃 Zip", callback_data="help:zip")],
        [InlineKeyboardButton("📷 Instagram", callback_data="help:ig"),
         InlineKeyboardButton("ℹ️ Info", callback_data="help:misc")],
        [InlineKeyboardButton("🍪 Cookies", callback_data="help:cookies"),
         InlineKeyboardButton("🔧 Admin", callback_data="help:admin")],
    ])
    text = (
        "👋 <b>AzLeechBot</b>\n\n"
        "Paste a link — YouTube, Instagram, TikTok, Twitter/X, Reddit, a "
        "magnet link, hundreds of other sites — and I'll <b>leech</b> it "
        "(send it back here) automatically. No command needed for that.\n\n"
        "<code>/m &lt;link&gt;</code> — <b>mirror</b> to Drive\n"
        "<code>/l &lt;link&gt;</code> — <b>leech</b> to Telegram (same as pasting)\n"
        "<code>/zm</code> / <code>/zl</code> — same, zipped first\n\n"
        "<i>All commands accept multiple links, <code>| newname</code>, and "
        "<code>#folder</code> for Drive uploads.</i>\n\n"
        "Uploads go over MTProto directly (Pyrogram) — files up to Telegram's "
        "~2GB per-file cap go through natively.\n\n"
        "Tap a section below for the full command list."
    )
    if msg_id:
        safe_edit(app, chat_id, msg_id, text, reply_markup=kb)
    else:
        app.send_message(chat_id, text, reply_markup=kb, reply_to_message_id=reply_to)


HELP = {
    "dl": ("⬇️ <b>Downloads</b>\n\n"
           "Paste a link — auto-<b>leeched</b> to Telegram, no command needed.\n\n"
           "<b>Every download command supports:</b>\n"
           "• multiple links → one job per link (batch)\n"
           "• <code>| name</code> after a link → saved as that name (extension untouched)\n"
           "• <code>#folder</code> → Drive uploads go into that folder (auto-created)\n\n"
           "<code>/m &lt;link&gt; | name #folder</code> — mirror to Drive\n"
           "<code>/l &lt;link&gt;</code> — leech to Telegram\n"
           "<code>/zm</code> / <code>/zl</code> — same, zipped into one file first\n"
           "<code>/yt &lt;link&gt;</code>, <code>/y</code>/<code>/ytdl</code>, "
           "<code>/yl</code>/<code>/ytdlleech</code> — WZML-X-style aliases (quality picker)\n\n"
           "<code>/torrent &lt;magnet or .torrent url&gt;</code> — force aria2c\n"
           "<code>/gallery &lt;url&gt;</code> / <code>/g</code> — gallery-dl (files to chat)\n"
           "<code>/gm</code> / <code>/gallerym</code> — gallery-dl, files to Drive (<code>#folder</code> works)\n"
           "<code>/galleryz</code> — one zip of everything → chat\n"
           "<code>/galleryzm</code> — one zip → Drive\n"
           "<code>/clone &lt;url&gt;</code> / <code>/clonem</code> — wget mirror to chat / Drive\n"
           "<i>Raw binary flags after</i> <code>--</code> <i>pass straight through:</i> "
           "<code>/gallery &lt;url&gt; -- --range 1-5 --verbose</code>\n"
           "<code>/zipl</code> · <code>/zipm</code> · <code>/unzip</code> · "
           "<code>/unzipm</code> · <code>/unzipmulti</code> — see the 🗃 Zip section\n\n"
           "<code>/drive</code> — browse Drive: get links, send files here, rename/delete, new folders • <code>/drivesearch &lt;q&gt;</code>\n\n"
           "Magnet links are auto-detected in plain messages too. Reply to any file or link instead of typing a URL."),
    "ig": ("📷 <b>Instagram</b>\n\n"
           "<code>&lt;post/reel url&gt;</code> — just paste it, leeched like any other link "
           "(always via gallery-dl — never the yt-dlp picker)\n"
           "<code>/ig &lt;profile_url&gt;</code> — multi-select picker: Posts/Reels/Stories/"
           "Highlights/Tagged, then ▶️ Start (Stories on by default). Leeches to Telegram.\n"
           "<code>/igm &lt;profile&gt;</code> — same, but archives go to Drive sorted into "
           "<code>Instagram/&lt;user&gt;/&lt;type&gt;/</code> folders\n"
           "<code>/igl &lt;profile&gt;</code> — same as /ig (explicit leech)\n\n"
           "<b>Indexes</b> (what's already fetched) are stored per user/type — mirrored to Drive.\n"
           "<code>/igindex</code> — view + delete indexes per user or per type. "
           "Deleting an index makes the next run re-fetch everything.\n\n"
           "Stories &amp; Highlights need a logged-in cookie profile (<code>/cookie</code>)."),
    "misc": ("ℹ️ <b>Good to know</b>\n\n"
             "🖼 <b>Thumbnails</b>: fully automatic — every video gets its real "
             "dimensions/duration shown, and the thumbnail is the source's own "
             "cover (YouTube etc.) or a frame grabbed from the file itself. "
             "No setup needed.\n\n"
             "♻️ <b>Duplicate guard</b>: sending the exact same link while it's still "
             "downloading is rejected instead of downloading it twice."),
    "zip": ("🗃 <b>Zip leech / mirror / unpack</b>\n\n"
            "<code>/zipl</code> — gather files into ONE archive, sent to this chat:\n"
            "• reply to a file → zip it\n"
            "• pass direct links: <code>/zipl link1 link2 | myname.zip</code>\n"
            "<code>/zipm</code> — same but the archive goes to Drive (<code>#folder</code> works)\n\n"
            "<code>/unzip</code> / <code>/unzipl</code> — reply to an archive "
            "(<code>.zip .rar .7z .tar…</code>) or pass its link: every inner file is "
            "sent to this chat. If the reply is already a .zip it's sent as-is (no double-zip).\n"
            "<code>/unzipm</code> — unpack and MIRROR: every inner file goes to Drive "
            "(<code>#folder</code> works), links collected in the summary.\n\n"
            "<code>/unzipmulti</code> — MULTIPART session: send the parts one by one "
            "(<code>.001</code>/<code>.part1.rar</code>/…), each is collected with a live "
            "counter, then ✅ <b>Done</b> extracts and ✖ <b>Cancel</b> aborts. "
            "<code>#folder</code> works for Drive.\n\n"
            "All support batch links + <code>| name</code>. "
            "gallery-dl equivalents: <code>/galleryz</code> (zip → chat), "
            "<code>/galleryzm</code> (zip → Drive)."),
    "cookies": ("🍪 <b>Cookies</b>\n\n"
                "<code>/cookie</code> — list profiles\n<code>/cookie &lt;name&gt;</code> — switch active profile\n"
                "Reply to a Netscape-format <code>cookies.txt</code> with <code>/cookie &lt;name&gt;</code> to add one.\n\n"
                "Needed for private Instagram accounts, stories/highlights, age-gated videos."),
    "admin": ("🔧 <b>Admin</b>\n\n"
              "<code>/stats</code> — disk, RAM, active jobs\n<code>/clean</code> — purge stale temp files\n"
              "<code>/cancel &lt;id&gt;</code> / 🛑 button — stop a job\n<code>/cancelall</code> — stop everything\n"
              "<code>/sh</code> — live persistent shell terminal (arrow keys, history, Ctrl+C button; <code>/shexit</code> quits)\n"
              "<code>/allow &lt;id&gt;</code> / <code>/ban &lt;id&gt;</code> — access control"),
}


@app.on_callback_query(filters.regex(r"^help:"))
@guarded
def cb_help(client, cq):
    section = cq.data.split(":", 1)[1]
    cq.answer()
    if section == "back":
        return render_help(cq.message.chat.id, cq.message.id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="help:back")]])
    safe_edit(app, cq.message.chat.id, cq.message.id, HELP.get(section, "?"), reply_markup=kb)


@app.on_message(filters.command("cookie") & Authorized)
@guarded
def cmd_cookie(client, message):
    parts = (message.text or message.caption or "").split(maxsplit=1)
    doc_msg = message if message.document else (message.reply_to_message if (message.reply_to_message and message.reply_to_message.document) else None)
    if doc_msg:
        name = parts[1].strip() if len(parts) > 1 else "global"
        path = state.cookie_path(name)
        client.download_media(doc_msg, file_name=path)
        state.set_pref(message.chat.id, "active_cookie", name)
        state.mirror_cookie(name)   # persist to Drive datastore
        logger.info(f"chat {message.chat.id} saved cookie profile '{name}'")
        return message.reply_text(
            f"🍪 Saved cookie profile <code>{esc(name)}</code> and made it active.\n"
            f"☁️ Mirrored to Drive: <code>AzBotData/cookies/{esc(os.path.basename(path))}</code>")
    if len(parts) > 1:
        name = parts[1].strip()
        if os.path.exists(state.cookie_path(name)):
            state.set_pref(message.chat.id, "active_cookie", name)
            return message.reply_text(f"🍪 Switched to cookie profile <code>{esc(name)}</code>.")
        return message.reply_text(
            f"No cookie profile named <code>{esc(name)}</code>. Reply to a cookies.txt file with "
            f"<code>/cookie {esc(name)}</code> to create it.")
    if not state.list_cookie_profiles():
        return message.reply_text(
            "🍪 No cookie profiles yet.\n\n"
            "Send (or reply to) a Netscape-format <code>cookies.txt</code> with <code>/cookie &lt;name&gt;</code> "
            "to add one — needed for private Instagram accounts, stories/highlights, and age-gated videos."
        )
    message.reply_text(_cookie_list_text(message.chat.id), reply_markup=_cookie_list_kb(message.chat.id))


def _cookie_list_text(chat_id):
    active = state.get_prefs(chat_id).get("active_cookie", "global")
    return f"🍪 <b>Cookie profiles</b> — active: <code>{esc(active)}</code>\n\nTap a name to switch, ⚙️ to rename/delete."

def _cookie_list_kb(chat_id):
    active = state.get_prefs(chat_id).get("active_cookie", "global")
    profiles = state.list_cookie_profiles()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if p == active else "") + p, callback_data=f"ck:{p}"),
         InlineKeyboardButton("⚙️", callback_data=f"ckmgr:{p}")]
        for p in profiles
    ])


@app.on_callback_query(filters.regex(r"^ck:"))
@guarded
def cb_switch_cookie(client, cq):
    name = cq.data.split(":", 1)[1]
    state.set_pref(cq.message.chat.id, "active_cookie", name)
    logger.info(f"chat {cq.message.chat.id} switched to cookie profile '{name}' via button")
    cq.answer(f"Switched to {name}")
    safe_edit(cq, _cookie_list_text(cq.message.chat.id), reply_markup=_cookie_list_kb(cq.message.chat.id))


@app.on_callback_query(filters.regex(r"^ckback$"))
@guarded
def cb_cookie_back(client, cq):
    safe_edit(cq, _cookie_list_text(cq.message.chat.id), reply_markup=_cookie_list_kb(cq.message.chat.id))
    cq.answer()


@app.on_callback_query(filters.regex(r"^ckmgr:"))
@guarded
def cb_cookie_manage(client, cq):
    name = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Use this profile", callback_data=f"ck:{name}")],
        [InlineKeyboardButton("✏️ Rename", callback_data=f"ckren:{name}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"ckdel:{name}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="ckback")],
    ])
    safe_edit(cq, f"🍪 Managing profile <code>{esc(name)}</code>", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^ckdel:"))
@guarded
def cb_cookie_delete_confirm(client, cq):
    name = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"ckdelok:{name}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"ckmgr:{name}"),
    ]])
    safe_edit(cq, f"🗑 Delete cookie profile <code>{esc(name)}</code>? This can't be undone.", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^ckdelok:"))
@guarded
def cb_cookie_delete(client, cq):
    name = cq.data.split(":", 1)[1]
    ok = state.delete_cookie_profile(cq.message.chat.id, name)
    cq.answer("🗑 Deleted." if ok else "❌ Not found.", show_alert=not ok)
    if not state.list_cookie_profiles():
        return safe_edit(cq, "🍪 No cookie profiles left. Send a <code>cookies.txt</code> with <code>/cookie &lt;name&gt;</code> to add one.")
    safe_edit(cq, _cookie_list_text(cq.message.chat.id), reply_markup=_cookie_list_kb(cq.message.chat.id))


@app.on_callback_query(filters.regex(r"^ckren:"))
@guarded
def cb_cookie_rename_prompt(client, cq):
    name = cq.data.split(":", 1)[1]
    state.set_awaiting_input(cq.message.chat.id, "cookie_rename", name)
    safe_edit(cq, f"✏️ Reply with the new name for cookie profile <code>{esc(name)}</code> (or wait 2 minutes to cancel).")
    cq.answer()


def handle_cookie_rename(client, message, old_name):
    """Called from auto_leech when a chat has a pending cookie-rename
    waiting on the next text message."""
    new_name = (message.text or "").strip()
    if not new_name:
        return message.reply_text("⚠️ Empty name, rename cancelled.")
    ok, err = state.rename_cookie_profile(message.chat.id, old_name, new_name)
    if ok:
        message.reply_text(f"✅ Renamed <code>{esc(old_name)}</code> → <code>{esc(new_name)}</code>.")
    else:
        message.reply_text(f"❌ {esc(err)}")


# ── /settings — per-chat toggles ─────────────────────────────────────────
# Destination (mirror/leech) is chosen per-command, not stored here — see
# /m, /l. These are the settings that ARE persistent per chat.

def _settings_kb(chat_id):
    clean_on = state.get_autoclean(chat_id)
    doc_on = state.get_as_document(chat_id)
    media_on = state.get_media_group(chat_id)
    rows = [
        [InlineKeyboardButton(
            "🧹 Auto-clean: " + ("✅ ON" if clean_on else "⬜ OFF"),
            callback_data="toggle_autoclean")],
        [InlineKeyboardButton(
            "📄 Send as document: " + ("✅ ON" if doc_on else "⬜ OFF"),
            callback_data="toggle_as_document")],
        [InlineKeyboardButton(
            "🖼 Albums: " + ("✅ ON" if media_on else "⬜ OFF"),
            callback_data="toggle_albums")],
        [InlineKeyboardButton(f"☁️ Drive ({len(config.DRIVE_ACCOUNTS)} account"
                              f"{'s' if len(config.DRIVE_ACCOUNTS) != 1 else ''}) ▸",
                              callback_data="acctmenu")],
    ]
    return InlineKeyboardMarkup(rows)

def _settings_text(chat_id):
    clean_on = state.get_autoclean(chat_id)
    doc_on = state.get_as_document(chat_id)
    media_on = state.get_media_group(chat_id)
    clean_line = (
        "🧹 <b>Auto-clean</b>: on — a finished job deletes your link/command "
        "message and my status message, leaving only the delivered file "
        "(or the Drive link, if nothing went to Telegram)."
        if clean_on else
        "🧹 <b>Auto-clean</b>: off — command and status messages stay in the chat."
    )
    doc_line = (
        "📄 <b>As document</b>: on — everything uploads as a plain file "
        "instead of a video/audio/photo, skipping Telegram's re-encoding "
        "and thumbnail generation (keeps exact quality, no inline player)."
        if doc_on else
        "📄 <b>As document</b>: off — videos/audio/photos upload as their "
        "native type with previews and streaming."
    )
    acct = state.get_drive_account(chat_id) or "—"
    album_line = (
        "🖼 <b>Albums</b>: on — photos go out in groups of up to 10 "
        "(one send instead of ten)."
        if media_on else
        "🖼 <b>Albums</b>: off — every photo uploads individually."
    )
    extra = (f"\n\n☁️ <b>Drive</b>: {len(config.DRIVE_ACCOUNTS)} account(s), "
             f"this chat uploads to <code>{esc(acct)}</code>.")
    return f"⚙️ <b>Settings</b> <i>(this chat only)</i>\n\n{clean_line}\n\n{doc_line}\n\n{album_line}{extra}"

@app.on_message(filters.command("settings") & Authorized)
@guarded
def cmd_settings(client, message):
    message.reply_text(_settings_text(message.chat.id), reply_markup=_settings_kb(message.chat.id))

def _render_drive_accounts(cq):
    """Sync body of the ☁️ Drive management screen — shared by the button
    handler AND by add/delete/switch flows that re-render after acting
    (handlers must never call each other directly now that guarded()
    wraps them in coroutines)."""
    chat_id = cq.message.chat.id
    current = state.get_drive_account(chat_id)
    lines = []
    rows = []
    for name in sorted(config.DRIVE_ACCOUNTS):
        mark = "✅ " if name == current else ""
        lines.append(f"{mark}<code>{esc(name)}</code>")
        row = [InlineKeyboardButton(("🟢 " if name == current else "") + name,
                                     callback_data=f"acctset:{name}")]
        # only Telegram-registered accounts can be removed here
        if name in _tg_registered_accounts():
            row.append(InlineKeyboardButton("🗑", callback_data=f"acctdel:{name}"))
        rows.append(row)
    text = ("☁️ <b>Drive accounts</b>\n\n"
            + ("\n".join(lines) if lines else "_none configured_")
            + f"\n\nThis chat uploads to: <b>{esc(current or '—')}</b>\n\n"
              "➕ adds a new Google account via a sign-in code — no console needed. "
              "🗑 removes an account added through Telegram (.env accounts are managed in .env).")
    rows.append([InlineKeyboardButton("➕ Add account", callback_data="acctadd")])
    rows.append([InlineKeyboardButton("⬅️ Back to settings", callback_data="settingsback")])
    safe_edit(cq, text, reply_markup=InlineKeyboardMarkup(rows))
    cq.answer()


@app.on_callback_query(filters.regex(r"^acctmenu$"))
@guarded
def cb_acct_menu(client, cq):
    """☁️ Drive management section: list accounts, add via OAuth device
    flow, remove Telegram-registered ones, pick the active one."""
    _render_drive_accounts(cq)


def _tg_registered_accounts():
    """Account names whose tokens came from Telegram /drivelogin."""
    try:
        from . import handlers_driveauth
        data = state._load_json(handlers_driveauth.OVERRIDES_FILE, {})
        return {k[len("GDrive_"):-len("_REFRESH_TOKEN")]
                for k in data if k.startswith("GDrive_") and k.endswith("_REFRESH_TOKEN")}
    except Exception:
        return set()


@app.on_callback_query(filters.regex(r"^acctadd$"))
@guarded
def cb_acct_add(client, cq):
    state.set_awaiting_input(cq.message.chat.id, "drive_acct_name", None)
    safe_edit(cq, "✏️ Reply with a <b>name</b> for the new Drive account "
                  "(letters/digits/-/_, e.g. <code>work</code> or <code>backup2</code>). "
                  "Send /cancel to abort.")
    cq.answer()


@app.on_callback_query(filters.regex(r"^acctdel:"))
@guarded
def cb_acct_del(client, cq):
    name = cq.data.split(":", 1)[1]
    if name not in _tg_registered_accounts():
        return cq.answer(".env-defined accounts can't be removed here.", show_alert=True)
    try:
        from . import handlers_driveauth
        handlers_driveauth.remove_account(name)
        cq.answer(f"🗑 {name} removed.")
        _render_drive_accounts(cq)   # re-render (handlers can't call handlers anymore)
    except Exception as e:
        cq.answer(f"❌ {e}", show_alert=True)


@app.on_callback_query(filters.regex(r"^acctset:"))
@guarded
def cb_acct_set(client, cq):
    name = cq.data.split(":", 1)[1]
    ok = state.set_drive_account(cq.message.chat.id, name)
    cq.answer(f"Switched to {name}" if ok else "Unknown account.", show_alert=not ok)
    if ok:
        _render_drive_accounts(cq)   # re-render with new selection

@app.on_callback_query(filters.regex(r"^settingsback$"))
@guarded
def cb_settings_back(client, cq):
    chat_id = cq.message.chat.id
    safe_edit(cq, _settings_text(chat_id), reply_markup=_settings_kb(chat_id))
    cq.answer()

@app.on_callback_query(filters.regex(r"^acctback$"))
@guarded
def cb_acct_back(client, cq):
    chat_id = cq.message.chat.id
    safe_edit(cq, _settings_text(chat_id), reply_markup=_settings_kb(chat_id))
    cq.answer()

@app.on_callback_query(filters.regex(r"^toggle_autoclean$"))
@guarded
def cb_toggle_autoclean(client, cq):
    chat_id = cq.message.chat.id
    new_val = not state.get_autoclean(chat_id)
    state.set_autoclean(chat_id, new_val)
    logger.info(f"chat {chat_id} auto-clean set to {new_val}")
    cq.answer("Auto-clean " + ("enabled" if new_val else "disabled"))
    safe_edit(cq, _settings_text(chat_id), reply_markup=_settings_kb(chat_id))

@app.on_callback_query(filters.regex(r"^toggle_as_document$"))
@guarded
def cb_toggle_as_document(client, cq):
    chat_id = cq.message.chat.id
    new_val = not state.get_as_document(chat_id)
    state.set_as_document(chat_id, new_val)
    logger.info(f"chat {chat_id} as_document set to {new_val}")
    cq.answer("Send as document " + ("enabled" if new_val else "disabled"))
    safe_edit(cq, _settings_text(chat_id), reply_markup=_settings_kb(chat_id))


@app.on_callback_query(filters.regex(r"^toggle_albums$"))
@guarded
def cb_toggle_albums(client, cq):
    chat_id = cq.message.chat.id
    new_val = not state.get_media_group(chat_id)
    state.set_media_group(chat_id, new_val)
    logger.info(f"chat {chat_id} media_group set to {new_val}")
    cq.answer("Albums " + ("enabled" if new_val else "disabled"))
    safe_edit(cq, _settings_text(chat_id), reply_markup=_settings_kb(chat_id))


@app.on_message(filters.command(["allow", "ban"]) & AdminOnly)
@guarded
def cmd_access(client, message):
    if message.chat.type.name != "PRIVATE":
        return message.reply_text("⚠️ `/allow` and `/ban` only work in a private chat with me.")
    parts = message.text.split()
    if len(parts) != 2:
        return message.reply_text("Usage: <code>/allow &lt;user_id&gt;</code> or <code>/ban &lt;user_id&gt;</code>")
    if parts[0] == "/allow":
        state.allow_user(parts[1]); message.reply_text(f"✅ <code>{esc(parts[1])}</code> authorized.")
    else:
        state.ban_user(parts[1]); message.reply_text(f"🚫 <code>{esc(parts[1])}</code> banned.")


@app.on_callback_query(filters.regex(r"^cancel:"))
@guarded
def cb_cancel(client, cq):
    task_id = cq.data.split(":", 1)[1]
    ok = state.cancel_task(task_id)
    cq.answer("Stopping…" if ok else "Already finished.")
    if ok:
        # Flip the button row so the tap visibly did something (and can't
        # be tapped twice) — v15/v16 left the old 🛑 in place, so a slow
        # job made it look like the button was dead.
        try:
            cq.message.edit_text("🛑 Stop requested — waiting for the job to halt…",
                                 reply_markup=None)
        except Exception:
            pass


# ── Universal mirror/leech dispatcher: /m, /l, /zm, /zl ─────────────────
# Mirrors the original bot's design — one pair of commands that sniff the
# link type and route it to the right downloader, with the command itself
# (m=Drive vs l=Telegram) choosing the destination.
#
# Aliases: "yt" is ours (leech, like "l" — documented in /help already).
# "y"/"ytdl" (mirror) and "yl"/"ytdlleech" (leech) are WZML-X's actual
# command names for the same thing, added to match it — note "y" mirrors
# while "yt" leeches, which is worth remembering since they look similar.

YT_DEDICATED_CMDS = ("yt", "y", "ytdl", "ytdlleech", "yl", "ytm", "ytl")
IG_DEDICATED_CMDS = ("igm", "igl")

@app.on_message(filters.command(["m", "l", "zm", "zl", "yt", "y", "ytdl", "ytdlleech",
                                 "yl", "ym", "ytm", "ytl", "torrent", "igm", "igl"]) & Authorized)
@guarded
def cmd_mirror_leech(client, message):
    """Universal mirror/leech entry. Payload grammar (all commands):
        /m <link> [<link2> …]          batch — one job per link
        /m <link> | myname             rename — saved as `myname`, extension untouched
        /m <link> #folder              Drive: upload into folder `folder` (auto-created)
    Reply mode still works: reply to a file/link with no args.

    /igm <profile> — Instagram archive → Drive (per-type subfolders)
    /igl <profile> — Instagram archive → Telegram"""
    from .utils import parse_payload, split_ytdlp_format
    cmd = message.command[0]
    dest = "drive" if cmd in ("m", "zm", "y", "ytdl", "ym") else "telegram"
    zip_output = cmd in ("zm", "zl")
    if dest == "drive" and not drive.enabled():
        return message.reply_text("☁️ Drive isn't configured on this bot (missing GCP credentials in <code>.env</code>).")

    raw = ""
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        raw = parts[1].strip()
    elif message.reply_to_message and (message.reply_to_message.text or message.reply_to_message.caption):
        raw = message.reply_to_message.text or message.reply_to_message.caption
    # raw yt-dlp format selector: trailing " -f <selector>" (their own -f
    # syntax, verbatim). Consumed here so rename/folder parsing never sees it.
    raw, ytdlp_format = split_ytdlp_format(raw)

    links, rename, folder = parse_payload(raw)

    # Instagram archive commands (/igm /igl): route to the IG picker with
    # the right destination — never through the yt-dlp quality picker.
    if cmd in IG_DEDICATED_CMDS:
        if not links:
            return message.reply_text(
                f"⚠️ Usage: <code>/{esc(cmd)} &lt;profile_or_post_url&gt;</code> [links…]")
        from .handlers_instagram import dispatch_instagram
        dest_ig = "drive" if cmd == "igm" else "telegram"
        if dest_ig == "drive" and not drive.enabled():
            return message.reply_text("☁️ Drive isn't configured — use <code>/igl</code> for Telegram delivery.")
        for u in links:
            dispatch_instagram(client, message, u, dest_ig)
        return

    if not links:
        reply = message.reply_to_message
        reply_text = (reply.text or reply.caption) if reply else None
        if reply and reply.media:
            # "/m #folder" / "/m | name" as a REPLY to a file: the args
            # (folder/rename) apply to the re-uploaded media — they used to
            # be silently dropped here, so #folder mirrored to Drive root.
            return queue_telegram_media_job(client, message, reply, dest, zip_output=zip_output,
                                             rename=rename, folder=folder)
        if reply and reply_text:
            # "/m #folder" replied to a message containing LINKS: the args
            # apply to those links too
            rlinks, rrename, rfolder = parse_payload(reply_text)
            if rlinks:
                links = rlinks
                rename = rename or rrename
                folder = folder or rfolder
    if not links:
        return message.reply_text(
            f"⚠️ Usage: <code>/{esc(cmd)} &lt;link&gt;</code> [links…] [ | name] [#folder]\n"
            f"or reply to a link/file with <code>/{esc(cmd)}</code>.")

    # Batch: when 2+ links are given, ONE batch job processes them
    # sequentially with a unified progress view (Drive-folder look — header
    # tracks "2/5 done", each link's bar renders underneath). Rename only
    # applies to a single link.
    skipped_dupes = []
    if len(links) >= 2:
        kept = []
        for url in links:
            # WZML-X parity: skip a second download of the SAME url while
            # one is still active (prevents double-leeched duplicates) —
            # but only that link, not the whole batch.
            active = state.find_active_url(url)
            if active and active in state.ACTIVE_JOBS:
                skipped_dupes.append(url)
                continue
            kept.append(url)
        if not kept:
            return message.reply_text(
                "⚠️ Every link in this batch is already being downloaded. "
                "Use /cancel first if you really want them again.")
        links = kept
        task_id = new_task_id()
        tag = f" 📦→{config.DEST_LABEL[dest]}" if zip_output else f" →{config.DEST_LABEL[dest]}"
        dupe_note = (f" — skipped {len(skipped_dupes)} duplicate(s) already downloading"
                     if skipped_dupes else "")
        status = message.reply_text(
            f"🔎 Batch of {len(links)} queued{tag}{dupe_note} — <code>{esc(task_id)}</code>",
            reply_markup=cancel_kb(task_id))
        task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
        state.register_job(task_id, message.chat.id, status.id, "batch", task_dir)
        for url in links:
            state.register_url(url, task_id)
        state.task_queue.put((run_batch_leech_job,
                               (client, message.chat.id, status.id, links, task_id,
                                message.id, dest, zip_output, ytdlp_format, folder,
                                file_anchor(message))))
        return

    url = links[0]
    # WZML-X parity: reject a second download of the SAME url while one
    # is still active (prevents double-leeched duplicates).
    active = state.find_active_url(url)
    if active and active in state.ACTIVE_JOBS:
        return message.reply_text(
            f"⚠️ This exact link is already being downloaded (<code>{esc(active)}</code>). "
            f"Use /cancel {esc(active)} first if you really want it again.")
    _dispatch_one(client, message, cmd, url, dest, zip_output, rename, folder,
                  format_selector=ytdlp_format)


def _dispatch_one(client, message, cmd, url, dest, zip_output, rename=None, folder=None,
                  format_selector=None):
    if cmd == "torrent":
        # BUG: this ternary's two branches both evaluated to "telegram"
        # literally, so it was a no-op that always forced Telegram delivery
        # regardless of `dest` — currently harmless only because cmd_mirror_
        # leech() never routes an explicit "/torrent" command through the
        # drive-dest list, but it silently discarded any future "mirror
        # this torrent to Drive" support (and made this branch inconsistent
        # with the equivalent, correct magnet-link handling in
        # dispatch_link(), which passes `dest` straight through). Just pass
        # `dest` through here too — callers already validate drive.enabled()
        # before setting dest="drive" (see cmd_mirror_leech above).
        from .handlers_subproc import queue_subprocess_job
        return queue_subprocess_job(client, message, "torrent", url,
                                     dest=dest, zip_output=zip_output)

    # Instagram NEVER goes through yt-dlp's quality picker (WZML-X parity:
    # gallery-dl owns Instagram — photos, carousels and videos alike; the
    # yt-dlp extractor just errors with "No video formats found!").
    # Checked FIRST because instagram.com also sits in YTDLP_AUTO_HOSTS —
    # a later media-page check would shadow it and misroute bare pastes.
    host = _host_of(url)
    if any(host == h or host.endswith("." + h) for h in config.IG_HOSTS):
        from .handlers_instagram import dispatch_instagram
        return dispatch_instagram(client, message, url, dest)

    # Media-page links get the WZML-X-style quality picker (real resolutions
    # + sizes). Direct files/magnets skip it. An explicit -f selector skips
    # the picker entirely (the user already chose the format).
    if format_selector:
        return queue_leech_job(client, message, url, dest, zip_output,
                                rename=rename, folder=folder,
                                format_selector=format_selector)
    is_media_page = any(host == h or host.endswith("." + h) for h in config.YTDLP_AUTO_HOSTS)
    if is_media_page:
        return show_quality_picker(client, message, url, dest, zip_output,
                                    rename=rename, folder=folder)
    dispatch_link(client, message, url, dest, zip_output=zip_output,
                  rename=rename, folder=folder, format_selector=format_selector)


def show_quality_picker(client, message, url, dest, zip_output, rename=None, folder=None):
    status = message.reply_text("🔎 Checking available qualities…")
    import concurrent.futures as _cf
    _ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        # HARD 45s bound on the probe. yt-dlp extraction can hang for
        # minutes on a hostile/throttled network — with no timeout, /m
        # stayed silent forever ("no response"). NOTE: the executor is
        # deliberately NOT used as a context manager — `with` waits for the
        # still-running probe on exit, which silently turned this 45s
        # timeout into "block the handler thread for as long as yt-dlp
        # feels like". shutdown(wait=False) leaves the doomed probe to
        # finish in the background instead of blocking this handler.
        fut = _ex.submit(engine.probe_qualities, url, message.chat.id)
        try:
            title, options, is_playlist = fut.result(timeout=45)
        except _cf.TimeoutError:
            fut.cancel()
            logger.warning(f"quality probe timed out after 45s for {url}")
            client.edit_message_text(
                message.chat.id, status.id,
                "⏱ Format check took too long (site slow or blocking me).\n"
                "Tap below to download <b>best-available</b> directly, or retry:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚡ Best-available now",
                                         callback_data=f"ytq:{state.new_quality_session(url, dest, zip_output, message.id, file_anchor(message))}:bv*+ba/b"),
                    InlineKeyboardButton("🔄 Retry check",
                                         callback_data=f"ytretry:{state.new_retry_token(url, dest, zip_output, rename, folder)}"),
                ]]))
            return
    except Exception as e:
        # Tell the user WHY there's no picker instead of silently grabbing
        # a low-quality merged format (the "360p with black thumbnail"
        # report = probe failed and auto-best ran with a degraded format
        # list). Most of these are transient extraction walls — the retry
        # button helps (and a bot restart picks up a newer yt-dlp).
        logger.warning(f"quality probe failed for {url}: {e}")
        from pyrogram.types import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
        kb = _IKM([[_IKB("🔄 Retry", callback_data=f"ytretry:{state.new_retry_token(url, dest, zip_output, rename, folder)}")]])
        client.edit_message_text(
            message.chat.id, status.id,
            f"⚠️ Couldn't read this video's formats ({esc(str(e)[:140])}).\n"
            f"You can retry, or I'll download best-available directly:",
            reply_markup=kb,
        )
        return
    finally:
        _ex.shutdown(wait=False)
    # WZML-X parity: a menu is only worth showing when there IS a choice.
    # One real option → grab it directly for SINGLE videos. PLAYLISTS
    # ALWAYS show the menu (even BEST-only): silently downloading an
    # entire playlist without asking was the complaint.
    video_opts = [o for o in options[1:] if "🎵" not in o["label"]]
    if not is_playlist and len(video_opts) <= 1:
        throttled_edit(client, message.chat.id, status.id,
                        f"🎬 <b>{esc(title[:70])}</b>\nOne quality available — grabbing it…",
                        force=True)
        return queue_leech_job(client, message, url, dest, zip_output,
                                rename=rename, folder=folder)
    try:
        _do_picker(client, message, status.id, url, dest, zip_output, rename, folder, title, options)
    except Exception:
        logger.exception("picker render failed")
        dispatch_link(client, message, url, dest, zip_output=zip_output,
                      rename=rename, folder=folder)


def _do_picker(client, message, status_id, url, dest, zip_output, rename, folder, title, options):
    """WZML-X-style menu: one button per real format. Video-only formats
    auto-merge best audio on pick. The menu message STAYS after selection
    (user preference — the fresh job status message appears separately)."""
    token = state.new_quality_session(url, dest, zip_output, message.id, file_anchor(message))
    state.QualityExtra.save(token, rename=rename, folder=folder)
    state.PickerFormats.save(token, title, options)
    rows = []

    def add_btn(label, data):
        nonlocal row            # v15 bug: assignment made Python treat `row`
        row.append(InlineKeyboardButton(label[:60], callback_data=data))  # as a fresh local → UnboundLocalError on every tap
        if len(row) == 2:
            rows.append(row); row = []

    row = []
    for i, opt in enumerate(options):
        add_btn(opt["label"], f"ytf:{token}:{i}")
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"ytqcancel:{token}")])

    client.edit_message_text(
        message.chat.id, status_id,
        f"🎬 <b>{esc(title[:70])}</b>\n<i>Choose video quality:</i>",
        reply_markup=InlineKeyboardMarkup(rows),
    )


@app.on_callback_query(filters.regex(r"^ytf:"))
@guarded
def cb_format_pick(client, cq):
    """A concrete format was chosen. Video-only formats get BEST audio
    merged automatically (the old second "🎚 Merge which audio?" step is
    gone — nobody ever wants a silent video; the rare no-audio case is
    covered by the 🔇-labeled buttons which already skip merging)."""
    _, token, idx = cq.data.split(":", 2)
    session = state.get_quality_session(token)
    if not session:
        return cq.answer("Expired — send the link again.", show_alert=True)
    title, options = state.PickerFormats.load(token)
    if not options or int(idx) >= len(options):
        return cq.answer("Menu expired.", show_alert=True)
    opt = options[int(idx)]

    selector = opt["selector"]
    if opt.get("needs_audio"):
        selector = f"{selector}+ba/b"   # auto-merge best audio
    cq.answer()
    _launch_leech_from_menu(client, cq, token, session, selector)


@app.on_callback_query(filters.regex(r"^ytback:"))
@guarded
def cb_format_back(client, cq):
    token = cq.data.split(":", 1)[1]
    session = state.get_quality_session(token)
    if session:
        try:
            status = cq.message
            title, options = state.PickerFormats.load(token)
            _do_picker(client, status, status.id, session["url"], session["dest"],
                       session["zip_output"], None, None, title, options)
            return cq.answer()
        except Exception:
            pass
    cq.answer("Expired — send the link again.", show_alert=True)


def _launch_leech_from_menu(client, cq, token, session, selector):
    extra = state.QualityExtra.take(token)
    state.pop_quality_session(token)
    cq.answer()
    _launch_status_and_job(client, cq.message.chat.id, cq.message.id, session,
                           selector, extra)


def _launch_status_and_job(client, chat_id, reply_msg_id_unused, session,
                           selector, extra):
    task_id = new_task_id()
    tag = f" 📦→{config.DEST_LABEL[session['dest']]}" if session["zip_output"] else f" →{config.DEST_LABEL[session['dest']]}"
    # Anchor to session["reply_to"] (the ORIGINAL command message, already
    # inside the right topic) — this Pyrogram fork has no message_thread_id
    # param, so (WZML-X-style) a new send needs reply_to_message_id pointing
    # at a message already in the topic, or it lands in the group's General
    # topic instead of the one the user is actually chatting in.
    status = client.send_message(
        chat_id,
        f"🔎 Queued{tag} — <code>{esc(task_id)}</code>",
        reply_to_message_id=session["reply_to"],
        reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, chat_id, status.id, "yt-dlp", task_dir)
    state.task_queue.put((run_leech_job,
                           (client, chat_id, status.id, session["url"], task_id,
                            session["reply_to"], session["dest"], session["zip_output"],
                            selector, extra.get("rename"), extra.get("folder"),
                            None, True, session.get("file_anchor"))))


# Legacy ytq: handler kept for the old bucket-style callbacks (in case an
# old message is still on screen) and the plain "Best" path.
@app.on_callback_query(filters.regex(r"^ytq:"))
@guarded
def cb_quality_pick(client, cq):
    _, token, selector = cq.data.split(":", 2)
    session = state.pop_quality_session(token)
    if not session:
        return cq.answer("This picker expired — send the link again.", show_alert=True)
    extra = state.QualityExtra.take(token)
    cq.answer()
    task_id = new_task_id()
    tag = f" 📦→{config.DEST_LABEL[session['dest']]}" if session["zip_output"] else f" →{config.DEST_LABEL[session['dest']]}"
    status = cq.message.reply_text(f"🔎 Queued{tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, cq.message.chat.id, status.id, "yt-dlp", task_dir)
    state.task_queue.put((run_leech_job,
                           (client, cq.message.chat.id, status.id, session["url"], task_id,
                            session["reply_to"], session["dest"], session["zip_output"], selector,
                            extra.get("rename"), extra.get("folder"),
                            None, True, session.get("file_anchor"))))


@app.on_callback_query(filters.regex(r"^ytqcancel:"))
@guarded
def cb_quality_cancel(client, cq):
    token = cq.data.split(":", 1)[1]
    state.pop_quality_session(token)
    safe_edit(cq, "Cancelled.")
    cq.answer()


@app.on_callback_query(filters.regex(r"^ytretry:"))
@guarded
def cb_quality_retry(client, cq):
    token = cq.data.split(":", 1)[1]
    t = state.pop_retry_token(token)
    if not t:
        return cq.answer("Expired — send the link again.", show_alert=True)
    cq.answer("Retrying…")
    safe_edit(cq, "🔎 Checking available qualities… (retry)")
    # Re-run the picker with a FRESH probe; yt-dlp may have recovered.
    try:
        title, options, _is_pl = engine.probe_qualities(t["url"], cq.message.chat.id)
        _do_picker(client, cq.message, cq.message.id, t["url"], t["dest"],
                   t["zip_output"], t["rename"], t["folder"], title, options)
    except Exception as e:
        logger.warning(f"retry probe still failing for {t['url']}: {e}")
        dispatch_link(client, cq.message, t["url"], t["dest"],
                      zip_output=t["zip_output"], rename=t["rename"], folder=t["folder"])


def queue_telegram_media_job(client, message, media_msg, dest, zip_output=False,
                             rename=None, folder=None):
    """Handles `/m`/`/l` (etc.) used as a reply to a file already in the
    chat — re-downloads that file via Pyrogram and re-uploads it to the
    requested destination, instead of requiring a URL."""
    if not state.ensure_free(config.MIN_FREE_MB):
        return message.reply_text("❌ Disk full, try again later.")
    task_id = new_task_id()
    tag = f"📦→{config.DEST_LABEL[dest]}" if zip_output else f"→{config.DEST_LABEL[dest]}"
    status = message.reply_text(f"🔎 Queued {tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "tg-media", task_dir)
    state.task_queue.put((run_telegram_media_job,
                           (client, message.chat.id, status.id, media_msg, task_id, message.id, dest,
                            zip_output, rename, folder, file_anchor(message))))


def run_telegram_media_job(client, chat_id, msg_id, media_msg, task_id, reply_to, dest,
                           zip_output=False, rename=None, folder=None, file_anchor=None):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        throttled_edit(client, chat_id, msg_id, "⬇️ Fetching from Telegram…", markup=cancel_kb(task_id), force=True)
        samples, last_edit = [], [0.0]

        def progress(current, total):
            if state.is_cancelled(task_id):
                raise state.CancelledError("cancelled by user")
            now = time.time()
            if now - last_edit[0] < 1.5 and current != total:
                return
            last_edit[0] = now
            samples.append((current, now))
            if len(samples) > 6:
                samples.pop(0)
            text = (f"⬇️ Fetching from Telegram…\n{bar(current, total)}\n"
                    f"`{fmtsz(current)} / {fmtsz(total)}`  •  {smooth_speed(samples)}")
            throttled_edit(client, chat_id, msg_id, text, markup=cancel_kb(task_id))

        fpath = client.download_media(media_msg, file_name=os.path.join(task_dir, ""), progress=progress)
        if not fpath:
            return throttled_edit(client, chat_id, msg_id, "❌ Couldn't fetch that message's file.", force=True)

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        fpath = fix_unknown_ext(fpath)

        if zip_output and fpath.lower().endswith(".zip"):
            # source is ALREADY a zip — re-zipping would build zpath == fpath
            # and ZipFile("w") truncates the just-downloaded file before
            # reading it (the ENOENT-on-upload crash). Send as-is.
            pass
        elif zip_output:
            throttled_edit(client, chat_id, msg_id, "📦 Zipping…", force=True)
            import zipfile
            zpath = os.path.join(task_dir, os.path.splitext(os.path.basename(fpath))[0] + ".zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                zf.write(fpath, os.path.basename(fpath))
            os.remove(fpath)
            fpath = zpath

        size = os.path.getsize(fpath)
        links = []
        file_kb = None
        if dest == "telegram":
            throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", markup=cancel_kb(task_id), force=True)
            uploader.upload_to_telegram(client, chat_id, fpath, msg_id,
                                        reply_to=file_anchor, task_id=task_id)
        else:
            throttled_edit(client, chat_id, msg_id, "☁️ Uploading to Drive…", markup=cancel_kb(task_id), force=True)
            info = drive.upload_file_full(fpath, task_id=task_id, rename=rename, folder=folder)
            if info:
                links.append(info["link"])
                file_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{info['id']}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{info['id']}"),
                ]])

        summary = f"✅ Done — <code>{fmtsz(size)}</code> → {config.DEST_LABEL[dest]}"
        if links:
            summary += "\n" + "\n".join(links)
        throttled_edit(client, chat_id, msg_id, summary, markup=file_kb, force=True)
        autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)

    except state.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except Exception as e:
        logger.exception(f"[{task_id}] telegram-media job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ Failed: <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        state.drop_url_by_task(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


# ── /setthumb removed — thumbnails are fully automatic now (natural):
#    the source video's own cover, else an ffmpeg frame grab. ────────────


GDRIVE_RE = re.compile(
    r"drive\.google\.com/(?:file/d/|drive/folders/|open\?id=|drive/u/\d+/folders/)([\w-]+)"
    r"|drive\.google\.com/uc\?(?:export=download&)?id=([\w-]+)"
    r"|drive\.usercontent\.google\.com/download\?id=([\w-]+)"
    r"|docs\.google\.com/uc\?(?:export=download&)?id=([\w-]+)"
    r"|drive\.google\.com/drive/(?:u/\d+/|mobile/)*folders/([\w-]+)"
)

def _gdrive_file_id(url):
    """file_id for ANY recognized Google Drive link form (file/folder/uc/
    usercontent download, mobile/u/ folder forms), else None. Used by the
    router so no Drive link variant can fall through to yt-dlp's generic
    extractor (the usercontent.google.com host form did exactly that — its
    host isn't drive.google.com, so the old host check missed it)."""
    m = GDRIVE_RE.search(url)
    if not m:
        return None
    # groups(): returns None for non-participating groups
    return next((g for g in m.groups() if g), None)


def dispatch_link(client, message, url, dest, zip_output=False, rename=None, folder=None,
                  format_selector=None):
    """Routes url to the right downloader based on what it looks like —
    magnet/torrent, Instagram, a Google Drive file/folder link, or the
    generic yt-dlp/direct-download path — carrying dest/zip/rename/folder
    (and the raw yt-dlp -f selector, when given) through to whichever job
    actually runs."""
    host = _host_of(url)

    if url.startswith("magnet:") or url.lower().endswith(".torrent"):
        from .handlers_subproc import queue_subprocess_job
        return queue_subprocess_job(client, message, "torrent", url, dest, zip_output)

    # Google Drive — file, folder, uc, open?id and usercontent download
    # forms. A recognized link ALWAYS routes to the Drive pipeline here;
    # an unrecognized drive.google.com URL (the Drive UI itself — no file
    # id) is rejected with a hint instead of silently going to yt-dlp.
    if host in ("drive.google.com", "drive.usercontent.google.com", "docs.google.com"):
        fid = _gdrive_file_id(url)
        if fid:
            return queue_gdrive_job(client, message, fid, dest, zip_output,
                                     rename=rename, folder=folder)
        return message.reply_text(
            "☁️ That's a Google Drive URL but I couldn't find a file/folder id in it.\n"
            "Open it in Drive and share the direct file or folder link "
            "(<code>drive.google.com/file/d/…</code> or <code>/folders/…</code>).")

    if any(host == h or host.endswith("." + h) for h in config.IG_HOSTS):
        from .handlers_instagram import dispatch_instagram
        return dispatch_instagram(client, message, url, dest)

    queue_leech_job(client, message, url, dest, zip_output, rename=rename, folder=folder,
                    format_selector=format_selector)


def queue_gdrive_job(client, message, file_id, dest, zip_output=False, rename=None, folder=None):
    if not drive.enabled():
        return message.reply_text(
            "☁️ That's a Google Drive link, but Drive isn't configured on this bot "
            "(missing GCP credentials in <code>.env</code>) — I can't fetch it without API access."
        )
    if not state.ensure_free(config.MIN_FREE_MB):
        return message.reply_text("❌ Disk full, try again later.")
    task_id = new_task_id()
    tag = f" 📦→{config.DEST_LABEL[dest]}" if zip_output else f" →{config.DEST_LABEL[dest]}"
    status = message.reply_text(f"🔎 Queued{tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "gdrive", task_dir)
    state.task_queue.put((run_gdrive_job,
                           (client, message.chat.id, status.id, file_id, task_id, message.id,
                            dest, zip_output, rename, folder, file_anchor(message))))


def run_gdrive_job(client, chat_id, msg_id, file_id, task_id, reply_to, dest,
                   zip_output=False, rename=None, folder=None, file_anchor=None):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        throttled_edit(client, chat_id, msg_id, "🔎 Checking Drive link…", markup=cancel_kb(task_id), force=True)
        meta = drive.get_file_any(file_id)
        if not meta:
            return throttled_edit(
                client, chat_id, msg_id,
                "❌ Couldn't read that Drive link — it may be private (not shared with "
                "this bot's Drive account) or deleted.",
                force=True,
            )

        if meta.get("mimeType") == "application/vnd.google-apps.folder":
            # Overview FIRST (pure API, no downloads): what's inside, how big.
            ov = _drive_folder_overview(meta["id"])
            lines = [f"📂 <b>{esc(meta.get('name', 'folder'))}</b> — overview",
                     f"📄 {ov['files']} file(s) • 📦 {fmtsz(ov['bytes'])}"]
            bits = []
            if ov["video"]:
                bits.append(f"🎬 {ov['video']} video")
            if ov["image"]:
                bits.append(f"🖼 {ov['image']} image")
            if ov["audio"]:
                bits.append(f"🎵 {ov['audio']} audio")
            if ov["other"]:
                bits.append(f"📎 {ov['other']} other")
            if bits:
                lines.append(" · ".join(bits))
            lines.append("⏳ Starting — every file is sent the moment it's downloaded "
                         "and deleted right after its upload…")
            throttled_edit(client, chat_id, msg_id, "\n".join(lines), force=True)

            if zip_output:
                # an archive needs every file on disk before it can be built
                _download_gdrive_folder(client, chat_id, msg_id, task_id, meta, task_dir)
            else:
                # Live-stream the folder: a LiveDispatcher watches task_dir
                # WHILE the walk downloads, so each file is uploaded the
                # moment it lands and deleted right after it's sent. The old
                # behavior held the ENTIRE folder on disk before the first
                # upload even started — both a "sent immediately" violation
                # and a disk-space bomb on big folders.
                # The overview totals feed a live "37/214 sent • 1.2 GB /
                # 4.5 GB" position header on every progress edit.
                disp = LiveDispatcher(client, chat_id, msg_id, task_dir, dest,
                                      reply_to=file_anchor, task_id=task_id, folder=folder,
                                      title=meta.get("name", "folder"),
                                      total_files=ov["files"], total_bytes=ov["bytes"])
                try:
                    _download_gdrive_folder(client, chat_id, msg_id, task_id, meta, task_dir,
                                            disp=disp)
                    if state.is_cancelled(task_id):
                        # route through finalize (not just stop()) so the
                        # executor threads shut down deterministically —
                        # pending workers see the cancel flag and discard
                        # themselves, and their finally-block deletes the
                        # partial files they were holding.
                        try:
                            disp.finalize(timeout=60)
                        except Exception:
                            disp.stop()   # timeout waiting on stragglers — just stop
                        return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
                    sent, errors = disp.finalize(timeout=1800)
                except BaseException:
                    disp.stop()   # never leak the watcher thread on error paths
                    raise
                if sent == 0 and not errors:
                    return throttled_edit(client, chat_id, msg_id,
                                          "❌ Nothing to upload — the folder may be empty.", force=True)
                summary = f"✅ Done — {sent} file(s) → {config.DEST_LABEL[dest]}"
                if errors:
                    summary += f"\n⚠️ {len(errors)} failed."
                throttled_edit(client, chat_id, msg_id, summary, force=True)
                autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)
                return
        else:
            _download_gdrive_single(client, chat_id, msg_id, task_id, meta, task_dir)

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        entries = [os.path.join(r, f) for r, _, fs in os.walk(task_dir) for f in fs]
        if not entries:
            return throttled_edit(client, chat_id, msg_id, "❌ Nothing to upload — the file may be empty.", force=True)

        if zip_output:
            throttled_edit(client, chat_id, msg_id, "📦 Zipping…", force=True)
            import zipfile
            zpath = os.path.join(config.DOWNLOAD_DIR, f"{task_id}.zip")
            try:
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                    for fp in entries:
                        zf.write(fp, os.path.relpath(fp, task_dir))
                throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(os.path.getsize(zpath))}</code>)", force=True)
                if dest == "telegram":
                    uploader.upload_to_telegram(client, chat_id, zpath, msg_id,
                                                reply_to=file_anchor, task_id=task_id)
                else:
                    drive.upload_file_full(zpath, task_id=task_id, rename=rename,
                                           folder=folder, chat_id=chat_id)
                throttled_edit(client, chat_id, msg_id, f"✅ Done → {config.DEST_LABEL[dest]}", force=True)
            finally:
                # zpath lives OUTSIDE task_dir (so zip_dir can't zip itself) —
                # without this finally, any upload error leaked the archive
                # in downloads/ forever (purge_stale only scans dirs).
                try:
                    os.remove(zpath)
                except OSError:
                    pass
        else:
            fpath = fix_unknown_ext(entries[0])
            size = os.path.getsize(fpath)
            done_kb = None
            link = None
            final_name = os.path.basename(fpath)
            if dest == "telegram":
                if rename:
                    rename = rename.strip()
                    ext = os.path.splitext(fpath)[1]
                    if ext and not rename.lower().endswith(ext.lower()):
                        rename = rename + ext
                    new_path = os.path.join(os.path.dirname(fpath), rename)
                    try:
                        os.rename(fpath, new_path)
                        fpath = new_path
                    except OSError:
                        pass
                    size = os.path.getsize(fpath)
                throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", markup=cancel_kb(task_id), force=True)
                uploader.upload_to_telegram(client, chat_id, fpath, msg_id,
                                            reply_to=file_anchor, task_id=task_id)
            else:
                throttled_edit(client, chat_id, msg_id, "☁️ Uploading to Drive…", markup=cancel_kb(task_id), force=True)
                info = drive.upload_file_full(fpath, task_id=task_id, rename=rename,
                                               folder=folder, chat_id=chat_id)
                if info:
                    link = info["link"]
                    done_kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔗 Get link", callback_data=f"qlink:{info['id']}"),
                        InlineKeyboardButton("📥 Send to Telegram", callback_data=f"qsnd:{info['id']}"),
                    ], [
                        InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{info['id']}"),
                        InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{info['id']}"),
                    ]])
            summary = (f"✅ <b>{esc(final_name)}</b>\n"
                       f"📦 <code>{fmtsz(size)}</code> → {config.DEST_LABEL[dest]}")
            if folder and dest == "drive":
                summary += f" 📁<code>{esc(folder)}</code>"
            if link:
                summary += f"\n{link}"
            throttled_edit(client, chat_id, msg_id, summary, markup=done_kb, force=True)

        autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)

    except state.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except Exception as e:
        logger.exception(f"[{task_id}] gdrive job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ Failed: <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


def _drive_folder_overview(folder_id):
    """Cheap API-only walk (no downloads): counts files, sums bytes and
    buckets them by broad type, so the user gets an overview of what's
    inside a Drive folder link BEFORE anything is fetched. Capped at the
    same 2000-item limit the downloader uses."""
    FOLDER_MIME = "application/vnd.google-apps.folder"
    stats = {"files": 0, "bytes": 0, "video": 0, "image": 0, "audio": 0, "other": 0}

    def _bucket(mime):
        mime = mime or ""
        for k in ("video", "image", "audio"):
            if mime.startswith(k + "/"):
                return k
        return "other"

    def walk(fid, depth):
        if depth > 8 or stats["files"] >= 2000:
            return
        for f in drive.list_folder_any(fid):
            if f.get("mimeType") == FOLDER_MIME:
                walk(f["id"], depth + 1)
            else:
                stats["files"] += 1
                stats["bytes"] += int(f.get("size") or 0)
                stats[_bucket(f.get("mimeType"))] += 1

    try:
        walk(folder_id, 0)
    except Exception as e:
        logger.warning(f"folder overview walk failed: {e}")
    return stats


def _download_gdrive_single(client, chat_id, msg_id, task_id, meta, task_dir):
    fname = meta.get("name", "file")
    fpath = os.path.join(task_dir, fname)
    throttled_edit(client, chat_id, msg_id, f"⬇️ Downloading from Drive…\n`{fname}`", markup=cancel_kb(task_id), force=True)
    drive.download_file_content(meta["id"], fpath, task_id=task_id, mime_type=meta.get("mimeType"))


def _download_gdrive_folder(client, chat_id, msg_id, task_id, folder_meta, task_dir,
                            disp=None):
    """Walks a Drive folder, downloading each file. disp (the streaming
    dispatcher) makes download progress LIVE and unambiguous during the
    overlap: its header shows sent X/N, and a callback updates the
    current-download line with per-chunk byte progress — so during the
    'file 2 uploads while file 3 downloads' phase BOTH sides are visible."""
    root = os.path.join(task_dir, folder_meta.get("name", "folder"))
    os.makedirs(root, exist_ok=True)

    def walk(folder_id, local_dir, depth=0):
        if state.is_cancelled(task_id) or depth > 8:
            return
        for f in drive.list_folder_any(folder_id):
            if state.is_cancelled(task_id):
                return
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                sub = os.path.join(local_dir, f.get("name", "folder"))
                os.makedirs(sub, exist_ok=True)
                walk(f["id"], sub, depth + 1)
            else:
                fname = f.get("name", "?")
                expected_total = int(f.get("size") or 0)

                def _dprog(done, ctotal, _n=[0], _fname=fname, _total=expected_total):
                    # ctotal can read 0 on early chunks — fall back to the
                    # Drive listing's known file size so the bar never shows
                    # the bogus "176 MB / 0 B"
                    if disp is None:
                        return
                    now = time.time()
                    if now - _n[0] < 1.5 and done != _total:
                        return
                    _n[0] = now
                    disp._set_download(_fname, done, ctotal or _total)

                if disp is not None:
                    disp._set_download(fname, 0, expected_total, force=True)
                else:
                    throttled_edit(client, chat_id, msg_id,
                                   f"⬇️ Downloading from Drive…\n`{fname}`",
                                   markup=cancel_kb(task_id))
                try:
                    drive.download_file_content(
                        f["id"], os.path.join(local_dir, f.get("name", f["id"])),
                        task_id=task_id, mime_type=f.get("mimeType"),
                        progress_cb=_dprog)
                except state.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[{task_id}] gdrive folder item failed: {f.get('name')}: {e}")
                finally:
                    if disp is not None:
                        disp._set_download(None, 0, 0)

    walk(folder_meta["id"], root)



def queue_leech_job(client, message, url, dest, zip_output=False, format_selector=None,
                    rename=None, folder=None):
    task_id = new_task_id()
    tag = f" 📦→{config.DEST_LABEL[dest]}" if zip_output else f" →{config.DEST_LABEL[dest]}"
    status = message.reply_text(f"🔎 Queued{tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "yt-dlp", task_dir)
    state.register_url(url, task_id)
    state.task_queue.put((run_leech_job,
                           (client, message.chat.id, status.id, url, task_id, message.id, dest,
                            zip_output, format_selector, rename, folder,
                            None, True, file_anchor(message))))


def run_batch_leech_job(client, chat_id, msg_id, urls, task_id, reply_to, dest,
                        zip_output=False, format_selector=None, folder=None,
                        file_anchor=None):
    """Batch links in ONE job with the Drive-folder look: a single status
    message whose header tracks the batch (📂 Batch — 2/5 processed) while
    each link's own download/upload bar renders underneath it. Links are
    processed SEQUENTIALLY in one worker so their progress edits can't
    fight each other (the flicker bug), files stream out per link, and a
    final batch summary lands as its own message."""
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    t_start = time.time()
    total = len(urls)
    batch = {"i": 0, "failed": 0, "files": 0, "bytes": 0}

    def prefix_fn():
        left = total - batch["i"]
        return (f"📂 <b>Batch</b> — {batch['i']}/{total} done • {left} left • "
                f"{fmtsz(batch['bytes'])} sent")

    def bump(path, size):
        batch["files"] += 1
        batch["bytes"] += size

    try:
        for url in urls:
            if state.is_cancelled(task_id):
                break
            batch["i"] += 1
            try:
                run_leech_job(client, chat_id, msg_id, url, task_id, reply_to, dest,
                              zip_output, format_selector, None, folder,
                              progress_prefix_fn=prefix_fn, cleanup=False,
                              file_anchor=file_anchor, on_sent=bump)
            except Exception as e:
                batch["failed"] += 1
                logger.warning(f"[{task_id}] batch link failed: {e}")
                throttled_edit(client, chat_id, msg_id,
                               f"{prefix_fn()}\n⚠️ One link failed: <code>{esc(str(e)[:120])}</code>",
                               force=True)
        summary = (f"✅ Batch complete — {total - batch['failed']}/{total} links → "
                   f"{config.DEST_LABEL[dest]}")
        if batch["failed"]:
            summary += f"\n⚠️ {batch['failed']} failed"
        summary += f"\n⏱ {fmt_time(int(time.time() - t_start))}"
        # final result as its own plain message (the status bubble keeps the
        # last progress state as a record)
        try:
            client.send_message(chat_id, summary, reply_to_message_id=file_anchor)
        except Exception as e:
            logger.warning(f"batch summary send failed, falling back to status edit: {e}")
            throttled_edit(client, chat_id, msg_id, summary, force=True)
        autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)
    finally:
        state.drop_job(task_id)
        state.drop_url_by_task(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


def run_leech_job(client, chat_id, msg_id, url, task_id, reply_to, dest, zip_output=False,
                  format_selector=None, rename=None, folder=None,
                  progress_prefix_fn=None, cleanup=True, file_anchor=None, on_sent=None):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    _source_thumb_url = None   # original video's thumbnail (YouTube etc.)
    t_start = time.time()
    try:
        if not state.ensure_free(config.MIN_FREE_MB):
            return throttled_edit(client, chat_id, msg_id, "❌ Disk full, try again later.", force=True)

        pref = progress_prefix_fn() if progress_prefix_fn else ""
        if pref:
            pref += "\n"
        throttled_edit(client, chat_id, msg_id, f"{pref}🔎 Resolving link…", markup=cancel_kb(task_id), force=True)

        host = _host_of(url)
        known_site = any(host == h or host.endswith("." + h) for h in config.YTDLP_AUTO_HOSTS)
        # Instagram is gallery-dl territory, never yt-dlp (WZML-X parity).
        # v15 bug: this referenced `message`, which doesn't exist inside
        # run_leech_job (it runs from the queue with explicit args) — every
        # Instagram link that reached here crashed with NameError.
        if any(host == h or host.endswith("." + h) for h in config.IG_HOSTS):
            from .handlers_subproc import queue_subprocess_job

            class _MsgShim:
                """queue_subprocess_job expects a Message (chat.id/.id/
                reply_text); rebuild the minimum surface from our args.
                reply_text() must mimic real Pyrogram semantics (quote=True
                by default in groups, i.e. reply_to_message_id=self.id) or
                the status message it creates loses its topic anchor and
                lands in the group's General topic instead of msg_id's."""
                def __init__(self):
                    self.chat = type("C", (), {"id": chat_id})()
                    self.id = msg_id
                def reply_text(self, text, **kw):
                    kw.setdefault("reply_to_message_id", self.id)
                    return client.send_message(chat_id, text, **kw)

            sub_tid = queue_subprocess_job(client, _MsgShim(), "gallery", url,
                                            dest, zip_output, anchor=file_anchor)
            # Leave an explanatory breadcrumb in THIS status bubble instead
            # of a dead "Queued" message pointing at nothing.
            if sub_tid:
                throttled_edit(client, chat_id, msg_id,
                                f"📷 Instagram link — handed to <b>gallery-dl</b> "
                                f"(job <code>{esc(sub_tid)}</code>).", force=True)
            return
        use_direct = engine.is_direct_file_link(url)
        probed_name = None
        if not use_direct and not known_site:
            probed_name, probed = engine.probe_direct(url)
            if probed:
                logger.info(f"[{task_id}] {host} isn't text/html — treating as a direct file, not a video page")
            use_direct = bool(probed)
            if not use_direct and probed is None and engine.looks_like_file_url(url):
                # probe inconclusive (slow/dead server) + the URL path ends
                # with a file extension → direct download, never yt-dlp
                logger.info(f"[{task_id}] {host} probe inconclusive but URL has a file extension — direct download")
                use_direct = True

        if use_direct:
            with state.jobs_lock:
                if task_id in state.ACTIVE_JOBS:
                    state.ACTIVE_JOBS[task_id]["kind"] = "direct"   # /stats truth
            fpath = engine.download_direct(url, chat_id, msg_id, task_id, client, task_dir,
                                            progress_prefix_fn=progress_prefix_fn)
            info = None
        else:
            # PLAYLIST (WZML-X style): the yt-dlp info-dict hook is the
            # SINGLE source of truth — each entry is handed over the moment
            # its final post-processed file exists; the dispatcher runs with
            # the filesystem watcher DISABLED (a watcher here sweeps up
            # yt-dlp's intermediate format files (.f137.mp4/.f140.m4a) and
            # .meta sidecars, causing double-sends and phantom failures).
            # Each entry gets the metadata re-tag + unknown-ext fix before
            # its send, then is deleted right after it's sent.
            # /zm /zl (zip_output) bypasses streaming entirely: every entry
            # is collected and sent as ONE zip — streaming + zipping are
            # mutually exclusive by definition.
            playlist_state = {"n": 0}
            disp_holder = {}
            if zip_output:
                result = engine.download(url, chat_id, msg_id, task_id, client, task_dir,
                                         format_selector=format_selector)
                if not isinstance(result, list):
                    files = []
                else:
                    files = [p for p in result if os.path.exists(p) and os.path.getsize(p) > 0]
                if not files:
                    return throttled_edit(client, chat_id, msg_id,
                                          "❌ Playlist had no downloadable entries.", force=True)
                throttled_edit(client, chat_id, msg_id,
                               f"📦 Zipping {len(files)} item(s)…", force=True)
                import zipfile
                # zip name from the playlist title — entries are "NN - Title"
                stem = re.sub(r"^\d+\s*[-.]\s*", "",
                              os.path.splitext(os.path.basename(files[0]))[0]) or task_id
                zpath = os.path.join(task_dir, f"{stem}.zip")
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                    for fp in files:
                        zf.write(fp, os.path.basename(fp))
                size = os.path.getsize(zpath)
                links = []
                file_kb = None
                if dest == "drive":
                    throttled_edit(client, chat_id, msg_id, "☁️ Uploading to Drive…", markup=cancel_kb(task_id), force=True)
                    dinfo = drive.upload_file_full(zpath, task_id=task_id, rename=rename, folder=folder)
                    if dinfo:
                        links.append(dinfo["link"])
                        file_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Get link", callback_data=f"qlink:{dinfo['id']}")]])
                else:
                    throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", markup=cancel_kb(task_id), force=True)
                    uploader.upload_to_telegram(client, chat_id, zpath, msg_id,
                                                reply_to=file_anchor, task_id=task_id)
                summary = (f"✅ Playlist zip — {len(files)} item(s), 📦 <code>{fmtsz(size)}</code> "
                           f"→ {config.DEST_LABEL[dest]}")
                if links:
                    summary += "\n" + "\n".join(links)
                summary += f"\n⏱ {fmt_time(int(time.time() - t_start))}"
                throttled_edit(client, chat_id, msg_id, summary, markup=file_kb, force=True)
                autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)
                return
            playlist_state = {"n": 0}
            disp_holder = {}

            def _finish_entry(path):
                """WZML-X per-entry pipeline: fix ext, re-tag metadata,
                THEN submit. Returns the final path (or None to skip)."""
                fixed = fix_unknown_ext(path)
                if fixed == path and not os.path.exists(path):
                    return None   # gone before we got to it — skip
                return engine.fix_media_metadata(fixed)

            def _on_entry(path):
                try:
                    final = _finish_entry(path)
                    if not final:
                        return
                    if "disp" not in disp_holder:
                        disp_holder["disp"] = LiveDispatcher(
                            client, chat_id, msg_id, task_dir, dest,
                            reply_to=file_anchor, task_id=task_id, max_workers=2,
                            watch=False,   # info-dict hook is the ONLY feed
                            on_sent=on_sent)
                        throttled_edit(client, chat_id, msg_id,
                                       f"📂 Playlist — streaming → {config.DEST_LABEL[dest]}",
                                       force=True)
                    playlist_state["n"] += 1
                    disp_holder["disp"].submit(final)
                except Exception as ex:
                    logger.warning(f"[{task_id}] playlist entry dispatch failed: {ex}")

            result = engine.download(url, chat_id, msg_id, task_id, client, task_dir,
                                     format_selector=format_selector,
                                     on_entry_done=_on_entry,
                                     progress_prefix_fn=progress_prefix_fn)
            if result is None:
                # playlist streamed — every entry already dispatched
                d = disp_holder.get("disp")
                if d is None:
                    return throttled_edit(client, chat_id, msg_id,
                                          "❌ Playlist had no downloadable entries.", force=True)
                sent, errors = d.finalize(timeout=7200)
                summary = f"✅ Playlist done — {sent} item(s) → {config.DEST_LABEL[dest]}"
                if errors:
                    summary += f"\n⚠️ {len(errors)} failed"
                summary += f"\n⏱ {fmt_time(int(time.time() - t_start))}"
                # final result as its own plain message (like the IG summary) —
                # the status bubble stays behind as the progress record
                try:
                    client.send_message(chat_id, summary, reply_to_message_id=d.reply_to)
                except Exception as e:
                    logger.warning(f"playlist summary send failed, falling back to status edit: {e}")
                    throttled_edit(client, chat_id, msg_id, summary, force=True)
                autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)
                return
            fpath, info = result
            # Remember the ORIGINAL video's thumbnail so the Telegram upload
            # shows exactly what the YouTube page shows (WZML-X parity).
            if isinstance(info, dict):
                _source_thumb_url = info.get("thumbnail")

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        fpath = fix_unknown_ext(fpath)
        # WZML-X-style metadata pass: yt-dlp's merge can drop the duration/
        # resolution tags Telegram needs (a missing duration is why uploads
        # showed up as "0 seconds" in the player). Re-tag from ffprobe.
        fpath = engine.fix_media_metadata(fpath)

        if zip_output and fpath.lower().endswith(".zip"):
            # source is ALREADY a zip — re-zipping would build zpath == fpath
            # and ZipFile("w") truncates the just-downloaded file before
            # reading it (the ENOENT-on-upload crash). Send as-is.
            pass
        elif zip_output:
            throttled_edit(client, chat_id, msg_id, "📦 Zipping…", force=True)
            import zipfile
            zpath = os.path.join(task_dir, os.path.splitext(os.path.basename(fpath))[0] + ".zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                zf.write(fpath, os.path.basename(fpath))
            os.remove(fpath)
            fpath = zpath

        size = os.path.getsize(fpath)
        links = []
        file_kb = None

        # Real resolution/duration of the finished file — shown in the
        # completion summary ("no quality is shown to me" fix). Gated to
        # actual media: ffprobe on a zip/pdf is just a wasted subprocess.
        vmeta = uploader.probe_meta_if_media(fpath)
        qline = ""
        if vmeta.get("width"):
            from .uploader import _quality_line
            qline = _quality_line(vmeta)

        if dest == "telegram":
            if rename:
                # Extension is NEVER replaced: append the real one if the
                # requested name doesn't already end with it ("my.video"
                # keeps ".mp4"; "clip.mp4" stays "clip.mp4").
                rename = rename.strip()
                ext = os.path.splitext(fpath)[1]
                if ext and not rename.lower().endswith(ext.lower()):
                    rename = rename + ext
                new_path = os.path.join(os.path.dirname(fpath), rename)
                try:
                    os.rename(fpath, new_path)
                    fpath = new_path
                except OSError:
                    pass
                size = os.path.getsize(fpath)
            pref = progress_prefix_fn() if progress_prefix_fn else ""
            if pref:
                pref += "\n"
            throttled_edit(client, chat_id, msg_id,
                           f"{pref}☁️ Uploading… (<code>{fmtsz(size)}</code>)",
                           markup=cancel_kb(task_id), force=True)
            uploader.upload_to_telegram(client, chat_id, fpath, msg_id,
                                        reply_to=file_anchor, task_id=task_id,
                                        video_meta=vmeta,
                                        thumb_url=_source_thumb_url)
            if on_sent:
                try:
                    on_sent(fpath, size)
                except Exception:
                    pass
        elif dest == "drive":
            throttled_edit(client, chat_id, msg_id, "☁️ Uploading to Drive…", markup=cancel_kb(task_id), force=True)
            dinfo = drive.upload_file_full(fpath, task_id=task_id, rename=rename,
                                           folder=folder, chat_id=chat_id)
            if dinfo:
                links.append(dinfo["link"])
                file_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔗 Get link", callback_data=f"qlink:{dinfo['id']}"),
                    InlineKeyboardButton("📥 Send to Telegram", callback_data=f"qsnd:{dinfo['id']}"),
                ], [
                    InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{dinfo['id']}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{dinfo['id']}"),
                ]])

        pref = progress_prefix_fn() if progress_prefix_fn else ""
        if pref:
            pref += "\n"
        summary = (f"{pref}✅ <b>{esc(os.path.basename(fpath))}</b>\n"
                   f"📦 <code>{fmtsz(size)}</code>"
                   + (f" • 🎬 {qline}" if qline else "") +
                   f" → {config.DEST_LABEL[dest]}")
        if folder and dest == "drive":
            summary += f" 📁<code>{esc(folder)}</code>"
        title = (info or {}).get("title") if isinstance(info, dict) else None
        if title and dest != "telegram":
            summary = f"🎬 {esc(str(title)[:80])}\n{summary}"
        if links:
            summary += "\n" + "\n".join(links)
        summary += f"\n⏱ {fmt_time(int(time.time() - t_start))}"
        # in batch mode the per-link completion is attached to the batch
        # log (with its Drive buttons when mirrored); the final batch
        # summary lands as its own message. A single link keeps the
        # classic in-place summary with its buttons.
        if cleanup:
            throttled_edit(client, chat_id, msg_id, summary, markup=file_kb, force=True)
            autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)
        elif file_kb:
            batch_done_kb = file_kb
            try:
                app.edit_message_reply_markup(chat_id, msg_id, reply_markup=batch_done_kb)
            except Exception as e:
                logger.warning(f"batch per-link buttons skipped: {e}")

    except engine.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except engine.DownloadError as e:
        throttled_edit(client, chat_id, msg_id, f"❌ Couldn't fetch that link:\n<code>{esc(str(e)[:300])}</code>", force=True)
    except Exception as e:
        logger.exception(f"[{task_id}] leech job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ Failed: <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        # was missing (unlike every other job type) — ACTIVE_URLS kept every
        # finished task's URL in RAM forever on this long-running process
        state.drop_url_by_task(task_id)
        if cleanup:
            # batch jobs keep the shared task_dir until the WHOLE batch ends
            # (run_batch_leech_job's own finally does the cleanup)
            shutil.rmtree(task_dir, ignore_errors=True)


# ── Auto-leech: any message containing a link, no command required ─────
# Always leeches to Telegram — use /m if you want it mirrored to Drive.

@app.on_message(filters.text & Authorized & NotCommand & ~filters.via_bot)
@guarded
def auto_leech(client, message):
    text = message.text or ""

    pending = state.pop_awaiting_input(message.chat.id)
    if pending:
        kind, target = pending
        if kind == "cookie_rename":
            return handle_cookie_rename(client, message, target)
        if kind == "drive_acct_name":
            from .handlers_driveauth import start_device_login
            return start_device_login(client, message, (message.text or "").strip())
        if isinstance(kind, tuple) and kind[0] == "drive_token":
            from .handlers_driveauth import _handle_token_input
            return _handle_token_input(client, message, kind[1])
        if isinstance(kind, tuple) and kind[0] == "drive_auth_code":
            from .handlers_driveauth import _exchange_auth_code
            return _exchange_auth_code(client, message, kind[1], message.text or "")
        if isinstance(kind, tuple) and kind[0] == "drive_rename":
            from .handlers_drive import handle_drive_rename
            return handle_drive_rename(client, message, kind[1], target)
        if kind == "drive_newfolder":
            from .handlers_drive import handle_drive_new_folder
            return handle_drive_new_folder(client, message, target)
        if kind == "drive_page":
            from .handlers_drive import handle_drive_page_jump
            return handle_drive_page_jump(client, message, target)
        if kind == "unzipmulti_collect":
            from .handlers_zip import handle_multi_collect
            return handle_multi_collect(client, message, target)

    # Live shell owns plain text while a session is open (admin only) —
    # this is what makes /sh a continuous terminal instead of one-shot runs.
    from .handlers_shell import shell_feed_or_none
    if shell_feed_or_none(client, message.chat.id,
                          message.from_user.id if message.from_user else 0, text):
        return

    magnet = config.MAGNET_RE.search(text)
    if magnet:
        logger.info(f"chat {message.chat.id} auto-leech: magnet link")
        return dispatch_link(client, message, magnet.group(0), "telegram")

    m = URL_RE.search(text)
    if not m:
        return
    url = m.group(0).rstrip(").,]>\"'")
    logger.info(f"chat {message.chat.id} auto-leech: {url}")

    # Same grammar as /m: "| name" rename and "#folder" Drive routing.
    # Media pages get the quality picker exactly like /l (never a silent
    # degraded 360p download) — but Instagram is checked FIRST and always
    # goes to gallery-dl: instagram.com also sits in YTDLP_AUTO_HOSTS, so
    # without this check the bare paste was shadowed into the yt-dlp
    # picker (which then dies with "No video formats found!").
    from .utils import parse_payload, split_ytdlp_format
    _, rename, folder = parse_payload(text)
    text, ytdlp_format = split_ytdlp_format(text)
    host = _host_of(url)
    # Google Drive hosts route through dispatch_link's Drive branch — they
    # must NEVER reach the yt-dlp picker (a drive.usercontent.google.com
    # download link used to get grabbed by yt-dlp's generic extractor,
    # which was the "why is a Drive link managed by yt-dlp" report)
    if host in ("drive.google.com", "drive.usercontent.google.com", "docs.google.com"):
        return dispatch_link(client, message, url, "telegram", rename=rename, folder=folder,
                             format_selector=ytdlp_format)
    if any(host == h or host.endswith("." + h) for h in config.IG_HOSTS):
        return dispatch_link(client, message, url, "telegram", rename=rename, folder=folder)
    is_media_page = any(host == h or host.endswith("." + h) for h in config.YTDLP_AUTO_HOSTS)
    if is_media_page:
        if ytdlp_format:
            return dispatch_link(client, message, url, "telegram", rename=rename,
                                 folder=folder, format_selector=ytdlp_format)
        return show_quality_picker(client, message, url, "telegram", False,
                                    rename=rename, folder=folder)
    dispatch_link(client, message, url, "telegram", rename=rename, folder=folder,
                  format_selector=ytdlp_format)
