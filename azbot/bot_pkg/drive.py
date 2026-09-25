import os
import threading

from . import config, state, log

logger = log.get(__name__)

CancelledError = state.CancelledError

# ── Multi-account support ────────────────────────────────────────────────
# Accounts are defined in env (see config._parse_drive_accounts). Each chat
# picks one via /settings; every API call here takes the account name so
# concurrent chats can use different accounts safely.
_srv_cache = {}
_srv_lock = threading.Lock()

def enabled():
    return bool(config.DRIVE_ACCOUNTS)

# ONE process-wide lock around every googleapiclient call: the shared
# service object's httplib2 transport is NOT thread-safe — concurrent
# requests from IG-archive workers + datastore mirrors corrupted the heap
# ("free(): corrupted unsorted chunks") and killed the container twice on
# Sevalla. Serializing all Drive API traffic trades a little throughput
# for not dying mid-job.
_api_lock = threading.RLock()


def get_service(account=None):
    """Returns a Drive service bound to THIS thread. Each worker thread gets
    its own service instance (httplib2 objects must never be shared across
    threads); creation and refresh are serialized by _api_lock."""
    name = account or state.DEFAULT_ACCOUNT
    if not name or name not in config.DRIVE_ACCOUNTS:
        return None
    key = (name, threading.get_ident())
    with _srv_lock:
        cached = _srv_cache.get(key)
        if cached is not None:
            return cached
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import build_http
    from google_auth_httplib2 import AuthorizedHttp
    with _api_lock:
        a = config.DRIVE_ACCOUNTS[name]
        creds = Credentials(
            token=None, refresh_token=a["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=a["client_id"], client_secret=a["client_secret"],
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        # CRITICAL: build via googleapiclient's own build_http(), NOT a bare
        # httplib2.Http(timeout=300). build_http() removes 308 from
        # httplib2's redirect codes — Drive's resumable protocol answers
        # every chunk with 308 "Resume Incomplete" (no Location header),
        # and with 308 left in the redirect set httplib2 raises
        # "Redirected but the response is missing a Location: header" on
        # EVERY chunk (the systematic upload failure). A 300s socket
        # timeout ceiling is then layered on top to kill true dead hangs.
        http = AuthorizedHttp(creds, build_http())
        http.timeout = 300
        srv = build("drive", "v3", http=http, static_discovery=False,
                    cache_discovery=False)
    with _srv_lock:
        _srv_cache[key] = srv
    return srv


def api_locked():
    """Context manager: serialize any multi-call Drive API sequence."""
    class _Ctx:
        def __enter__(self):
            _api_lock.acquire()
            return self

        def __exit__(self, *exc):
            _api_lock.release()
            return False
    return _Ctx()

def _with_retry(fn, attempts=3):
    """Retries a READ-ONLY Drive API closure on transport failures (broken
    pipe / connection reset / timeout / redirect glitch on a stale
    keep-alive socket — the '[Errno 32] Broken pipe' the /drive browser
    hit). Each attempt calls fn() which re-fetches the service; a failed
    attempt clears the whole service cache so the next attempt builds
    FRESH connections. Never used for uploads (those are resumable and
    retry internally)."""
    import time as _t
    import httplib2 as _h2
    last = None
    for i in range(attempts):
        try:
            return fn()
        except (BrokenPipeError, ConnectionError, TimeoutError, OSError,
                _h2.HttpLib2Error) as e:
            last = e
            logger.warning(f"Drive transport error (attempt {i + 1}/{attempts}): {e}")
            with _srv_lock:
                _srv_cache.clear()
            _t.sleep(1 + i)
    raise last


def _root_id(chat_id):
    """Uploads go to the chat's chosen account root (or DRIVE_FOLDER_ID if set)."""
    return config.DRIVE_FOLDER_ID or "root"

def _resolve_folder(srv, name, parent_id, create=True):
    """Finds (or creates) folder `name` inside parent_id. `name` may contain
    slashes ("Instagram/user/posts") — each level is resolved/created in
    order. Returns final folder id."""
    with _api_lock:
        node = parent_id
        for level in name.split("/"):
            level = level.strip()
            if not level:
                continue
            safe = level.replace("'", "\\'")
            q = (f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
                 f"and '{node}' in parents and trashed = false")
            res = srv.files().list(q=q, fields="files(id)", pageSize=1).execute()
            files = res.get("files", [])
            if files:
                node = files[0]["id"]
                continue
            if not create:
                return None
            meta = {"name": level, "mimeType": "application/vnd.google-apps.folder",
                    "parents": [node]}
            node = srv.files().create(body=meta, fields="id").execute()["id"]
        return node

def ensure_folder(name, chat_id=None, account=None):
    """Returns the id of Drive folder `name` for this chat's account,
    creating it inside the root if it doesn't exist. None on failure."""
    srv = get_service(account or state.get_drive_account(chat_id))
    if not srv:
        return None
    try:
        return _resolve_folder(srv, name, _root_id(chat_id), create=True)
    except Exception as e:
        logger.warning(f"ensure_folder('{name}') failed: {e}")
        return None

def _find_file(srv, name):
    with _api_lock:
        safe_name = name.replace("'", "\\'")
        q = f"name = '{safe_name}' and trashed = false"
        res = srv.files().list(q=q, fields="files(id, name)", pageSize=1).execute()
        files = res.get("files", [])
        return files[0]["id"] if files else None

def download_named_file(name, dest_path):
    """Downloads a Drive file by name to dest_path.
    Returns True if found and downloaded, False if it doesn't exist yet."""
    with _api_lock:
        srv = get_service()
        if not srv:
            return False
        from googleapiclient.http import MediaIoBaseDownload
        fid = _find_file(srv, name)
        if not fid:
            return False
        request = srv.files().get_media(fileId=fid)
        with open(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True

def upload_named_file(name, local_path):
    """Uploads/overwrites a Drive file by name."""
    with _api_lock:
        srv = get_service()
        if not srv:
            return None
        from googleapiclient.http import MediaFileUpload
        fid = _find_file(srv, name)
        media = MediaFileUpload(local_path, resumable=False)
        if fid:
            srv.files().update(fileId=fid, media_body=media).execute()
            return fid
        meta = {"name": name}
        resp = srv.files().create(body=meta, media_body=media, fields="id").execute()
        return resp["id"]

def list_files(query_extra=None, page_size=20, account=None):
    """Lists files at the configured root level."""
    with _api_lock:
        srv = get_service()
        if not srv:
            return None
        q_parts = ["trashed = false"]
        if config.DRIVE_FOLDER_ID:
            q_parts.append(f"'{config.DRIVE_FOLDER_ID}' in parents")
        if query_extra:
            q_parts.append(query_extra)
        q = " and ".join(q_parts)
        res = srv.files().list(
            q=q, fields="files(id, name, mimeType, size, webViewLink)",
            pageSize=page_size, orderBy="modifiedTime desc",
        ).execute()
        return res.get("files", [])

def search_files(name_contains, page_size=20, account=None):
    safe = name_contains.replace("'", "\\'")
    return list_files(query_extra=f"name contains '{safe}'", page_size=page_size, account=account)

# Drive uploads: process-wide semaphore (see config.DRIVE_UPLOAD_CONCURRENCY).
# googleapiclient's httplib2 transport is not reliably thread-safe under
# concurrent resumable uploads (observed heap corruption "free(): corrupted
# unsorted chunks" on Sevalla when an IG archive and a settings mirror
# uploaded simultaneously). Default 1 = strictly serial; the download side
# keeps running regardless, so a not-yet-uploaded backlog accumulates on
# disk until each file's turn (deleted the moment ITS upload completes).
_UPLOAD_SEMA = threading.Semaphore(max(1, config.DRIVE_UPLOAD_CONCURRENCY))


def upload_file(fpath, progress_cb=None, task_id=None, rename=None, folder=None,
                chat_id=None, account=None):
    """Uploads fpath; returns webViewLink or None (see upload_file_full)."""
    info = upload_file_full(fpath, progress_cb=progress_cb, task_id=task_id,
                            rename=rename, folder=folder, chat_id=chat_id, account=account)
    return info["link"] if info else None


def upload_file_full(fpath, progress_cb=None, task_id=None, rename=None,
                     folder=None, chat_id=None, account=None, make_public=True):
    """Serialized wrapper: DRIVE_UPLOAD_CONCURRENCY uploads at a time, with
    automatic retries on transport-level failures (httplib2's
    'Redirected but the response is missing a Location: header' glitch,
    broken pipes, resets). The impl resumes resumable sessions in-place;
    this outer loop rebuilds the session from scratch as a last resort."""
    import time as _t
    import httplib2 as _h2
    last = None
    for attempt in range(3):
        try:
            with _UPLOAD_SEMA:
                return _upload_file_full_impl(fpath, progress_cb=progress_cb, task_id=task_id,
                                              rename=rename, folder=folder, chat_id=chat_id,
                                              account=account, make_public=make_public)
        except (BrokenPipeError, ConnectionError, TimeoutError, OSError,
                _h2.HttpLib2Error) as e:
            last = e
            logger.warning(f"Drive upload transport error (attempt {attempt + 1}/3): {e}")
            with _srv_lock:
                _srv_cache.clear()   # force a fresh transport next attempt
            _t.sleep(2)
        except Exception:
            raise
    raise last


def _upload_file_full_impl(fpath, progress_cb=None, task_id=None, rename=None,
                           folder=None, chat_id=None, account=None, make_public=True):
    """Uploads fpath to the chat's chosen Drive account.

    rename  — final file NAME; extension is preserved from fpath unless the
              caller already included one ("extension won't be touched" rule:
              we only REPLACE the basename).
    folder  — "#folder" tag target: created inside the root on demand, file
              goes there instead of the root.
    Returns {"id", "link"} or None."""
    from googleapiclient.http import MediaFileUpload
    acct = account or state.get_drive_account(chat_id)
    srv = get_service(acct)
    if not srv:
        return None
    logger.info(f"Drive[{acct}] upload starting — {fpath}")

    base = os.path.basename(fpath)
    ext = os.path.splitext(base)[1]
    if rename:
        # Extension is NEVER replaced by the requested name: if |name lacks
        # the file's real extension we append it; if the user already typed
        # the same extension we don't double it. A dotted name like "my.v2"
        # still keeps the true ".mp4".
        rename = rename.strip()
        if ext and not rename.lower().endswith(ext.lower()):
            rename = rename + ext
        name = rename
    else:
        # strip yt-dlp's trailing " [id]" suffix from the DISPLAY name —
        # generic-extractor ids are 32-char hash strings, ugly in Drive
        # listings. The file on disk keeps its name (collision safety);
        # only the Drive copy gets the clean one.
        import re as _re
        name = _re.sub(r"\s*\[[0-9A-Za-z_-]{16,64}\]$",
                       "", os.path.splitext(base)[0]).strip() or base
        name += ext

    meta = {"name": name}
    if folder:
        pid = ensure_folder(folder, chat_id=chat_id, account=acct)
        if pid:
            meta["parents"] = [pid]
    elif config.DRIVE_FOLDER_ID:
        meta["parents"] = [config.DRIVE_FOLDER_ID]

    media = MediaFileUpload(fpath, chunksize=16 * 1024 * 1024, resumable=True)
    req = srv.files().create(body=meta, media_body=media, fields="id, webViewLink")
    response = None
    import time as _t
    import httplib2 as _h2
    transport_fails = 0
    while response is None:
        if task_id and state.is_cancelled(task_id):
            logger.info(f"[{task_id}] Drive upload cancelled by user")
            raise CancelledError("drive upload cancelled by user")
        # next_chunk holds the socket for up to a 16MB chunk — hold the API
        # lock across the whole resumable loop so no other thread touches
        # this service's transport mid-request.
        with _api_lock:
            try:
                status, response = req.next_chunk(num_retries=3)
            except (_h2.HttpLib2Error, ConnectionError, TimeoutError, OSError) as e:
                # Transient glitch on a chunk POST (e.g. httplib2's
                # "Redirected but the response is missing a Location: header"
                # when Google answers a 308 oddly, or a stale socket). The
                # resumable protocol resumes from the last ACKNOWLEDGED byte
                # — nothing is re-uploaded. Cap at 6; beyond that the outer
                # upload_file_full retry rebuilds the session from scratch.
                transport_fails += 1
                if transport_fails > 6:
                    raise
                logger.warning(f"Drive upload transport glitch #{transport_fails} ({e}) "
                               f"— resuming resumable session")
                _t.sleep(2 * transport_fails)
                continue
        if status and progress_cb:
            progress_cb(int(status.resumable_progress), int(status.total_size or 0))
    if make_public:
        try:
            srv.permissions().create(fileId=response["id"],
                                     body={"role": "reader", "type": "anyone"}).execute()
        except Exception as e:
            logger.warning(f"Drive permission set failed for {response.get('id')}: {e}")
    link = response.get("webViewLink") or f"https://drive.google.com/file/d/{response['id']}/view"
    logger.info(f"Drive[{acct}] upload done — {fpath} -> {link}")
    return {"id": response["id"], "link": link}

def delete_file(file_id, account=None):
    with _api_lock:
        srv = get_service(account)
        if not srv:
            return False
        srv.files().delete(fileId=file_id).execute()
        logger.info(f"Drive file deleted: {file_id}")
        return True

def rename_file(file_id, new_name, account=None):
    with _api_lock:
        srv = get_service(account)
        if not srv:
            return False
        srv.files().update(fileId=file_id, body={"name": new_name}).execute()
        logger.info(f"Drive file {file_id} renamed to '{new_name}'")
        return True

def get_file(file_id, account=None):
    with _api_lock:
        srv = get_service(account)
        if not srv:
            return None
        try:
            return srv.files().get(fileId=file_id, fields="id, name, size, webViewLink, mimeType").execute()
        except Exception as e:
            logger.warning(f"Drive get_file failed for {file_id}: {e}")
            return None

def list_folder_full(folder_id, max_total=10000, account=None):
    """Fetches every item directly inside folder_id (files AND subfolders),
    paginating internally. The UI slices this client-side rather than
    chaining Drive's own pageToken, since that only supports stepping
    forward — client-side slicing lets the browser jump to any page
    directly and supports real folder navigation with a back button."""
    def once():
        with _api_lock:
            srv = get_service(account)
            if not srv:
                return []
            q = f"'{folder_id}' in parents and trashed = false"
            files, page_token = [], None
            while True:
                res = srv.files().list(
                    q=q, fields="nextPageToken, files(id, name, mimeType, size, webViewLink)",
                    pageSize=200, orderBy="folder,name_natural", pageToken=page_token,
                ).execute()
                files.extend(res.get("files", []))
                page_token = res.get("nextPageToken")
                if not page_token or len(files) >= max_total:
                    break
            return files[:max_total]
    return _with_retry(once)

def create_folder(name, parent_id, account=None):
    with _api_lock:
        srv = get_service(account)
        if not srv:
            return None
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
        resp = srv.files().create(body=meta, fields="id").execute()
        return resp.get("id")

def get_file_any(file_id):
    """Like get_file, but also works for a file that isn't ours — just
    shared with our account, or shared publicly ('anyone with the link').
    Needed for leeching Drive links a user pastes in that weren't
    uploaded through this bot."""
    with _api_lock:
        srv = get_service()
        if not srv:
            return None
        try:
            return srv.files().get(
                fileId=file_id, fields="id, name, size, webViewLink, mimeType",
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            logger.warning(f"Drive get_file_any failed for {file_id}: {e}")
            return None

def list_folder_any(folder_id, max_total=20000):
    """Like list_folder_full, but for a shared folder that isn't ours."""
    def once():
        with _api_lock:
            srv = get_service()
            if not srv:
                return []
            q = f"'{folder_id}' in parents and trashed = false"
            files, page_token = [], None
            while True:
                res = srv.files().list(
                    q=q, fields="nextPageToken, files(id, name, mimeType, size, webViewLink)",
                    pageSize=200, orderBy="folder,name_natural", pageToken=page_token,
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                ).execute()
                files.extend(res.get("files", []))
                page_token = res.get("nextPageToken")
                if not page_token or len(files) >= max_total:
                    break
            return files[:max_total]
    return _with_retry(once)

def download_file_content(file_id, dest_path, task_id=None, mime_type=None, progress_cb=None):
    """Downloads a Drive file's actual bytes to dest_path via the API
    (using our own OAuth credentials — much more reliable than yt-dlp's
    anonymous GoogleDrive extractor, which frequently 400s on ordinary
    shared links). Google-native docs (Sheets/Docs/Slides) get exported
    to a normal file format instead, since they have no raw bytes.

    ATOMIC DELIVERY: bytes land in dest_path + ".part" and are renamed
    into place only when the download is complete. MediaIoBaseDownload
    writes in large (50MB) chunks with multi-second gaps between them —
    without this, a LiveDispatcher watching the folder "2s untouched"
    check would grab the HALF-DOWNLOADED file and upload it: the striped
    uploader then hit FILE_PART_INVALID on short reads at the growing
    EOF, and the delivered partial MP4 had no readable metadata (moov
    not yet written) — hence "100MB video with no thumbnail". The
    ".part" suffix is already in LiveDispatcher's SKIP_SUFFIXES."""
    # NOTE: the global lock is held only for setup and per-chunk
    # below — holding it for the whole multi-GB fetch used to serialize
    # ALL other Drive traffic (browser, uploads) behind one download.
    with _api_lock:
        srv = get_service()
        if not srv:
            raise RuntimeError("Drive isn't configured")
        from googleapiclient.http import MediaIoBaseDownload

        export_map = {
            "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
            "application/vnd.google-apps.spreadsheet": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
            "application/vnd.google-apps.presentation": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
        }
        if mime_type in export_map:
            export_mime, ext = export_map[mime_type]
            if not dest_path.endswith(ext):
                dest_path += ext
            request = srv.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            request = srv.files().get_media(fileId=file_id, supportsAllDrives=True)

    partial = dest_path + ".part"
    try:
        with open(partial, "wb") as fh:
            # 16MB chunks (was 50MB): googleapiclient buffers the whole
            # chunk in RAM per next_chunk() call — on the OOM-prone
            # container the 50MB buffer, stacked on pyrogram + any
            # overlapping upload, is part of the restart profile.
            # 16MB resumable chunks are just as fast on normal links.
            downloader = MediaIoBaseDownload(fh, request, chunksize=16 * 1024 * 1024)
            done = False
            while not done:
                if task_id and state.is_cancelled(task_id):
                    raise state.CancelledError("cancelled by user")
                # Per-chunk lock only: one slow download used to serialize
                # EVERY other Drive call (browser, uploads) behind it for
                # minutes. The transport object is thread-local (one
                # service per thread), so locking just the chunk keeps the
                # heap-corruption invariant without the stall.
                with _api_lock:
                    status, done = downloader.next_chunk(num_retries=3)
                if progress_cb:
                    try:
                        progress_cb(int(getattr(status, "resumable_progress", 0) or 0),
                                    int(getattr(downloader, "total_size", 0) or 0))
                    except Exception:
                        pass
        os.replace(partial, dest_path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    return dest_path
