import threading, time, os, sys, ctypes

# ══ IMPORT ORDER HERE IS LOAD-BEARING ════════════════════════════════════
# crypto_guard.install() MUST run before pyrogram is imported anywhere in
# this process. pyrogram.crypto.aes does `import tgcrypto` AT MODULE IMPORT
# TIME, so a guard that runs after `from pyrogram import Client` can no
# longer prevent the (potentially segfaulting) C extension from binding —
# it can only raise ImportError into an already-loaded module. v15 had
# exactly that broken order, making the whole exit-139 defense inert; that
# is the most plausible root cause of the mid-archive process deaths
# ("it just restarts"): heavy MTProto crypto load during a group-topic
# archive hits the buggy native code the guard was supposed to fence off.
#
# config imports only os/re/json/dotenv — safe before pyrogram.
from . import config

# ── Crash guards — must run BEFORE anything imports tgcrypto/pyrogram ───
# 1) faulthandler: if native code segfaults again (exit 139), dump the C
#    and Python stack traces to a file we can actually read afterwards.
try:
    import faulthandler
    os.makedirs(config.LOG_DIR, exist_ok=True)
    _fault_log = open(os.path.join(config.LOG_DIR, "crash.log"), "a", buffering=1)
    faulthandler.enable(file=_fault_log, all_threads=True)
except Exception:
    pass

# 2) TgCrypto guard: self-test the C extension in a subprocess; block it if
#    it's the segfaulting kind (pure-Python fallback keeps the bot alive).
from . import crypto_guard
crypto_guard.install()

# ── only NOW may pyrogram (and therefore tgcrypto) load ──────────────────
from pyrogram import Client
from pyrogram.enums import ParseMode

from . import state, log

# pyrogram's Client() touches asyncio.get_event_loop() at construction
# time; on Python 3.10+ that raises RuntimeError when no loop exists yet
# (3.12 deprecation, removed auto-creation in 3.13+). Docker pins 3.12
# where it still worked — but any 3.13 host would crash at import.
import asyncio
try:
    _loop = asyncio.get_event_loop()
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

log.setup()
logger = log.get(__name__)

def _acquire_single_instance_lock():
    """Refuses to start if another instance of this bot is already running
    on the same data volume — the classic cause of every command getting
    answered twice (a redeploy that didn't cleanly stop the old process,
    two containers sharing one BOT_TOKEN, etc.). An in-process dedup guard
    can't catch this since each process has its own memory; this uses a
    real OS-level file lock on data/, so it also works across containers
    that mount the same volume."""
    lock_path = os.path.join(config.DATA_DIR, "azleechbot.lock")
    try:
        import fcntl
        fh = open(lock_path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh  # keep a reference for the life of the process — closing/GC releases the lock
    except ImportError:
        logger.warning("fcntl not available (non-POSIX host?) — skipping single-instance lock")
        return None
    except OSError:
        logger.error(
            f"Another instance already holds {lock_path} — refusing to start. "
            "This is almost always why commands get answered twice: two bot "
            "processes polling the same BOT_TOKEN. Stop the other one first."
        )
        print(f"CRITICAL: another instance is already running (lock: {lock_path}). Exiting.", file=sys.stderr)
        sys.exit(1)

_lock_fh = None  # kept alive for the process lifetime — see _acquire_single_instance_lock

app = Client(
    "azleechbot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=config.DATA_DIR,
    # Global HTML parse mode (the WZML-X approach). Markdown's delimiter
    # auto-balancing produced entities that overrun the message length
    # (Telegram error 400 ENTITY_BOUNDS_INVALID) whenever dynamic content —
    # filenames, log tails, URLs with backticks/brackets — shifted a
    # delimiter. HTML has no auto-balancing: what you escape is what gets
    # parsed, so entity bounds always match the text.
    parse_mode=ParseMode.HTML,
    # Default is 1 — meaning file parts upload/download serially over a
    # single connection. Raising this lets Pyrogram push multiple parts of
    # the same file in parallel, which is the single biggest lever for
    # large-file Telegram upload/download speed.
    max_concurrent_transmissions=8,
)

def _bg_worker(worker_id):
    logger.debug(f"worker-{worker_id} started")
    while True:
        fn, args = state.task_queue.get()
        try:
            logger.debug(f"worker-{worker_id} picked up {fn.__name__}")
            fn(*args)
        except Exception:
            logger.exception(f"worker-{worker_id} job {fn.__name__} raised")
        except BaseException as e:
            # A SystemExit/RecursionError-style escape would kill the whole
            # interpreter (container restart). Log it with a faulthandler
            # dump and keep the worker alive.
            logger.critical(f"worker-{worker_id} job {fn.__name__} raised "
                            f"BaseException: {type(e).__name__}: {e}", exc_info=True)
        finally:
            state.task_queue.task_done()
            # after EVERY job: hand freed pages back to the OS while RSS is
            # at its peak — the ideal trim moment (better than a periodic
            # sweep, which trims long after the peak anyway)
            release_ram()

def _start_workers():
    for i in range(config.QUEUE_WORKERS):
        threading.Thread(target=_bg_worker, args=(i,), daemon=True).start()
    logger.info(f"started {config.QUEUE_WORKERS} queue workers")

def release_ram():
    """Frees Python garbage and returns glibc arena pages to the OS.
    Called after EVERY job finishes (and still on the 10-min sweep) —
    post-job is the ideal moment: RSS is at its peak right then, and the
    freed pages are exactly the ones the job was using. Without the trim,
    RSS climbs to the high-water mark and stays there."""
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # non-glibc / non-Linux — trim unavailable, harmless

def _purge_loop():
    # Never die: one unhandled exception here used to kill the purge
    # thread permanently, letting stale dirs pile up until disk fills.
    while True:
        try:
            _purge_once()
        except Exception:
            logger.exception("purge pass failed — retrying in 10 min")
        time.sleep(600)


def _purge_once():
        before = state.disk_free_mb()
        state.purge_stale()
        after = state.disk_free_mb()
        if after != before:
            logger.info(f"purged stale downloads, freed {after - before} MB")
        # safety net for idle drift (websocket buffers etc.) between jobs
        release_ram()

def start():
    global _lock_fh
    _lock_fh = _acquire_single_instance_lock()
    # Handler bodies run in utils.AZ_HANDLER_POOL (explicit 16-thread pool —
    # see utils.guarded). Nothing to tune on the asyncio default executor
    # anymore; this comment documents where the capacity lives.
    # NOTE: no yt-dlp auto-update here anymore — restart the bot to pick up
    # a newer yt-dlp (update the image or run pip in the container first).
    # Restore EVERYTHING user-configured from the Drive datastore on a fresh
    # deploy: settings, users (access control), cookies, OAuth tokens.
    try:
        from . import datastore
        pulled = datastore.restore_all_local()
        if pulled:
            logger.info(f"datastore: restored {', '.join(pulled)}")
        import importlib
        importlib.reload(state)
    except Exception as e:
        logger.warning(f"datastore settings restore skipped: {e}")
    # Telegram-registered Drive accounts (overrides.json) over env ones.
    try:
        from . import handlers_driveauth
        handlers_driveauth._load_overrides_into_config()
    except Exception as e:
        logger.warning(f"drive overrides load skipped: {e}")
    from . import (handlers_core, handlers_admin, handlers_shell, handlers_subproc,
                   handlers_instagram, handlers_drive, handlers_zip,
                   handlers_driveauth, handlers_fallback)  # noqa: F401
    logger.info("handlers registered: core, admin+shell, subproc, instagram+zip, drive+auth, fallback")
    try:
        from . import build_info
        logger.info(f"build: {build_info.build_line()} — if this stamp doesn't move "
                    f"after a redeploy, the old process is still running")
    except Exception as e:
        logger.warning(f"build stamp skipped: {e}")
    _start_workers()
    threading.Thread(target=_purge_loop, daemon=True).start()
    # one trim right after boot: import-time garbage (unused dunder caches,
    # dead code objects) goes back to the OS before the first idle reading
    release_ram()
    logger.info("AzLeechBot starting (Pyrogram, native >50MB uploads)…")
    app.run()
