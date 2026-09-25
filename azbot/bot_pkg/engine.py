import os, time, re, subprocess, requests, mimetypes

from yt_dlp import YoutubeDL

from . import config, state, log
from .utils import fmtsz, fmt_time, smooth_speed, throttled_edit, cancel_kb, progress_line, esc

logger = log.get(__name__)

CancelledError = state.CancelledError

ARIA2_PROGRESS_RE = re.compile(r"\[#\w+\s+([\d.]+\w+)/([\d.]+\w+)\((\d+)%\).*?DL:([\d.]+\w+)")

_ARIA2_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
                "TIB": 1024 ** 4, "KB": 1000, "MB": 1000 ** 2,
                "GB": 1000 ** 3, "TB": 1000 ** 4}

def _parse_aria2_size(s):
    """'10MiB'/'1.2GiB'/'512B' → bytes. Raises ValueError when unknown."""
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", (s or "").strip())
    if not m:
        raise ValueError(f"bad aria2 size: {s!r}")
    return float(m.group(1)) * _ARIA2_UNITS[m.group(2).upper()]

# Content-Types that mean "this is a webpage, let yt-dlp parse it for an
# embedded video" rather than "this IS the file, just download it".
_PAGE_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

class DownloadError(Exception):
    pass

# yt-dlp auto-update removed (user request — restart the bot to update
# instead; pip install -U yt-dlp in the image/deploy is the update path).
# The 6h in-process updater was deleted: it ran pip (a network + CPU
# spike) and could os.execv-restart the process mid-idle, which looked
# exactly like the random restarts we've been hunting.

def _ydl_base_opts(chat_id, outtmpl):
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "nocheckcertificate": True, "geo_bypass": True,
        "socket_timeout": 30, "retries": 10, "fragment_retries": 10,
        "outtmpl": outtmpl,
        # False, not True: "restrict" mode strips non-ASCII characters,
        # which mangles perfectly valid filenames (Persian/Arabic/etc. in
        # particular) into underscores. Keep the real name; Telegram and
        # the filesystem both handle Unicode fine.
        "restrictfilenames": False,
        "windowsfilenames": False,
        "trim_file_name": 150,
        "http_headers": {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
        },
        # Robust, standard yt-dlp selector: best video+audio (any codec/
        # container) merged, falling back to best combined.
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        # WZML-X parity (verified against their yt_dlp_download.py): they
        # force NEITHER a player_client list NOR an external downloader.
        # v15-v17 did both, and on 2026 YouTube that backfires — pinned
        # clients hit the SABR/PO-token wall and return thin format tables
        # ("only one quality"), while handing googlevideo URLs to aria2c
        # stalls mid-download. yt-dlp ships fixes near-daily; trust its
        # defaults like WZML-X does. YT_DLP_OPTIONS
        # in .env remains the escape hatch for anything site-specific.
        # 8 concurrent fragment connections: fragment buffers are modest
        # (~1MB each) and this is the main DASH-stream download speed
        # lever. The old 4 was an OOM-era revert; every real OOM source
        # (Drive chunk buffers, ungated ffmpeg) is individually gated now.
        "concurrent_fragment_downloads": 8,
        # WZML-X-style fast, bounded retry sleeps instead of yt-dlp's
        # exponential backoff (which looks exactly like "stuck").
        "retry_sleep_functions": {
            "http": lambda n: 3,
            "fragment": lambda n: 3,
            "file_access": lambda n: 3,
            "extractor": lambda n: 3,
        },
        # Black thumbnails: some clients return storyboards instead of real
        # thumbs. Prefer an embedded thumbnail when converting.
        "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
    }
    ck = state.active_cookie_file(chat_id)
    if ck:
        opts["cookiefile"] = ck
    if config.YT_DLP_OPTIONS:
        opts.update(config.YT_DLP_OPTIONS)
    return opts

def probe(url, chat_id):
    """Lightweight metadata-only probe, no download."""
    opts = _ydl_base_opts(chat_id, "%(id)s.%(ext)s")
    opts["skip_download"] = True
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

# ── WZML-X-style format listing ──────────────────────────────────────────
# WZML-X never guesses resolution buckets: it extracts the REAL format table
# and renders one button per usable format (exact size, container, fps),
# then asks which audio track to merge into video-only formats. This module
# mirrors that flow.

def _fmt_size(b):
    if not b:
        return ""
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f}{unit}" if unit == "B" else f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


def _est_bytes(f, duration):
    """Exact size when reported; otherwise bitrate × duration estimate."""
    s = f.get("filesize") or f.get("filesize_approx")
    if s:
        return int(s)
    br = f.get("tbr") or 0
    return int(br * 1000 / 8 * duration) if br and duration else 0


def probe_qualities(url, chat_id, allow_retry_without_cookies=True):
    """Returns (title, options, is_playlist) where options is a list of dicts:
      {label, selector, needs_audio}
    built straight from the extractor's format table — WZML-X style. For
    PLAYLISTS, the menu is built from the first entry that actually has
    formats, and is_playlist=True tells the caller to ALWAYS show the menu
    (silently auto-grabbing a whole playlist as "best" without asking was
    the complaint — one tap on BEST costs nothing).

    v17: every format id gets its own button (v15/v16 deduplicated by
    resolution and hid real options), and if the table comes back with
    only low resolutions while a logged-in cookie profile is active
    (cookies commonly pin yt-dlp to a basic client → 360p-only tables),
    it automatically re-probes WITHOUT cookies once."""
    def _extract():
        info = probe(url, chat_id)
        if info.get("_type") == "playlist":
            # first entry that actually HAS formats (entries[0] can be a
            # deleted/private video with none — that used to trip the
            # "one quality → auto-grab" path and skip asking entirely)
            for e in (info.get("entries") or []):
                if e and e.get("formats"):
                    e["_playlist_title"] = info.get("title") or ""
                    return e, True
        return info, False

    info, is_playlist = _extract()

    def build(info):
        formats = [f for f in (info.get("formats") or [])
                   if f.get("format_id")
                   and f.get("vcodec") != "mhtml"          # storyboards
                   and not (f.get("vcodec") == "none" and f.get("acodec") == "none")]
        duration = info.get("duration") or 0

        # ── video formats: one BUTTON PER REAL FORMAT ID (exact size,
        #    container, codec, fps) sorted high→low bitrate — what WZML-X
        #    shows. v15/16 merged everything into one button per height.
        vids, auds = [], []
        for f in formats:
            if f.get("vcodec", "none") != "none" and f.get("height"):
                vids.append(f)
            elif f.get("acodec", "none") != "none":
                auds.append(f)

        options = [{"label": "⚡ BEST (V+A)", "selector": "bv*+ba/b",
                    "needs_audio": False}]

        def codec_tag(c):
            c = (c or "").split(".")[0]
            return {"av01": "av1", "vp9": "vp9", "vp09": "vp9",
                    "h264": "h264", "mp4a": "aac"}.get(c, c[:4])

        for f in sorted(vids,
                        key=lambda x: (_est_bytes(x, duration) or 0), reverse=True):
            fps = f.get("fps")
            fps_s = f"{fps}@ " if fps and fps not in (25, 30) else ""
            sz = _est_bytes(f, duration)
            tag = codec_tag(f.get("vcodec"))
            note = "" if f.get("acodec") != "none" else " 🔇"
            label = (f"{f['height']}p{fps_s} {f.get('ext', '?')} "
                     f"{_fmt_size(sz)} {tag}{note}").strip()
            options.append({
                "label": label[:60],
                "selector": f["format_id"],
                "needs_audio": f.get("acodec") == "none",
                "vid_id": f["format_id"],
            })

        for f in sorted(auds, key=lambda x: x.get("abr") or 0, reverse=True)[:4]:
            abr = int(f.get("abr") or 0)
            sz = _est_bytes(f, duration)
            options.append({
                "label": f"🎵 {abr}K {f.get('ext', '?')} {_fmt_size(sz)}".strip(),
                "selector": f["format_id"],
                "needs_audio": False,
            })
        return options

    options = build(info)

    # Thin-table retry: only-low-res + an active cookie profile → try once
    # more without it before showing a starved menu. This is the classic
    # reason another bot offers 1080p where we offered 360p.
    heights = {o["label"].split("p")[0] for o in options[1:] if "p" in o["label"] and "🎵" not in o["label"]}
    if (heights
            and max(int(h) for h in heights) <= 480
            and state.active_cookie_file(chat_id)):
        logger.info(f"thin format table ({sorted(heights)}) with active cookie — re-probing without cookies")
        opts = _ydl_base_opts(chat_id, "%(id)s.%(ext)s")
        opts.pop("cookiefile", None)
        opts["skip_download"] = True
        try:
            with YoutubeDL(opts) as ydl:
                info2 = ydl.extract_info(url, download=False)
                if info2.get("_type") == "playlist":
                    for e in (info2.get("entries") or []):
                        if e and e.get("formats"):
                            info2 = e
                            break
            options2 = build(info2)
            h2 = [int(o["label"].split("p")[0]) for o in options2[1:]
                  if "p" in o["label"] and "🎵" not in o["label"]]
            if h2 and max(h2) > max(int(h) for h in heights):
                return info2.get("title", info.get("title", "video")), options2, is_playlist
        except Exception as e:
            logger.warning(f"cookie-less re-probe failed: {e}")

    return info.get("title", "video"), options, is_playlist


def download(url, chat_id, msg_id, task_id, client, dest_dir, format_selector=None,
             on_entry_done=None, progress_prefix_fn=None):
    """Downloads url via yt-dlp into dest_dir, editing the status message
    with progress. format_selector overrides the default "best" pick —
    used by the /yt-family quality picker. Pass "audio" for an
    audio-only extraction (converted to mp3).

    PLAYLISTS: on_entry_done(path) is called the moment each entry is
    fully downloaded — the caller streams it (send immediately, delete
    right after the send) while later entries are still downloading.
    Returns a LIST of paths when on_entry_done is None (paths collected
    for a post-download batch), else None. Deduplicates entries by video
    id (yt-dlp can yield the same video twice, which used to double-send
    an item) and resolves each entry's REAL file (prepare_filename lies
    about extensions post-merge)."""
    os.makedirs(dest_dir, exist_ok=True)
    # ONE template handles both cases: %(playlist_index|)s is empty for
    # single videos (bare "Title [id].ext") and "NN - " for playlist
    # entries — so a playlist never collides names and never produces the
    # ".NA" placeholder prepare_filename used to return.
    outtmpl = os.path.join(
        dest_dir,
        "%(playlist_index|)s%(playlist_index& - |)s%(title).150B [%(id)s].%(ext)s")
    opts = _ydl_base_opts(chat_id, outtmpl)

    if format_selector == "audio":
        opts["format"] = "ba/b"
        opts["postprocessors"] = opts.get("postprocessors", []) + [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    elif format_selector:
        opts["format"] = format_selector

    samples = []
    last_edit = [0.0]

    def hook(d):
        if state.is_cancelled(task_id):
            raise CancelledError("cancelled by user")
        if d["status"] == "downloading":
            now = time.time()
            if now - last_edit[0] < 1.5:
                return
            last_edit[0] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            samples.append((done, now))
            if len(samples) > 6:
                samples.pop(0)
            speed = smooth_speed(samples)
            eta = d.get("eta")
            fname = os.path.basename(d.get("filename") or "")
            # progress_prefix_fn lets batch jobs inject their own header
            # ('📂 Batch — 2/5 • …') so every bar carries batch context —
            # the same look as Drive-folder streaming
            prefix = progress_prefix_fn() if progress_prefix_fn else ""
            if prefix:
                prefix += "\n"
            head = (f"📄 <code>{esc(fname)}</code>\n" if fname else "")
            # playlist position: playlist_index is 1-based for entries, and
            # "playlist" in info_dict is the playlist TITLE (a str) — using
            # it for math is what crashed with "can only concatenate str
            # (not 'int') to str"
            info_d = d.get("info_dict") or {}
            pl_idx = info_d.get("playlist_index")
            n = info_d.get("n_entries")
            pos = f"#{pl_idx} of {n if n else '?'}  " if pl_idx else ""
            text = (
                f"{prefix}⬇️ <b>Downloading</b>\n{head}{pos}{progress_line('⬇️', done, total)}\n"
                f"⚡ {speed}" + (f"  •  ETA {fmt_time(eta)}" if eta else "")
            )
            throttled_edit(client, chat_id, msg_id, text, markup=cancel_kb(task_id))
        elif d["status"] == "finished":
            throttled_edit(client, chat_id, msg_id, "⚙️ Processing…", force=True)

    def _resolve(fpath):
        """prepare_filename often disagrees with post-processed reality
        (merge ext change, mp3 extraction, playlist '.NA' quirk) — find
        the actual file for the same stem."""
        if fpath and os.path.exists(fpath):
            return fpath
        if not fpath:
            return None
        base, _ = os.path.splitext(fpath)
        for ext in (".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".opus"):
            if os.path.exists(base + ext):
                return base + ext
        return None

    def _on_entry(e, p):
        if on_entry_done and p:
            try:
                on_entry_done(p)
            except Exception as ex:
                logger.warning(f"[{task_id}] on_entry_done callback failed: {ex}")

    def _on_pp(d):
        """after_move = the final post-processed file for ONE entry is in
        place (fires once per entry after merge/move — the download
        'finished' event fires once per FORMAT and double-fired merged
        videos). Fires the per-entry callback immediately so the caller
        sends while the rest of the playlist is still downloading.
        Deduped by video id: yt-dlp can yield the same video twice."""
        if d.get("status") != "finished" or d.get("postprocessor") != "MoveFiles":
            return
        info_d = d.get("info_dict") or {}
        eid = str(info_d.get("id") or "")
        if eid and eid in _streamed_ids:
            return
        if eid:
            _streamed_ids.add(eid)
        p = _resolve(info_d.get("filepath") or info_d.get("filename"))
        if p:
            _on_entry(info_d, p)

    _streamed_ids = set()

    logger.info(f"[{task_id}] yt-dlp download starting — {url}")
    try:
        if on_entry_done:
            # CHEAP playlist pre-check (flat extraction — no per-video
            # metadata requests). CRITICAL: postprocessor_hooks must be in
            # opts BEFORE YoutubeDL() is constructed — the constructor
            # consumes them (add_postprocessor_hook runs in __init__ and
            # wires each PP's progress reporting); setting the key after
            # construction is a silent no-op, the hook never fires, and no
            # entry ever streams (the 'NoneType.finalize' crash).
            try:
                flat = YoutubeDL(opts).extract_info(url, download=False, process=False)
                is_playlist = bool(flat) and flat.get("_type") in ("playlist", "multi_video")
            except Exception as e:
                logger.debug(f"[{task_id}] flat pre-check skipped: {e}")
                is_playlist = False
            if is_playlist:
                opts["postprocessor_hooks"] = [_on_pp]
                logger.info(f"[{task_id}] playlist detected — streaming entries as they finish")
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except CancelledError:
        logger.info(f"[{task_id}] yt-dlp download cancelled")
        raise
    except Exception as e:
        logger.warning(f"[{task_id}] yt-dlp download failed — {e}")
        raise DownloadError(str(e))

    # PLAYLIST result handling — TWO modes:
    #   streaming (on_entry_done set): entries were already handed off by
    #     the _on_pp hook during extraction; return None (caller has them).
    #   batch (on_entry_done None — e.g. /zl zip jobs): resolve every
    #     entry's real file and return the list. prepare_filename on the
    #     playlist DICT is the ".NA" garbage — never return it.
    if info.get("_type") == "playlist" or info.get("entries"):
        if on_entry_done:
            logger.info(f"[{task_id}] playlist streamed — {len(_streamed_ids)} file(s)")
            return None
        out, seen_ids = [], set()
        for e in (info.get("entries") or []):
            if not e:
                continue
            eid = str(e.get("id") or "")
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            p = _resolve(ydl.prepare_filename(e)) or _resolve(
                os.path.join(dest_dir, f"{e.get('id')}."))
            if p:
                out.append(p)
        if not out:
            raise DownloadError("playlist had no downloadable entries")
        logger.info(f"[{task_id}] playlist batch done — {len(out)} file(s)")
        return out

    fpath = _resolve(ydl.prepare_filename(info))
    if not fpath:
        raise DownloadError("download reported success but no file was found")
    logger.info(f"[{task_id}] yt-dlp download done — {fpath} ({fmtsz(os.path.getsize(fpath))})")

    # yt-dlp's generic extractor saves direct media links as
    # "generic video #<content-type-junk> [<id>]" — recover the
    # server's real name (Content-Disposition on a tiny ranged GET)
    # and rename. No-op for real sites with real titles.
    try:
        if (str(info.get("extractor_key", "")).lower() == "generic"
                and str(info.get("title", "")).lower().startswith("generic")):
            real = _server_name_via_get(url)
            if real:
                # Hostile servers send '../', slashes or absolute paths
                # here — confine to a bare basename or skip the rename.
                real = os.path.basename(real.replace("\\", "/")).strip()
                if not real or real in (".", ".."):
                    real = None
            if real:
                if not os.path.splitext(real)[1]:
                    real += os.path.splitext(fpath)[1]
                newp = os.path.join(os.path.dirname(fpath), real)
                os.replace(fpath, newp)
                fpath = newp
                info["title"] = os.path.splitext(real)[0]
                logger.info(f"[{task_id}] generic name recovered → {real!r}")
    except Exception as e:
        logger.debug(f"generic rename skipped: {e}")
    return fpath, info

def is_direct_file_link(url):
    lower = url.split("?")[0].lower()
    return lower.endswith(config.DIRECT_FILE_EXT)

# URL path extensions that mean "this is a webpage, not a file" — used ONLY
# when the network probe is inconclusive (slow/dead HEAD+GET) to decide the
# fallback route. Anything else with a trailing extension is treated as a
# file (e.g. fsn1-speed.hetzner.com/10GB.bin: its server never answers the
# probe, which used to misroute it into yt-dlp's generic extractor — whose
# ffmpeg postprocessor then died on the .bin with "Invalid data found").
_PAGE_URL_EXTS = (".html", ".htm", ".xhtml", ".php", ".asp", ".aspx", ".jsp",
                  ".cgi", ".cfm", ".py", ".shtml", ".rhtml")

def looks_like_file_url(url):
    """Extension heuristic for inconclusive probes: the URL PATH ends with
    an extension that isn't a webpage type → treat as a direct file."""
    path = url.split("?")[0].split("#")[0]
    m = re.search(r"\.([A-Za-z0-9]{1,6})$", path)
    if not m:
        return False
    return f".{m.group(1).lower()}" not in _PAGE_URL_EXTS

def probe_direct(url, timeout=15):
    """(filename, is_direct) probe for links with no usable extension.
    Returns (server_filename, True/False) or (None, None) when
    inconclusive. The filename comes from Content-Disposition or, as a
    fallback, the real query-string name many file hosts embed (the
    ?n=name.mp4 pattern — smhdl/telegram-CDN style redirects). The old
    8s HEAD-only probe timed out on slow redirect hops and misrouted
    real files into yt-dlp's generic extractor, which saved them as
    "generic video #mp4&cs=2 [mp4&cs=2]" garbage."""
    fname = None
    final_url = url
    try:
        r = None
        try:
            r = requests.head(url, allow_redirects=True, timeout=timeout)
            if r.status_code >= 400:
                r = None
        except requests.RequestException:
            # Many file/CDN servers (speed-test endpoints included) just
            # never answer HEAD — the ranged GET below is the real probe.
            r = None
        if r is None:
            # Some servers don't answer HEAD properly — try a tiny ranged GET.
            r = requests.get(url, headers={"Range": "bytes=0-0"},
                             stream=True, timeout=timeout, allow_redirects=True)
            # close the 1-byte body immediately — stream=True keeps the
            # socket open otherwise
            r.close()
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        final_url = r.url or url
        cd = r.headers.get("content-disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
        if m:
            fname = m.group(1).strip()
        if not fname:
            # last resort: a real name embedded in the FINAL url's query
            # (?n=name.mp4), which survives the redirect chain
            qm = re.search(r"[?&](?:n|filename|name)=([\w.\-]+\.[A-Za-z0-9]{2,5})(?:&|$)",
                           final_url)
            if qm:
                fname = qm.group(1)
        if not ctype:
            return (fname, None)
        if ctype not in _PAGE_CONTENT_TYPES:
            # derive name from the content-type if still unknown
            if not fname:
                ext = mimetypes.guess_extension(ctype) or ""
                if ext:
                    fname = f"file{ext}"
            return (fname, True)
        return (fname, False)
    except requests.RequestException:
        return (None, None)


def _server_name_via_get(url):
    """Content-Disposition filename via a tiny ranged GET (the HEAD probe
    times out on slow redirect hops; the ranged GET answers)."""
    try:
        with requests.get(url, headers={"Range": "bytes=0-0"},
                           stream=True, timeout=15, allow_redirects=True) as r:
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)",
                          r.headers.get("content-disposition", ""))
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return None


def download_direct(url, chat_id, msg_id, task_id, client, dest_dir, label=None,
                    progress_prefix_fn=None):
    """Direct file link — downloaded via aria2c (16 connections) instead of
    a single-stream `requests` fetch for real multi-connection speed, and
    with --content-disposition=true so the file is named exactly what the
    server calls it rather than guessed from the URL path. The probe's
    filename (when the server gives one) is shown live during download —
    progress used to be nameless, which made multi-file jobs opaque.
    label — optional extra line (multipart runners pass "Part 2/14" so the
    per-part bar carries its position in the set)."""
    os.makedirs(dest_dir, exist_ok=True)
    fname, _is_direct = probe_direct(url)
    if fname:
        logger.info(f"[{task_id}] direct download (aria2c) — server name: {fname!r}")
    else:
        logger.info(f"[{task_id}] direct download starting (aria2c) — {url}")
    args = [
        "aria2c", "-x", "16", "-s", "16", "-k", "1M",
        "--summary-interval=1", "--content-disposition=true",
        "--file-allocation=none", "--auto-file-renaming=false",
        "-d", dest_dir, url,
    ]
    if fname:
        # aria2c names it from the LAST redirect hop's path (a UUID blob);
        # out= pins it to the server's own name up front.
        args += ["-o", fname]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, bufsize=1)
    state.attach_proc(task_id, proc)

    last_edit = 0.0
    try:
        for line in iter(proc.stdout.readline, ""):
            if state.is_cancelled(task_id):
                break
            now = time.time()
            if now - last_edit < 1.5:
                continue
            m = ARIA2_PROGRESS_RE.search(line)
            if not m:
                continue
            last_edit = now
            done, total, pct, spd = m.groups()
            try:
                done_b, total_b = _parse_aria2_size(done), _parse_aria2_size(total)
            except ValueError:
                continue  # one odd line must never kill the drain loop
            head = (f"📄 <code>{esc(fname)}</code>\n" if fname else "")
            lbl = (f"{label}\n" if label else "")
            prefix = progress_prefix_fn() if progress_prefix_fn else ""
            if prefix:
                prefix += "\n"
            text = (f"{prefix}⬇️ <b>Downloading</b>\n{lbl}{head}"
                    f"{progress_line('⬇️', done_b, total_b)}\n⚡ {spd}/s")
            throttled_edit(client, chat_id, msg_id, text, markup=cancel_kb(task_id))
    except (AttributeError, ValueError):
        # stdout can be None/closed when the process was just terminated
        # by /cancel — that's expected teardown, not an error.
        pass

    proc.wait()
    if state.is_cancelled(task_id):
        logger.info(f"[{task_id}] direct download cancelled")
        raise CancelledError("cancelled by user")
    if proc.returncode != 0:
        raise DownloadError(f"aria2c exited with code {proc.returncode} — the link may be dead or blocked")

    files = [f for f in os.listdir(dest_dir) if not f.endswith(".aria2")]
    if not files:
        raise DownloadError("Download reported success but no file was found")
    fpath = os.path.join(dest_dir, files[0])
    logger.info(f"[{task_id}] direct download done — {fpath} ({fmtsz(os.path.getsize(fpath))})")
    return fpath

def _ffprobe_meta(fpath):
    """Returns {duration, width, height} via ffprobe, or {} if unavailable/
    not a media file. Cheap (reads only headers)."""
    import subprocess as sp
    try:
        out = sp.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration:format=duration",
             "-of", "json", fpath],
            capture_output=True, text=True, timeout=20,
        ).stdout
        import json as _j
        d = _j.loads(out or "{}")
        st = ((d.get("streams") or [{}])[0])
        meta = {
            "duration": float(st.get("duration") or d.get("format", {}).get("duration") or 0) or None,
            "width": st.get("width"),
            "height": st.get("height"),
        }
        return {k: v for k, v in meta.items() if v}
    except Exception:
        return {}


def fix_media_metadata(fpath):
    """WZML-X-style metadata pass before upload. yt-dlp's ffmpeg merge can
    produce files whose container-level duration/dimensions are missing or
    wrong (Telegram then displays the video as "0 seconds"). Remuxing with
    `-c copy` rewrites the header from the actual elementary streams without
    recoding — near-instant, lossless, and fixes the tag problem at the
    source. Falls back silently to the original file if ffmpeg is missing
    or the file isn't remuxable."""
    ext = os.path.splitext(fpath)[1].lower().lstrip(".")
    if ext not in ("mp4", "mkv", "webm", "mov", "m4v", "ts", "mpg"):
        return fpath
    if not shutil_which("ffmpeg"):
        return fpath
    # Temp output keeps the ORIGINAL extension — ffmpeg picks its muxer from
    # it (mp4/matroska/mpegts…), then we atomically swap the remuxed file in.
    tmp = f"{fpath}.remux.{ext}"
    args = ["ffmpeg", "-y", "-loglevel", "error", "-i", fpath,
            "-map", "0", "-c", "copy", "-movflags", "+faststart", tmp]
    try:
        import subprocess as sp
        r = sp.run(args, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            _rm(tmp)
            return fpath
        # Keep the ORIGINAL extension (the container type doesn't change in
        # a stream-copy remux; only headers are rewritten).
        os.replace(tmp, fpath)
    except Exception as e:
        logger.warning(f"metadata remux failed for {fpath}: {e}")
        _rm(tmp)
    return fpath


def _rm(p):
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def probe_video_meta(fpath):
    """Duration/dimensions for Telegram upload attributes — read straight
    off the final file so they're always right, never guessed."""
    m = _ffprobe_meta(fpath)
    return {
        "duration": int(m["duration"]) if m.get("duration") else 0,
        "width": int(m["width"]) if m.get("width") else 0,
        "height": int(m["height"]) if m.get("height") else 0,
    }


def shutil_which(name):
    import shutil
    return shutil.which(name)
