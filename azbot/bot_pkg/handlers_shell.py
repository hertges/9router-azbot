"""Live persistent shell terminal over Telegram (admin only).

Instead of one-shot subprocess runs, this opens ONE real bash session in a
PTY (pseudo-terminal) that keeps living between messages:

  /sh            → open the live terminal (or show it again)
  <any text>     → executed in that same shell (cd, exports, env all persist;
                   bash readline gives ↑/↓ history, ←/→ editing, TAB completion)
  /shc           → send Ctrl+C to the running command
  /shexit /shq   → end the session

Output streams into a single Telegram message that keeps updating (throttled,
flood-safe). Because it's a real PTY, everything bash offers interactively
works exactly like SSH-lite.

Routing notes (pyrogram dispatch is first-match-wins per group):
  • Non-slash text while a session is active is intercepted inside
    handlers_core.auto_leech via shell_feed_or_none() — that handler owns
    the "plain text" match, so there is no catch-all competition.
  • Slash commands never reach the shell; they stay bot commands.
"""
import os, re, time, pty, threading, signal, select, subprocess

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import config, log
from .utils import esc, throttled_edit
logger = log.get(__name__)

SHELL_COMMAND_NAMES = ("sh", "shell")

# ── tunables ──────────────────────────────────────────────────────────────
OUTPUT_TICK_S = 0.9        # how often the live-output message refreshes
SNAPSHOT_CHARS = 3000      # max chars of terminal tail shown per edit
BUFFER_CHARS = 16000       # rolling terminal buffer kept in RAM
IDLE_TIMEOUT_S = 1800     # auto-close after 30 idle minutes
READ_CHUNK = 4096

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[BDEHMc789=>]|\x07")
# readline's backspace echo: pair + the character it erases
_BS_ERASE_RE = re.compile(r"(.)\x08 \x08", re.S)
# Any remaining C0/C1 control char (except \n) and any non-BMP char —
# Telegram's entity offsets are UTF-16 code units; astral-plane chars count
# as 2 and desync our computed bounds → ENTITY_BOUNDS_INVALID.
_CTRL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\U00010000-\U0010ffff]")

def _clean(text):
    text = text.replace("\r\n", "\n").replace("\r", "")
    # Model readline's backspace echo properly: each "\x08 \x08" pair erases
    # the PRECEDING character on a real terminal, so the pair plus that char
    # must all be removed (shell-bot parity for the ⌫ button).
    while True:
        m = _BS_ERASE_RE.search(text)
        if not m:
            break
        text = text[:m.start()] + text[m.end():]
    text = _ANSI_RE.sub("", text)
    # Final safety net: any control/astral char that survived would break
    # Telegram's entity bounds on edit — drop them.
    return _CTRL_RE.sub("", text)


def _is_admin_user(user_id):
    return bool(config.ADMIN_ID) and str(user_id) == config.ADMIN_ID


def _descendant_pgids(root_pid):
    """All distinct process-group IDs under root_pid (inclusive), walked via
    /proc — lets us Ctrl+C whichever job bash put in the foreground."""
    info = {}   # pid -> (ppid, pgid)
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat", "rb") as fh:
                    stat = fh.read()
                # ppid/pgid are fields 4/5; comm (field 2) may contain spaces
                # or parens — split after the last ')'.
                fields = stat[stat.rfind(b")") + 2:].split()
                info[int(pid)] = (int(fields[1]), int(fields[2]))
            except Exception:
                continue
    except Exception:
        pass

    pgids, stack = set(), [root_pid]
    while stack:
        cur = stack.pop()
        entry = info.get(cur)
        if entry:
            pgids.add(entry[1])
        stack.extend(pid for pid, (ppid, _) in info.items() if ppid == cur)
    return pgids


class ShellSession:
    def __init__(self, client, chat_id, canvas_msg_id):
        self.client = client
        self.chat_id = chat_id
        self.canvas = canvas_msg_id          # the message we keep repainting
        self.buf = ""                        # rolling terminal output
        self.lock = threading.Lock()
        self.exited = False                  # bash has quit
        self.closed = False                  # fully torn down
        self.last_activity = time.time()
        self.last_painted = None

        master, slave = pty.openpty()
        self.master = master
        env = dict(os.environ,
                   TERM="xterm-256color",
                   LANG="C.UTF-8",
                   HISTFILE="/dev/null",     # don't persist bot shell history to disk
                   PS1="\\w $ ")
        # start_new_session → bash becomes its own session/group leader;
        # the slave side of the pty is its stdin/stdout/stderr.
        self.proc = subprocess.Popen(
            ["bash", "--norc", "-i"],
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, env=env,
        )
        os.close(slave)  # keep only the master in the bot process

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._painter = threading.Thread(target=self._paint_loop, daemon=True)
        self._reader.start()
        self._painter.start()

    # -- threads -------------------------------------------------------------
    def _read_loop(self):
        def _drain_once():
            r, _, _ = select.select([self.master], [], [], 0.5)
            if not r:
                return True  # nothing to read — keep looping
            try:
                chunk = os.read(self.master, READ_CHUNK)
            except OSError:
                return False
            if not chunk:
                return False
            with self.lock:
                self.buf = (self.buf + chunk.decode("utf-8", "replace"))[-BUFFER_CHARS:]
            return True

        try:
            while not self.closed:
                if not _drain_once():
                    break
                if self.proc.poll() is not None:
                    # Bash exited — drain what's left, then finish.
                    deadline = time.time() + 1.0
                    while time.time() < deadline:
                        r, _, _ = select.select([self.master], [], [], 0.1)
                        if not r:
                            continue
                        try:
                            chunk = os.read(self.master, READ_CHUNK)
                        except OSError:
                            break
                        if not chunk:
                            break
                        with self.lock:
                            self.buf = (self.buf + chunk.decode("utf-8", "replace"))[-BUFFER_CHARS:]
                    break
        except Exception:
            logger.exception("shell reader thread crashed")
        finally:
            self.exited = True

    def _keyboard(self):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("▲", callback_data="shup"),
            InlineKeyboardButton("Tab", callback_data="shtab"),
            InlineKeyboardButton("▼", callback_data="shdown"),
        ], [
            InlineKeyboardButton("⌫ Backspace", callback_data="shbs"),
            InlineKeyboardButton("⛔ Ctrl+C", callback_data="shctrlc"),
            InlineKeyboardButton("⏎ Enter", callback_data="shenter"),
        ], [
            InlineKeyboardButton("Ctrl+D (EOF)", callback_data="shcd"),
            InlineKeyboardButton("⏹ Exit shell", callback_data="shexit"),
        ]])

    def _snapshot(self):
        with self.lock:
            return _clean(self.buf)[-SNAPSHOT_CHARS:]

    def _paint_loop(self):
        kb = self._keyboard()
        try:
            while not self.closed:
                time.sleep(OUTPUT_TICK_S)
                if self.exited or self.closed:
                    break
                if time.time() - self.last_activity > IDLE_TIMEOUT_S:
                    self.stop("⏱️ Terminal closed — idle for 30 minutes.")
                    return
                self._repaint(kb, force=False)
            if not self.closed:
                self._repaint(kb, force=True)
        finally:
            # Only unregister ourselves — a newer session may already own this chat.
            with _sess_lock:
                if SESSIONS.get(self.chat_id) is self:
                    SESSIONS.pop(self.chat_id, None)

    def _repaint(self, kb, force=False):
        snap = self._snapshot()
        if not force and snap == self.last_painted:
            return
        self.last_painted = snap
        status = "🟢 live" if not self.exited else "⚫ ended"
        head = (f"🖥 <b>Live shell</b> ({status}) — type any command below • "
                f"/shc = Ctrl+C • /shexit = quit\n")
        throttled_edit(self.client, self.chat_id, self.canvas,
                       head + f"<pre>{esc(snap) or ' '}</pre>",
                       markup=None if self.exited else kb, force=force)

    # -- input ---------------------------------------------------------------
    def feed(self, line):
        if self.exited or self.closed:
            return False
        self.last_activity = time.time()
        try:
            os.write(self.master, line.encode() + b"\n")
            return True
        except OSError:
            return False

    def ctrl_c(self):
        """Best-effort Ctrl+C: poke readline with 0x03 over the pty AND
        SIGINT every descendant process group (covers long-running jobs
        bash put in their own foreground group)."""
        if self.exited or self.closed:
            return False
        self.last_activity = time.time()
        try:
            os.write(self.master, b"\x03")
        except OSError:
            pass
        killed = 0
        for pgid in _descendant_pgids(self.proc.pid):
            if pgid == self.proc.pid:   # bash's own group: skip — SIGINT on
                continue                # idle bash would kill the session
            try:
                os.killpg(pgid, signal.SIGINT)
                killed += 1
            except Exception:
                pass
        return True

    def history_nav(self, direction):
        """▲ recalls the previous command into the line editor (like pressing
        the Up arrow); ▼ moves back down. Sent as raw terminal escape codes
        over the PTY so bash's real readline handles them."""
        if self.exited or self.closed:
            return False
        self.last_activity = time.time()
        try:
            os.write(self.master, b"\x1b[A" if direction == "up" else b"\x1b[B")
            return True
        except OSError:
            return False

    def send_key(self, key):
        """Extra terminal keys for the button row: Tab (completion),
        Backspace, Enter, Ctrl+D (EOF/logout)."""
        if self.exited or self.closed:
            return False
        self.last_activity = time.time()
        codes = {"tab": b"\t", "bs": b"\x7f", "enter": b"\n", "cd": b"\x04"}
        try:
            os.write(self.master, codes[key])
            return True
        except (OSError, KeyError):
            return False

    def cursor(self, direction):
        """◀ ▶ move the caret within the current input line."""
        if self.exited or self.closed:
            return False
        try:
            os.write(self.master, b"\x1b[D" if direction == "left" else b"\x1b[C")
            return True
        except OSError:
            return False

    def stop(self, reason=""):
        if self.closed:
            return
        self.closed = True
        # NOTE: interactive bash IGNORES SIGTERM, so terminate() alone never
        # dies on its own — always follow through to SIGKILL on its group.
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            pass
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            logger.warning(f"shell pid {self.proc.pid} refused to die")
        try:
            os.close(self.master)
        except OSError:
            pass
        if reason:
            try:
                # Anchor to self.canvas (the live shell message, already in
                # the right forum topic) — WZML-X-style, same convention as
                # every other new-message send in this bot.
                self.client.send_message(self.chat_id, reason, reply_to_message_id=self.canvas)
            except Exception:
                pass


SESSIONS = {}   # chat_id -> ShellSession
_sess_lock = threading.Lock()
_shell_warned = set()   # chats already told their text is eaten by an open shell


def shell_feed_or_none(client, chat_id, user_id, text):
    """Called from auto_leech for every non-command text message.
    Returns True if the text was consumed as shell input."""
    with _sess_lock:
        sess = SESSIONS.get(chat_id)
    if not sess:
        return False
    if sess.exited:
        _drop(chat_id)
        return False
    if not _is_admin_user(user_id):
        # Other authorized users in the same chat must not drive the shell —
        # but tell them ONCE per session instead of silently eating their
        # leech links (messages used to vanish without any feedback).
        if chat_id not in _shell_warned:
            _shell_warned.add(chat_id)
            try:
                client.send_message(chat_id, "⚠️ A shell session is open in this chat — your message was ignored. Close it with /shexit or send your link elsewhere.")
            except Exception:
                pass
        return True  # swallow rather than leeching into someone's terminal
    ok = sess.feed(text)
    if not ok:
        _drop(chat_id)
    return True


def _drop(chat_id):
    with _sess_lock:
        sess = SESSIONS.pop(chat_id, None)
    _shell_warned.discard(chat_id)
    if sess is not None:
        try:
            sess.stop("dropped")
        except Exception:
            pass


def register_shell_handlers(app, AdminOnly, guarded, on_one_shot):

    @app.on_message(filters.regex(r"^/(?:sh|shell)(?:@\w+)?\s*$") & AdminOnly)
    @guarded
    def sh_open(client, message):
        chat_id = message.chat.id
        with _sess_lock:
            sess = SESSIONS.get(chat_id)
        if sess and not sess.exited:
            sess._repaint(sess._keyboard(), force=True)
            return message.reply_text(
                "ℹ️ Live shell is already running above — just type commands "
                "(any non-command text goes to the terminal). <code>/shexit</code> to quit.")
        if sess:
            sess.stop()
            _drop(chat_id)
        status = message.reply_text("🖥 Opening live shell…")
        try:
            sess = ShellSession(client, chat_id, status.id)
        except Exception as e:
            logger.exception("failed to open PTY shell")
            return throttled_edit(client, chat_id, status.id,
                                   f"❌ Couldn't open a PTY: <code>{esc(e)}</code>", force=True)
        with _sess_lock:
            SESSIONS[chat_id] = sess
        logger.info(f"live shell opened in chat {chat_id} (pid {sess.proc.pid})")

    @app.on_message(filters.command("shc") & AdminOnly)
    @guarded
    def sh_ctrl_c_cmd(client, message):
        with _sess_lock:
            sess = SESSIONS.get(message.chat.id)
        if not sess or sess.exited:
            return message.reply_text("ℹ️ No live shell running — open one with <code>/sh</code>.")
        sess.ctrl_c()
        message.reply_text("⛔ Sent Ctrl+C.")

    @app.on_message(filters.command(["shexit", "shq"]) & AdminOnly)
    @guarded
    def sh_exit_cmd(client, message):
        with _sess_lock:
            sess = SESSIONS.pop(message.chat.id, None)
        if not sess:
            return message.reply_text("ℹ️ No live shell running.")
        sess.stop()
        message.reply_text("⏹ Shell session ended.")

    @app.on_callback_query(filters.regex(r"^shctrlc$"))
    @guarded
    def sh_ctrl_c_cb(client, cq):
        if not _is_admin_user(cq.from_user.id):
            return cq.answer("Admin only.", show_alert=True)
        with _sess_lock:
            sess = SESSIONS.get(cq.message.chat.id)
        if not sess:
            return cq.answer("No live shell.", show_alert=True)
        sess.ctrl_c()
        cq.answer("⛔ Ctrl+C sent")

    @app.on_callback_query(filters.regex(r"^shup$"))
    @guarded
    def sh_up_cb(client, cq):
        if not _is_admin_user(cq.from_user.id):
            return cq.answer("Admin only.", show_alert=True)
        with _sess_lock:
            sess = SESSIONS.get(cq.message.chat.id)
        if not sess:
            return cq.answer("No live shell.", show_alert=True)
        sess.history_nav("up")
        cq.answer("▲ previous command")

    @app.on_callback_query(filters.regex(r"^shdown$"))
    @guarded
    def sh_down_cb(client, cq):
        if not _is_admin_user(cq.from_user.id):
            return cq.answer("Admin only.", show_alert=True)
        with _sess_lock:
            sess = SESSIONS.get(cq.message.chat.id)
        if not sess:
            return cq.answer("No live shell.", show_alert=True)
        sess.history_nav("down")
        cq.answer("▼ next command")

    @app.on_callback_query(filters.regex(r"^shtab$"))
    @guarded
    def sh_tab_cb(client, cq):
        if not _is_admin_user(cq.from_user.id):
            return cq.answer("Admin only.", show_alert=True)
        with _sess_lock:
            sess = SESSIONS.get(cq.message.chat.id)
        if not sess:
            return cq.answer("No live shell.", show_alert=True)
        sess.send_key("tab")
        cq.answer("Tab — completion")

    @app.on_callback_query(filters.regex(r"^shbs$"))
    @guarded
    def sh_bs_cb(client, cq):
        if not _is_admin_user(cq.from_user.id):
            return cq.answer("Admin only.", show_alert=True)
        with _sess_lock:
            sess = SESSIONS.get(cq.message.chat.id)
        if not sess:
            return cq.answer("No live shell.", show_alert=True)
        sess.send_key("bs")
        # Backspace over a pty is echoed by readline as "\b \b" — give the
        # reader a beat to absorb it, then force the canvas repaint so the
        # shortened line is visible immediately.
        time.sleep(0.25)
        sess._repaint(sess._keyboard(), force=True)
        cq.answer("⌫")

    @app.on_callback_query(filters.regex(r"^shenter$"))
    @guarded
    def sh_enter_cb(client, cq):
        if not _is_admin_user(cq.from_user.id):
            return cq.answer("Admin only.", show_alert=True)
        with _sess_lock:
            sess = SESSIONS.get(cq.message.chat.id)
        if not sess:
            return cq.answer("No live shell.", show_alert=True)
        sess.send_key("enter")
        cq.answer("⏎")

    @app.on_callback_query(filters.regex(r"^shcd$"))
    @guarded
    def sh_cd_cb(client, cq):
        """Ctrl+D on an empty line ends the shell (like typing exit)."""
        if not _is_admin_user(cq.from_user.id):
            return cq.answer("Admin only.", show_alert=True)
        with _sess_lock:
            sess = SESSIONS.pop(cq.message.chat.id, None)
        if not sess:
            return cq.answer("No live shell.", show_alert=True)
        try:
            sess.send_key("cd")
        except Exception:
            pass
        time.sleep(0.5)
        sess.stop()
        cq.answer("Ctrl+D — shell closed")

    @app.on_callback_query(filters.regex(r"^shexit$"))
    @guarded
    def sh_exit_cb(client, cq):
        if not _is_admin_user(cq.from_user.id):
            return cq.answer("Admin only.", show_alert=True)
        with _sess_lock:
            sess = SESSIONS.pop(cq.message.chat.id, None)
        if not sess:
            return cq.answer("No live shell.", show_alert=True)
        sess.stop()
        cq.answer("⏹ Shell ended")

    @app.on_message(filters.command(list(SHELL_COMMAND_NAMES)) & AdminOnly)
    @guarded
    def sh_one_shot_redirect(client, message):
        # Reached only when args were given (bare /sh matched sh_open above)
        # — delegate to the legacy one-shot runner for backwards compat.
        return on_one_shot(client, message)

    logger.info("live shell handlers registered")
