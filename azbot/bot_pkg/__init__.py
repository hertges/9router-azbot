"""Package init — LOAD-BEARING.

Every handler module does `from pyrogram import filters` at module import
time, and pyrogram.crypto.aes does `import tgcrypto` at ITS import time.
Whichever submodule Python happens to load FIRST therefore decides whether
the (potentially segfaulting) native crypto extension binds before the
crash-guard can veto it — v15 shipped exactly that race (core.py guarded
itself, but importing e.g. handlers_core first still voided the guard,
which is the most plausible cause of the mid-archive process deaths).

Running install() HERE closes the hole permanently: a package's __init__
always executes before any of its submodules, so by the time ANY module
reaches `from pyrogram import ...` the guard has already decided whether
tgcrypto may load. install() is idempotent (see crypto_guard), so core.py
keeping its own call is harmless.
"""
from . import crypto_guard
crypto_guard.install()
