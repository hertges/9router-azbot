"""Startup guard against TgCrypto segfaults.

TgCrypto is a C extension; on some container hosts (musl/glibc mismatches,
buildpack Python ABI drift, low-memory nodes) it can randomly SEGFAULT the
whole bot mid-transfer (exit 139). Pyrogram cannot survive that, and the
exception system can't catch a signal — so the only defense is to test the
extension in a throwaway subprocess BEFORE Pyrogram loads it, and block its
import if it fails. Pyrogram then silently falls back to its built-in
pure-Python crypto: slower uploads/downloads, but immune to the crash.

Env switches:
  DISABLE_TGCRYPTO=1     skip the test, always run pure-Python
  TGCRYPTO_SELFTTEST=0   skip the test, always trust TgCrypto

v16: install() is idempotent and is invoked from bot_pkg/__init__.py — a
package's __init__ always runs before any submodule, so no module can ever
import pyrogram (and thereby bind tgcrypto) ahead of the guard again.
v15 ran the guard only inside core.py, AFTER `from pyrogram import Client`;
any import of a handler module first voided the entire segfault defense.
"""
import os, sys, subprocess

_log = lambda msg: print(f"[crypto-guard] {msg}", flush=True)

_decided = None   # memoized install() result — safe to call repeatedly


def install():
    """Decide whether TgCrypto may load, BEFORE pyrogram is imported.
    Idempotent: the decision is made once per process (bot_pkg/__init__
    calls it before any submodule can import pyrogram; core.py calls it
    again defensively)."""
    global _decided
    if _decided is not None:
        return _decided
    _decided = _install_impl()
    return _decided


def _install_impl():
    if "pyrogram" in sys.modules:
        # Defense-in-depth diagnostic: this means some module imported
        # pyrogram before the guard ran (v15 shipped that bug in core.py).
        # Blocking now cannot unload the copy of tgcrypto pyrogram already
        # bound, so say so LOUDLY — the segfault protection is void.
        _log("WARNING: pyrogram was imported BEFORE crypto-guard could run — "
             "TgCrypto protection is VOID in this process. Fix the import "
             "order (the guard must run from bot_pkg/__init__.py, before "
             "any pyrogram import).")
    if os.environ.get("DISABLE_TGCRYPTO", "").strip() in ("1", "true", "yes"):
        sys.meta_path.insert(0, _ImportBlocker())
        _log("DISABLE_TGCRYPTO set — running pure-Python crypto")
        return False

    if os.environ.get("TGCRYPTO_SELFTTEST", "1").strip() in ("0", "false", "no"):
        _log("self-test skipped — trusting TgCrypto")
        return True

    # Fast pre-check: not installed at all -> nothing to guard.
    try:
        import importlib.util
        if importlib.util.find_spec("tgcrypto") is None:
            _log("tgcrypto not installed — pure-Python crypto (fine)")
            return False
    except Exception:
        pass

    try:
        r = subprocess.run(
            [sys.executable, "-c", _stress_code()],
            capture_output=True, text=True, timeout=90,
        )
        crashed = (r.returncode != 0)          # segfault => negative (e.g. -11)
        ok = (not crashed) and "CRYPTO_OK" in r.stdout
    except subprocess.TimeoutExpired:
        r = None
        crashed, ok = True, False

    if crashed or not ok:
        sys.meta_path.insert(0, _ImportBlocker())
        why = f"exit={r.returncode}" if r is not None else "timeout"
        _log(f"TgCrypto FAILED its self-test ({why}) — blocking it. "
             f"Pyrogram will use pure-Python crypto (stable, ~2-3x slower crypto).")
        return False

    _log("TgCrypto passed self-test — using native crypto")
    return True


def _stress_code():
    """Runs in a subprocess using THIS interpreter + environment, exercising
    pyrogram's own AES wrappers (same call path the bot uses at runtime).

    v16-and-earlier BUG: this called aes.ige_encrypt / ige_decrypt /
    ctr256 — function names that DON'T EXIST in pyrogram (the real ones
    are ige256_encrypt/ige256_decrypt/ctr256_encrypt, and ctr256_* returns
    a single bytes object, not a tuple). Fixed against pyrogram.crypto.aes's
    actual surface in v17.

    v17-and-earlier BUG (still present until now): ige256_* needs a
    32-byte IV, but ctr256_* is keyed by a 16-byte counter/IV — tgcrypto's
    C extension enforces this strictly and raises
    ValueError('IV size must be exactly 16 bytes') if handed the same
    32-byte iv used for the IGE calls. That ValueError made this self-test
    exit 1 on EVERY host where tgcrypto was installed, so the guard
    dutifully blocked perfectly healthy native crypto — forcing the
    ~2-3x slower pure-Python fallback that made uploads crawl. Each
    cipher now gets an IV of its own correct size."""
    return (
        "import os, sys\n"
        "from pyrogram.crypto import aes\n"
        "for size in (16, 1024, 65536, 1 << 20):\n"
        "    data = os.urandom(size); key = os.urandom(32)\n"
        "    ige_iv = os.urandom(32)\n"
        "    ct = aes.ige256_encrypt(data, key, ige_iv)\n"
        "    assert aes.ige256_decrypt(ct, key, ige_iv) == data, 'ige mismatch'\n"
        "    ctr_iv = bytearray(os.urandom(16))\n"
        "    c = aes.ctr256_encrypt(data, key, ctr_iv)\n"
        "    assert isinstance(c, bytes) and len(c) == len(data), 'ctr mismatch'\n"
        "print('CRYPTO_OK')\n"
    )


class _ImportBlocker:
    """Meta-path finder that makes `import tgcrypto` raise ImportError."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "tgcrypto":
            raise ImportError(
                "tgcrypto disabled by crypto-guard (failed startup self-test)")
        return None


def enabled():
    """True if tgcrypto is currently importable (post-install decision)."""
    try:
        import tgcrypto  # noqa
        return True
    except Exception:
        return False
