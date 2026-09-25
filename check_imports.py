"""Build-time check, run as the `node` user: proves the venv interpreter is
executable by the runtime user AND reports whether fast crypto is present."""
import shutil
import pyrogram  # noqa: F401  (2.x import itself is the 3.14-compat proof)

print("pyrogram ok")
try:
    import tgcrypto  # noqa: F401
except ImportError:
    print("TGCRYPTO=missing-pure-python-fallback")
else:
    print("TGCRYPTO=fast")
for bin_name in ("gallery-dl", "yt-dlp"):
    print(f"{bin_name}={'found' if shutil.which(bin_name) else 'MISSING'}")
