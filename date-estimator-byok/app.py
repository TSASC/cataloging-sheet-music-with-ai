from flask import (
    Response,
    Flask,
    render_template,
    request,
    send_file,
    jsonify,
    session,
    redirect,
    url_for,
    flash,
    abort,
)
from flask_session import Session
from datetime import datetime, timedelta
import os
import pandas as pd
import re
from werkzeug.utils import secure_filename
from imslp_estimator.core import (
    estimate_year,
    fetch_html,
    extract_main_table_publishers,
    normalize,
    PUBLISHER_LIST_URL,
    find_imprint_year_ranges,
    get_cached_publishers,
)
from imslp_estimator.address_range import (
    extract_addresses_from_html,
    find_matching_addresses,
)
from difflib import get_close_matches, SequenceMatcher
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import json
import logging
from logging.handlers import RotatingFileHandler
import requests
import base64
from PIL import Image
import glob
import secrets
import tempfile
import threading
from ipaddress import ip_address
from urllib.parse import urlencode, urlsplit
try:
    import fcntl  # POSIX advisory file locking (Linux deploy + macOS dev)
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None
import markdown
import hashlib
from cryptography.fernet import Fernet, InvalidToken

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

# Set up logging
LOG_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)

# Configure logging
file_handler = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"), maxBytes=1024 * 1024, backupCount=10)
file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]"
    )
)
file_handler.setLevel(logging.INFO)

# Configure console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
console_handler.setLevel(logging.INFO)

# Set up the logger
logger = logging.getLogger("imslp_estimator")
logger.addHandler(file_handler)
logger.addHandler(console_handler)
logger.setLevel(logging.INFO)

app = Flask(__name__)
app.logger.addHandler(file_handler)  # Add the file handler to Flask's logger
app.logger.addHandler(console_handler)  # Add the console handler to Flask's logger
app.logger.setLevel(logging.INFO)

# Add secret key for session management
# If no FLASK_SECRET_KEY env var, generate one and persist to .env so sessions survive restarts
_secret_key = os.getenv("FLASK_SECRET_KEY")
if not _secret_key:
    _secret_key = secrets.token_hex(24)
    _env_path = os.path.join(ROOT_DIR, ".env")
    with open(_env_path, "a", encoding="utf-8") as f:
        f.write(f"\nFLASK_SECRET_KEY={_secret_key}\n")
    os.chmod(_env_path, 0o600)
app.secret_key = _secret_key
app.config["VERSION"] = "local"
# Sessions are stored in files instead of cookies, allowing large batch data
SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flask_session")
os.makedirs(SESSION_DIR, mode=0o700, exist_ok=True)
os.chmod(SESSION_DIR, 0o700)

app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = SESSION_DIR
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_FILE_THRESHOLD"] = 500  # Max number of session files before cleanup
app.config["SESSION_USE_SIGNER"] = True  # Sign session cookies for security
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Initialize Flask-Session
Session(app)


@app.before_request
def require_local_request():
    try:
        if not ip_address(request.remote_addr or "").is_loopback:
            abort(403)
    except ValueError:
        abort(403)
    if urlsplit(request.host_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        abort(403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            abort(403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403)

BATCH_RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "batch_results")
os.makedirs(BATCH_RESULTS_DIR, exist_ok=True)


# Serializes the read-modify-write cycle on a run's results file. fcntl.flock
# guards against the 2 gunicorn *worker processes*; the threading.Lock guards
# against multiple *threads* in one worker. Together they prevent the lost
# update that deleted the penultimate scan, and the torn read that surfaced as
# the "fatal error" dialog (issue #9).
_batch_locks_guard = threading.Lock()
_batch_locks = {}


def _batch_results_path(run_id):
    return os.path.join(BATCH_RESULTS_DIR, f"{run_id}.json")


def _valid_batch_run_id(run_id):
    return bool(run_id and re.fullmatch(r"[A-Za-z0-9_.-]+", run_id))


def _new_batch_run_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _batch_thread_lock(run_id):
    with _batch_locks_guard:
        lock = _batch_locks.get(run_id)
        if lock is None:
            lock = _batch_locks[run_id] = threading.Lock()
        return lock


def _load_batch_results(run_id):
    path = _batch_results_path(run_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_batch_results(run_id, results):
    """Write results atomically so a concurrent reader never sees a torn file."""
    path = _batch_results_path(run_id)
    # Unique temp name (per process AND thread) so concurrent writers to the
    # same run can't collide on the temp file before the atomic rename.
    fd, tmp_path = tempfile.mkstemp(
        dir=BATCH_RESULTS_DIR, prefix=f"{run_id}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)  # atomic on POSIX
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _append_batch_result(run_id, result):
    """Append one result under an exclusive cross-process + cross-thread lock.

    Idempotent on result['dedupe_key']: a retried or double-fired "Add to Run"
    adds the row at most once. Returns the authoritative new total count.
    """
    lock_path = _batch_results_path(run_id) + ".lock"
    with _batch_thread_lock(run_id):
        lock_f = open(lock_path, "w")
        try:
            if fcntl is not None:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
            results = _load_batch_results(run_id)
            key = result.get("dedupe_key")
            if key and any(r.get("dedupe_key") == key for r in results):
                return len(results)  # already recorded; idempotent no-op
            results.append(result)
            _save_batch_results(run_id, results)
            return len(results)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
            lock_f.close()


def _delete_batch_results(run_id):
    path = _batch_results_path(run_id)
    if os.path.exists(path):
        os.remove(path)
    for suffix in (".lock",):
        extra = path + suffix
        if os.path.exists(extra):
            os.remove(extra)
    with _batch_locks_guard:
        _batch_locks.pop(run_id, None)


def _list_saved_batch_runs(exclude_run_id=None):
    saved_runs = []
    pattern = os.path.join(BATCH_RESULTS_DIR, "*.json")
    for path in glob.glob(pattern):
        run_id = os.path.splitext(os.path.basename(path))[0]
        if run_id == exclude_run_id or not _valid_batch_run_id(run_id):
            continue
        results = _load_batch_results(run_id)
        if not results:
            continue
        saved_runs.append(
            {
                "id": run_id,
                "count": len(results),
                "saved_at": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
            }
        )
    saved_runs.sort(key=lambda r: r["saved_at"], reverse=True)
    return saved_runs[:20]


# Transcribed fields with locally preserved edit provenance.
TRANSCRIPTION_FIELDS = (
    "title",
    "statement_of_responsibility",
    "edition",
    "city",
    "publisher_name",
    "address",
    "plate_number",
    "date",
)
FIELD_PROVENANCE_SOURCES = (
    "model",
    "human_edited",
    "human_edited_external",
    "estimator_provided",
)

LOCAL_IMAGE_GLOBS = (
    "*.jpg",
    "*.jpeg",
    "*.png",
    "*.gif",
    "*.bmp",
    "*.tiff",
    "*.webp",
)
COMPARISON_PROVIDERS = ("harvard", "harvard_gemini")
COMPARISON_KEY_PROVIDERS = ("harvard",)
MAX_COMPARISON_IMAGES = 10


def _parse_json_object(raw):
    """Parse a JSON object out of a form field; {} on anything unexpected."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _batch_template_context(error=None, **extra):
    current_run = session.get("current_run")
    folder_path = session.get("batch_folder_path")
    run_count = len(_load_batch_results(current_run["id"])) if current_run else 0
    context = {
        "error": error,
        "folder_path": folder_path,
        "current_run": current_run,
        "run_count": run_count,
        "saved_runs": _list_saved_batch_runs(
            current_run["id"] if current_run else None
        ),
        "batch_prompt": session.get("batch_prompt"),
        "default_prompt": get_default_prompt(),
    }
    context.update(extra)
    return context


def _path_is_inside(folder_path, file_path):
    try:
        folder_abs = os.path.abspath(folder_path)
        file_abs = os.path.abspath(file_path)
        return os.path.commonpath([folder_abs, file_abs]) == folder_abs
    except (TypeError, ValueError):
        return False


def _list_local_folder_images(folder_path):
    if not folder_path or not os.path.isdir(folder_path):
        return []

    images = []
    for ext in LOCAL_IMAGE_GLOBS:
        images.extend(glob.glob(os.path.join(folder_path, ext)))
        images.extend(glob.glob(os.path.join(folder_path, ext.upper())))
    images = list({os.path.normcase(p): p for p in images}.values())

    image_info = []
    for img_path in images:
        try:
            stat = os.stat(img_path)
        except OSError:
            continue
        image_info.append(
            {
                "name": os.path.basename(img_path),
                "path": img_path,
                "modified": stat.st_mtime,
                "modified_str": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "size": stat.st_size,
            }
        )
    image_info.sort(key=lambda x: x["modified"], reverse=True)
    return image_info


API_PROVIDERS = {
    "harvard": {
        "name": "Harvard HUIT",
        "description": "Harvard HUIT OpenAI endpoint (Azure-style auth)",
        "placeholder": "Enter your Harvard HUIT key",
        "env_var": "OPENAI_API_KEY",
        "models": {
            "gpt-5.6-sol": "GPT-5.6 Sol",
            "gpt-5.6-terra": "GPT-5.6 Terra",
            "gpt-5.6-luna": "GPT-5.6 Luna",
            "chat-latest": "Chat Latest",
            "gpt-5.5": "GPT-5.5",
            "gpt-5.5-2026-04-23": "GPT-5.5 (2026-04-23)",
            "gpt-5.4": "GPT-5.4",
            "gpt-5.4-2026-03-05": "GPT-5.4 (2026-03-05)",
            "gpt-5.4-mini": "GPT-5.4 Mini",
            "gpt-5.4-mini-2026-03-17": "GPT-5.4 Mini (2026-03-17)",
            "gpt-5.4-nano": "GPT-5.4 Nano",
            "gpt-5.4-nano-2026-03-17": "GPT-5.4 Nano (2026-03-17)",
            "gpt-5.3-chat-latest": "GPT-5.3 Chat Latest",
            "gpt-5.2-chat-latest": "GPT-5.2 Chat Latest",
            "gpt-5.2": "GPT-5.2",
            "gpt-5.2-2025-12-11": "GPT-5.2 (2025-12-11)",
            "gpt-5.1-chat-latest": "GPT-5.1 Chat Latest",
            "gpt-5.1": "GPT-5.1",
            "gpt-5.1-2025-11-13": "GPT-5.1 (2025-11-13)",
            "gpt-5-chat-latest": "GPT-5 Chat Latest",
            "gpt-5": "GPT-5",
            "gpt-5-2025-08-07": "GPT-5 (2025-08-07)",
            "gpt-5-mini": "GPT-5 Mini",
            "gpt-5-mini-2025-08-07": "GPT-5 Mini (2025-08-07)",
            "gpt-5-nano": "GPT-5 Nano",
            "gpt-5-nano-2025-08-07": "GPT-5 Nano (2025-08-07)",
            "gpt-4.1": "GPT-4.1",
            "gpt-4.1-2025-04-14": "GPT-4.1 (2025-04-14)",
            "gpt-4.1-mini": "GPT-4.1 Mini",
            "gpt-4.1-mini-2025-04-14": "GPT-4.1 Mini (2025-04-14)",
            "gpt-4.1-nano": "GPT-4.1 Nano",
            "gpt-4.1-nano-2025-04-14": "GPT-4.1 Nano (2025-04-14)",
            "gpt-4o": "GPT-4o",
            "gpt-4o-2024-11-20": "GPT-4o (2024-11-20)",
            "gpt-4o-2024-08-06": "GPT-4o (2024-08-06)",
            "gpt-4o-2024-05-13": "GPT-4o (2024-05-13)",
            "gpt-4o-mini": "GPT-4o Mini",
            "gpt-4o-mini-2024-07-18": "GPT-4o Mini (2024-07-18)",
            "o4-mini": "o4 Mini",
            "o4-mini-2025-04-16": "o4 Mini (2025-04-16)",
            "o3": "o3",
            "o3-2025-04-16": "o3 (2025-04-16)",
            "o1": "o1",
            "o1-2024-12-17": "o1 (2024-12-17)",
        },
        "default_model": "gpt-5.4",
    },
    "harvard_gemini": {
        "name": "Harvard Gemini",
        "description": "Harvard HUIT Gemini endpoint",
        "placeholder": "Enter the same Harvard HUIT key",
        "env_var": "OPENAI_API_KEY",
        "key_provider": "harvard",
        "models": {
            "gemini-3.1-pro-preview": "Gemini 3.1 Pro Preview",
            "gemini-3-flash-preview": "Gemini 3 Flash Preview",
            "gemini-3.1-flash-lite": "Gemini 3.1 Flash Lite",
            "gemini-2.5-flash": "Gemini 2.5 Flash",
            "gemini-2.5-pro": "Gemini 2.5 Pro",
            "gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
        },
        "default_model": "gemini-2.5-flash",
    },
    "openai": {
        "name": "OpenAI",
        "description": "Standard OpenAI API (api.openai.com)",
        "placeholder": "Enter your OpenAI API key (sk-...)",
        "env_var": "OPENAI_STANDARD_API_KEY",
        "models": {
            "gpt-5.4": "GPT-5.4",
            "gpt-5.4-mini": "GPT-5.4 Mini",
            "gpt-4.1": "GPT-4.1",
            "gpt-4.1-mini": "GPT-4.1 Mini",
            "gpt-4.1-nano": "GPT-4.1 Nano",
            "gpt-4o": "GPT-4o",
            "gpt-4o-mini": "GPT-4o Mini",
        },
        "default_model": "gpt-5.4",
    },
    "anthropic": {
        "name": "Anthropic",
        "description": "Claude API for image transcription",
        "placeholder": "Enter your Anthropic API key (sk-ant-...)",
        "env_var": "ANTHROPIC_API_KEY",
        "models": {
            "claude-sonnet-4-20250514": "Claude Sonnet 4",
            "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
        },
        "default_model": "claude-sonnet-4-20250514",
    },
    "gemini": {
        "name": "Google Gemini",
        "description": "Gemini API for image transcription",
        "placeholder": "Enter your Gemini API key",
        "env_var": "GEMINI_API_KEY",
        "models": {
            "gemini-2.5-flash": "Gemini 2.5 Flash",
            "gemini-2.5-pro": "Gemini 2.5 Pro",
        },
        "default_model": "gemini-2.5-flash",
    },
}

HARVARD_OPENAI_CHAT_ENDPOINTS = (
    (
        "https://apis.huit.harvard.edu/ais-openai-direct/v2/chat/completions",
        "X-Api-Key",
    ),
    (
        "https://go.apis.huit.harvard.edu/ais-openai-direct/v1/chat/completions",
        "api-key",
    ),
)

HARVARD_GEMINI_ENDPOINT_TEMPLATE = (
    "https://apis.huit.harvard.edu/ais-google-gemini/v1beta/models/{model}:generateContent"
)


def _provider_key_id(provider):
    return API_PROVIDERS.get(provider, {}).get("key_provider", provider)


def _expand_provider_key_aliases(keys):
    expanded = dict(keys or {})
    for provider in API_PROVIDERS:
        key_provider = _provider_key_id(provider)
        if provider not in expanded and key_provider in expanded:
            expanded[provider] = expanded[key_provider]
    return expanded


def _get_fernet():
    secret = app.secret_key
    if isinstance(secret, str):
        secret = secret.encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_api_key(plaintext):
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext):
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None


def get_active_api_key():
    active = session.get("active_provider", "harvard")
    if active not in API_PROVIDERS:
        active = "harvard"
    return get_api_key_for_provider(active), active


def get_api_key_for_provider(provider):
    if provider not in API_PROVIDERS:
        return None
    key_provider = _provider_key_id(provider)
    encrypted_keys = session.get("api_keys", {})
    encrypted = encrypted_keys.get(key_provider)
    if encrypted:
        key = decrypt_api_key(encrypted)
        if key:
            return key
    return os.getenv(API_PROVIDERS[provider]["env_var"]) or None


def get_active_model():
    active = session.get("active_provider", "harvard")
    if active not in API_PROVIDERS:
        active = "harvard"
    model = session.get("selected_models", {}).get(active)
    if model in API_PROVIDERS[active]["models"]:
        return model
    return API_PROVIDERS[active]["default_model"]



ONEDRIVE_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
ONEDRIVE_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
ONEDRIVE_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")
ONEDRIVE_SCOPES = "Files.Read Sites.Read.All offline_access"
GRAPH_API = "https://graph.microsoft.com/v1.0"


def onedrive_auth_url(redirect_uri):
    params = urlencode({
        "client_id": ONEDRIVE_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": ONEDRIVE_SCOPES,
        "response_mode": "query",
        "state": session["onedrive_oauth_state"],
    })
    return f"https://login.microsoftonline.com/{ONEDRIVE_TENANT_ID}/oauth2/v2.0/authorize?{params}"


def onedrive_exchange_code(code, redirect_uri):
    resp = requests.post(
        f"https://login.microsoftonline.com/{ONEDRIVE_TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": ONEDRIVE_CLIENT_ID,
            "client_secret": ONEDRIVE_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "scope": ONEDRIVE_SCOPES,
        },
        timeout=30,
    )
    return resp.json()


def onedrive_refresh_token(refresh_token):
    resp = requests.post(
        f"https://login.microsoftonline.com/{ONEDRIVE_TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": ONEDRIVE_CLIENT_ID,
            "client_secret": ONEDRIVE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": ONEDRIVE_SCOPES,
        },
        timeout=30,
    )
    return resp.json()


def onedrive_get_token():
    """Get a valid OneDrive access token, refreshing if needed."""
    od = session.get("onedrive", {})
    if not od.get("access_token"):
        return None
    import time
    if od.get("expires_at", 0) < time.time() and od.get("refresh_token"):
        data = onedrive_refresh_token(od["refresh_token"])
        if "access_token" in data:
            od["access_token"] = data["access_token"]
            od["refresh_token"] = data.get("refresh_token", od["refresh_token"])
            od["expires_at"] = time.time() + data.get("expires_in", 3600)
            session["onedrive"] = od
            session.modified = True
    return od.get("access_token")


def onedrive_api(path, token):
    resp = requests.get(f"{GRAPH_API}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code == 200:
        return resp.json()
    return None


PUBLISHER_PREFIX_PATTERNS = [
    r"(?i)^published\s+by\s+",
    r"(?i)^printed\s+(and\s+)?(for\s+)?(by\s+)?",
    r"(?i)^issued\s+by\s+",
    r"(?i)^editor:\s+",
    r"(?i)^distribué\s+par\s+",
    r"(?i)^verlegt\s+von\s+",
    r"(?i)^for\s+private\s+circulation",
    r"(?i)^pub\.\s+by\s+",
]

app.config["UPLOAD_FOLDER"] = "uploads"
app.config["ALLOWED_EXTENSIONS"] = {"xlsx"}
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB max upload

# Add global progress tracking
upload_progress = {
    "current": 0,
    "total": 0,
    "status": "idle",
    "unfound_publishers": [],  # Add list to track unfound publishers
}


def cleanup_old_files(folder_path, max_age_hours=24):
    """Clean up old files in a folder to manage storage.

    Args:
        folder_path: Path to folder to clean
        max_age_hours: Delete files older than this many hours (default: 24)
    """
    if not os.path.exists(folder_path):
        return

    cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
    try:
        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)
            if os.path.isfile(file_path):
                file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                if file_mtime < cutoff_time:
                    os.remove(file_path)
    except OSError:
        logger.warning("Could not clean up old upload files")


def clean_publisher_name(raw):
    for pattern in PUBLISHER_PREFIX_PATTERNS:
        raw = re.sub(pattern, "", raw).strip()
    return raw


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )


def normalize_path(path):
    """Strip surrounding quotes and whitespace from pasted file paths."""
    path = path.strip().strip('"').strip("'").strip()
    return path





@app.route("/progress")

def get_progress():
    return jsonify(upload_progress)


@app.route("/unfound-publishers")

def unfound_publishers():
    """Return the list of unfound publishers from the last upload."""
    return jsonify(
        {
            "publishers": upload_progress["unfound_publishers"],
            "count": len(upload_progress["unfound_publishers"]),
        }
    )


@app.route("/upload", methods=["GET", "POST"])

def upload():
    if request.method == "POST":
        try:
            # Reset progress
            upload_progress["current"] = 0
            upload_progress["total"] = 0
            upload_progress["status"] = "starting"
            upload_progress["unfound_publishers"] = []  # Reset unfound publishers list

            file = request.files.get("file")
            excel_path = request.form.get("excel_path", "").strip()

            if excel_path:
                excel_path = normalize_path(excel_path)
                if not os.path.exists(excel_path):
                    upload_progress["status"] = "error"
                    return "❌ File not found at the specified path."
                if not excel_path.lower().endswith(".xlsx"):
                    upload_progress["status"] = "error"
                    return "❌ Only .xlsx files are supported."
                temp_path = excel_path
                filename = os.path.basename(excel_path)
            elif file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                temp_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
                file.save(temp_path)
            else:
                upload_progress["status"] = "error"
                return "❌ Please upload an .xlsx file or provide a file path."

            # Read Excel file with plate numbers as text
            df = pd.read_excel(
                temp_path,
                dtype={"Plate number": str},  # Force plate numbers to be read as text
                na_filter=False,  # Prevent NaN conversion
            )
            required_cols = {"City", "Publisher name", "Address", "Plate number"}
            if not required_cols.issubset(df.columns):
                missing = required_cols - set(df.columns)
                upload_progress["status"] = "error"
                return f"❌ Excel is missing required columns: {missing}"

            estimates = []
            detailed_results = []
            total_entries = len(df)
            upload_progress["total"] = total_entries
            upload_progress["status"] = "processing"
            successful_estimates = 0
            publishers_not_found = 0
            publishers_no_page = 0

            for idx, row in df.iterrows():
                try:
                    upload_progress["current"] = idx + 1
                    city = str(row.get("City", "")).strip()
                    publisher = clean_publisher_name(
                        str(row.get("Publisher name", "")).strip()
                    )
                    address = str(row.get("Address", "")).strip()
                    plate = clean_plate_number(row.get("Plate number", ""))

                    full_address = f"{address}, {city}".strip(", ")

                    result = estimate_year(publisher, plate, full_address)
                    if result.get("error"):
                        # Add to unfound publishers list if not already present
                        if publisher not in upload_progress["unfound_publishers"]:
                            upload_progress["unfound_publishers"].append(publisher)

                        estimates.append("❌ " + result["error"])
                        if "not found" in result["error"].lower():
                            publishers_not_found += 1
                        elif "no imslp page" in result["error"].lower():
                            publishers_no_page += 1
                        detailed_results.append(
                            {
                                "Publisher": publisher,
                                "Address": address,
                                "City": city,
                                "Plate_number": plate,
                                "Estimated_Year": "❌ " + result["error"],
                                "IMSLP_Link": "N/A",
                                "Date_Source": "N/A",
                                "Found_Dates": "N/A",
                                "date_estimation_analysis": {
                                    "main_table": result["error"],
                                    "imprint": result["error"],
                                    "address": result["error"],
                                    "plate": result["error"],
                                },
                            }
                        )
                    else:
                        successful_estimates += 1
                        estimates.append(result["estimated_year"] or "❌ Not found")

                        # Format all found dates in a readable way
                        all_dates = []
                        main_table_range = None
                        imprint_ranges = []
                        address_ranges = []
                        plate_ranges = []

                        for r in result["all_ranges"]:
                            if "main publisher table" in r["source"]:
                                main_table_range = r
                            elif "imprint match" in r["source"]:
                                imprint_ranges.append(r)
                            elif "address" in r["source"]:
                                address_ranges.append(r)
                            elif "plate" in r["source"]:
                                plate_ranges.append(r)

                        # Format main table range
                        if main_table_range:
                            all_dates.append(
                                f"{main_table_range['range']} (main publisher table)"
                            )
                        else:
                            all_dates.append("No range provided in main table")

                        # Format imprint matches
                        if imprint_ranges:
                            for r in imprint_ranges:
                                all_dates.append(f"{r['range']} ({r['source']})")
                        else:
                            all_dates.append("No close imprint match")

                        # Format address matches
                        if address_ranges:
                            for r in address_ranges:
                                all_dates.append(f"{r['range']} ({r['source']})")
                        elif address:
                            all_dates.append("Address supplied but date not extracted")
                        else:
                            all_dates.append("No address supplied")

                        # Format plate number matches
                        if plate_ranges:
                            for r in plate_ranges:
                                if "bounds" in r["source"]:
                                    bounds = r["source"].split("from plate ")[1]
                                    all_dates.append(f"plate number bounds {bounds}")
                                else:
                                    all_dates.append(f"{r['range']} ({r['source']})")
                        elif plate:
                            all_dates.append(
                                "Not enough information in plate number table"
                            )
                        else:
                            all_dates.append("No plate number provided")

                        # Check if publisher was found but has no link
                        imslp_link = result["link"]
                        if (
                            not imslp_link
                            and result["source"] == "main publisher table"
                        ):
                            # Create a text fragment link using the exact name as it appears in the table
                            encoded_publisher = (
                                publisher.replace(" ", "%20")
                                .replace("&", "%26")
                                .replace(",", "%2C")
                            )
                            imslp_link = f"https://imslp.org/wiki/IMSLP:Music_Publishers#:~:text={encoded_publisher}"

                        detailed_results.append(
                            {
                                "Publisher": publisher,
                                "Address": address,
                                "City": city,
                                "Plate_number": plate,
                                "Estimated_Year": result["estimated_year"]
                                or "❌ Not found",
                                "IMSLP_Link": imslp_link,
                                "Date_Source": result["source"],
                                "Found_Dates": "\n• " + "\n• ".join(all_dates),
                                "date_estimation_analysis": result[
                                    "date_estimation_analysis"
                                ],
                            }
                        )
                except Exception as e:
                    estimates.append("❌ Error processing row")
                    detailed_results.append(
                        {
                            "Publisher": publisher,
                            "Address": address,
                            "City": city,
                            "Plate_number": plate,
                            "Estimated_Year": "❌ Error processing row",
                            "IMSLP_Link": "N/A",
                            "Date_Source": "N/A",
                            "Found_Dates": "N/A",
                            "date_estimation_analysis": {
                                "main_table": "Error processing row",
                                "imprint": "Error processing row",
                                "address": "Error processing row",
                                "plate": "Error processing row",
                            },
                        }
                    )

            df["Estimated_Year"] = estimates

            # Calculate and log statistics
            stats = {
                "total_entries": total_entries,
                "successful_estimates": successful_estimates,
                "publishers_not_found": publishers_not_found,
                "publishers_no_page": publishers_no_page,
                "success_rate": (
                    f"{(successful_estimates/total_entries*100):.1f}%"
                    if total_entries > 0
                    else "0%"
                ),
            }

            # Save simple output
            df["Estimated_Year"] = estimates
            simple_output_path = os.path.join(
                app.config["UPLOAD_FOLDER"], "output_simple.xlsx"
            )
            df.to_excel(simple_output_path, index=False)

            # Save detailed output
            df_detailed = pd.DataFrame(detailed_results)
            detailed_output_path = os.path.join(
                app.config["UPLOAD_FOLDER"], "output_detailed.xlsx"
            )
            df_detailed.to_excel(detailed_output_path, index=False)

            # Create cataloging output
            df_cataloging = create_cataloging_output(df, df_detailed)

            # Set pandas display options for better HTML rendering
            pd.set_option("display.max_colwidth", None)
            pd.set_option("display.max_columns", None)

            upload_progress["status"] = "completed"

            # After processing all rows, store in session if review mode
            if request.form.get("mode") == "review":
                session["review_data"] = detailed_results
                session["current_review_index"] = 0
                return redirect(url_for("review"))

            return render_template(
                "preview.html",
                tables=[
                    df.to_html(index=False, classes="table table-striped"),
                    df_detailed.to_html(index=False, classes="table table-striped"),
                    df_cataloging.to_html(index=False, classes="table table-striped"),
                ],
                download=True,
                has_detailed=True,
                stats=stats,
            )
        except Exception as e:
            upload_progress["status"] = "error"
            return f"❌ An error occurred while processing the file: {str(e)}"

    return render_template("upload.html")


def create_cataloging_output(df_simple, df_detailed):
    # Create cataloging DataFrame with all specified columns
    df_cataloging = pd.DataFrame(
        {
            "Title": df_simple.get(
                "Title", ""
            ),  # Use get() to handle missing columns gracefully
            "Statement of responsibility": df_simple.get(
                "Statement of responsibility", ""
            ),
            "Edition": df_simple.get("Edition", ""),
            "City": df_simple["City"],
            "Publisher name": df_simple["Publisher name"],
            "Address": df_simple["Address"],
            "Plate number": df_simple["Plate number"],
            "Date": df_simple.get("Date", ""),
            "Image name": df_simple.get("Image name", ""),
            "Estimated_Year": df_simple["Estimated_Year"],
            "IMSLP_Link": df_detailed["IMSLP_Link"],
            "Date_Source": df_detailed["Date_Source"],
            "Found_Dates": df_detailed["Found_Dates"],
            "date_estimation_analysis": df_detailed["date_estimation_analysis"],
        }
    )

    # Save the cataloging output
    cataloging_output_path = os.path.join(app.config["UPLOAD_FOLDER"], "output_cataloging.xlsx")
    df_cataloging.to_excel(cataloging_output_path, index=False)
    return df_cataloging


@app.route("/download/<file_type>")

def download(file_type):
    # If we have review data in the session, save it to Excel files first
    if "review_data" in session:
        review_data = session["review_data"]

        # Create DataFrames from the review data
        df_detailed = pd.DataFrame(review_data)

        # Create simple DataFrame with just the essential columns
        df_simple = pd.DataFrame(
            {
                "Publisher name": df_detailed["Publisher"],
                "Address": df_detailed["Address"],
                "City": df_detailed["City"],
                "Plate number": df_detailed["Plate_number"],
                "Estimated_Year": df_detailed["Estimated_Year"],
            }
        )

        # Save both files
        simple_output_path = os.path.join(
            app.config["UPLOAD_FOLDER"], "output_simple.xlsx"
        )
        detailed_output_path = os.path.join(
            app.config["UPLOAD_FOLDER"], "output_detailed.xlsx"
        )

        df_simple.to_excel(simple_output_path, index=False)
        df_detailed.to_excel(detailed_output_path, index=False)

        # Create and save cataloging output
        create_cataloging_output(df_simple, df_detailed)

    # Now send the requested file
    if file_type == "simple":
        return send_file(
            os.path.join(app.config["UPLOAD_FOLDER"], "output_simple.xlsx"),
            as_attachment=True,
            download_name="estimated_years.xlsx",
        )
    elif file_type == "detailed":
        return send_file(
            os.path.join(app.config["UPLOAD_FOLDER"], "output_detailed.xlsx"),
            as_attachment=True,
            download_name="estimated_years_detailed.xlsx",
        )
    elif file_type == "cataloging":
        return send_file(
            os.path.join(app.config["UPLOAD_FOLDER"], "output_cataloging.xlsx"),
            as_attachment=True,
            download_name="estimated_years_cataloging.xlsx",
        )
    else:
        return "Invalid download type", 400


TOOL_INFO = {
    "/batch-transcription": {
        "title": "Batch Transcribe",
        "desc": "Monitor a folder for sheet music images, transcribe them with AI, review and edit results, then export as CSV or XLSX.",
        "url": "/batch-transcription",
    },
    "/model-comparison": {
        "title": "Model Comparison",
        "desc": "Compare Harvard OpenAI and Harvard Gemini transcriptions across selected images, prompts, and models.",
        "url": "/model-comparison",
    },
    "/search": {
        "title": "Date Search",
        "desc": "Enter a publisher name, city, address, or plate number to estimate a publication date.",
        "url": "/search",
    },
    "/upload": {
        "title": "Excel Upload",
        "desc": "Upload an Excel file to estimate dates for many entries at once.",
        "url": "/upload",
    },
    "/image-transcription": {
        "title": "Image Transcribe",
        "desc": "Extract bibliographic metadata from a single sheet music image using AI.",
        "url": "/image-transcription",
    },
    "/test-match": {
        "title": "Publisher Match",
        "desc": "Test fuzzy matching against the IMSLP publisher list.",
        "url": "/test-match",
    },
    "/test-similarity": {
        "title": "Name Similarity",
        "desc": "Find publishers above a similarity threshold.",
        "url": "/test-similarity",
    },
    "/test-imprint": {
        "title": "Imprint Lookup",
        "desc": "Look up imprint date ranges from a publisher page.",
        "url": "/test-imprint",
    },
    "/test-address": {
        "title": "Address Lookup",
        "desc": "Find matching addresses and date ranges.",
        "url": "/test-address",
    },
    "/view_tables": {
        "title": "Plate Tables",
        "desc": "View plate number to year mapping tables.",
        "url": "/view_tables",
    },
    "/formula-generator": {
        "title": "Date Formulas",
        "desc": "Generate Excel formulas for date range checking.",
        "url": "/formula-generator",
    },
}


@app.before_request
def track_last_tool():
    path = request.path
    if path in TOOL_INFO:
        session["last_tool"] = path


@app.route("/")

def landing():
    last_tool_path = session.get("last_tool")
    if last_tool_path and last_tool_path in TOOL_INFO:
        featured = TOOL_INFO[last_tool_path]
        featured_label = "Recently used"
    else:
        featured = TOOL_INFO["/batch-transcription"]
        featured_label = "Try"
    return render_template("landing.html", featured=featured, featured_label=featured_label)


@app.route("/search", methods=["GET", "POST"])

def search():
    if request.method == "POST":
        publisher = request.form.get("publisher")
        address = request.form.get("address", "")
        plate = request.form.get("plate", "")
        city = request.form.get("city", "")
        result = estimate_year(publisher, plate, address, city)
        error = result.get("error")
        return render_template("result.html", result=result, error=error)

    return render_template("index.html")


@app.route("/test-match", methods=["GET", "POST"])

def test_match():
    match = None
    normalized_input = ""

    if request.method == "POST":
        input_name = request.form.get("publisher")
        normalized_input = normalize(input_name)

        raw_html = fetch_html(PUBLISHER_LIST_URL)
        publishers = extract_main_table_publishers(raw_html)
        normalized_names = [pub["normalized"] for pub in publishers]

        best_match = get_close_matches(
            normalized_input, normalized_names, n=1, cutoff=0.6
        )
        if best_match:
            idx = normalized_names.index(best_match[0])
            match = publishers[idx]

    return render_template("test_match.html", match=match, norm=normalized_input)


@app.route("/test-similarity", methods=["GET", "POST"])

def test_similarity():
    matches = []
    input_name = ""
    cutoff = 0.75

    if request.method == "POST":
        input_name = request.form.get("publisher", "")
        try:
            cutoff = float(request.form.get("cutoff", "0.6"))
            if not (0 <= cutoff <= 1):
                raise ValueError
        except ValueError:
            return "❌ Cutoff must be a number between 0 and 1."

        raw_html = fetch_html(PUBLISHER_LIST_URL)
        if not raw_html:
            return "❌ Could not fetch IMSLP publisher list."

        publishers = extract_main_table_publishers(raw_html)
        norm_input = normalize(input_name)

        for pub in publishers:
            score = SequenceMatcher(None, norm_input, pub["normalized"]).ratio()
            if score >= cutoff:
                matches.append((pub["name"], score))

        matches.sort(key=lambda x: x[1], reverse=True)

    return render_template(
        "test_similarity.html", input_name=input_name, cutoff=cutoff, matches=matches
    )


@app.route("/test-imprint", methods=["GET", "POST"])

def test_imprint():
    imprint_result = None
    publisher_input = ""

    if request.method == "POST":
        publisher_input = request.form.get("publisher", "").strip()
        if not publisher_input:
            return "❌ Please provide a publisher name."

        publishers = get_cached_publishers()
        norm_input = normalize(publisher_input)
        match = next((p for p in publishers if p["normalized"] == norm_input), None)

        if not match:
            from difflib import get_close_matches

            normalized_names = [p["normalized"] for p in publishers]
            best_match = get_close_matches(
                norm_input, normalized_names, n=1, cutoff=0.6
            )
            if best_match:
                match = next(
                    (p for p in publishers if p["normalized"] == best_match[0]), None
                )

        if not match or not match.get("href"):
            imprint_result = "❌ Publisher not found or has no IMSLP page."
        else:
            wiki_html = fetch_html(match["href"])
            if wiki_html:
                soup = BeautifulSoup(wiki_html, "html.parser")
                imprints_header = soup.find("span", {"id": "Imprints"})
                if imprints_header:
                    ranges = find_imprint_year_ranges(
                        str(soup), target_imprint=publisher_input
                    )
                    if ranges:
                        imprint_result = [
                            {"range": r.format(), "source": r.source} for r in ranges
                        ]
                    else:
                        imprint_result = "❌ No matching imprints found."
                else:
                    imprint_result = "❌ No imprint section found."
            else:
                imprint_result = "❌ Failed to fetch publisher page."

    return render_template(
        "test_imprint.html",
        publisher_input=publisher_input,
        imprint_result=imprint_result,
    )


@app.route("/test-address", methods=["GET", "POST"])

def test_address():
    address_result = None
    publisher_input = ""
    address_input = ""
    city_input = ""

    if request.method == "POST":
        publisher_input = request.form.get("publisher", "").strip()
        address_input = request.form.get("address", "").strip()
        city_input = request.form.get("city", "").strip()

        if not publisher_input:
            return "❌ Please provide a publisher name."

        publishers = get_cached_publishers()
        norm_input = normalize(publisher_input)
        match = next((p for p in publishers if p["normalized"] == norm_input), None)

        if not match:
            from difflib import get_close_matches

            normalized_names = [p["normalized"] for p in publishers]
            best_match = get_close_matches(
                norm_input, normalized_names, n=1, cutoff=0.6
            )
            if best_match:
                match = next(
                    (p for p in publishers if p["normalized"] == best_match[0]), None
                )

        if not match or not match.get("href"):
            address_result = "❌ Publisher not found or has no IMSLP page."
        else:
            wiki_html = fetch_html(match["href"])
            if wiki_html:
                addresses = extract_addresses_from_html(wiki_html)
                if addresses:
                    matching_addresses = find_matching_addresses(
                        addresses, address_input, city_input
                    )
                    if matching_addresses:
                        address_result = [
                            {
                                "address": addr.address,
                                "city": addr.city,
                                "date": addr.date_range.format(),
                                "description": addr.description,
                            }
                            for addr in matching_addresses
                        ]
                    else:
                        address_result = "❌ No matching addresses found."
                else:
                    address_result = "❌ No addresses found in publisher page."
            else:
                address_result = "❌ Failed to fetch publisher page."

    return render_template(
        "test_address.html",
        publisher_input=publisher_input,
        address_input=address_input,
        city_input=city_input,
        address_result=address_result,
    )


@app.route("/review", methods=["GET", "POST"])

def review():
    if "review_data" not in session:
        return redirect(url_for("home"))

    current_index = session.get("current_review_index", 0)
    review_data = session["review_data"]

    if current_index >= len(review_data):
        # All rows reviewed, generate final output
        return redirect(url_for("download", file_type="detailed"))

    return render_template(
        "review.html",
        current_row=review_data[current_index],
        current_index=current_index,
        total_rows=len(review_data),
    )


@app.route("/next_review_row", methods=["POST"])

def next_review_row():
    data = request.get_json()
    current_index = data.get("current_index", 0)
    action = data.get("action", "")

    if "review_data" not in session:
        return jsonify({"redirect": url_for("home")})

    review_data = session["review_data"]

    if action == "agree":
        # Mark as reviewed
        review_data[current_index]["reviewed"] = True
        session["current_review_index"] = current_index + 1
    elif action == "later":
        # Mark for later review
        review_data[current_index]["review_later"] = True
        session["current_review_index"] = current_index + 1
    elif action == "back":
        # Go back one row
        session["current_review_index"] = max(0, current_index - 1)

    session["review_data"] = review_data

    if current_index + 1 >= len(review_data) and action != "back":
        return jsonify({"redirect": url_for("download", file_type="detailed")})

    return jsonify({"success": True})


@app.route("/save_review_edit", methods=["POST"])

def save_review_edit():
    if "review_data" not in session:
        return redirect(url_for("review"))

    review_data = session["review_data"]
    row_index = int(request.form.get("row_index", 0))

    # Update the data with form values
    review_data[row_index].update(
        {
            "Publisher": request.form.get("publisher", ""),
            "Address": request.form.get("address", ""),
            "City": request.form.get("city", ""),
            "Plate_number": request.form.get("plate_number", ""),
        }
    )

    # If data was edited, rerun estimation
    if request.form.get("rerun", False):
        try:
            result = estimate_year(
                review_data[row_index]["Publisher"],
                review_data[row_index]["Plate_number"],
                review_data[row_index]["Address"],
                review_data[row_index]["City"],
            )

            if not result.get("error"):
                review_data[row_index].update(
                    {
                        "Estimated_Year": (
                            result["estimated_year"] + " +user"
                            if result["estimated_year"]
                            else "❌ Not found"
                        ),
                        "IMSLP_Link": result["link"],
                        "Date_Source": result["source"] + " (rerun after edit)",
                        "Found_Dates": "\n• "
                        + "\n• ".join(
                            f"{r['range']} ({r['source']})"
                            for r in result["all_ranges"]
                        ),
                        "date_estimation_analysis": result["date_estimation_analysis"],
                    }
                )
        except Exception as e:
            pass

    session["review_data"] = review_data
    return redirect(url_for("review"))


@app.route("/view_tables", methods=["GET", "POST"])

def view_tables():
    if request.method == "POST":
        publisher = request.form.get("publisher", "").strip()
        publishers = get_cached_publishers()

        # Find the publisher
        normalized_input = normalize(publisher)
        found_publisher = next(
            (p for p in publishers if p["normalized"] == normalized_input), None
        )

        if not found_publisher or not found_publisher["href"]:
            return render_template("view_tables.html", error="Publisher not found")

        # Fetch and parse the page
        wiki_html = fetch_html(found_publisher["href"])
        if not wiki_html:
            return render_template(
                "view_tables.html", error="Could not fetch publisher page"
            )

        processed_tables = []
        json_table_entries = []  # Collect all JSON entries in one list

        # First try to extract from JSON-like arrays
        start_idx = 0
        while True:
            array_start = wiki_html.find('["', start_idx)
            if array_start == -1:
                break
            array_end = wiki_html.find("]", array_start)
            if array_end == -1:
                break

            array_content = wiki_html[array_start : array_end + 1]
            try:
                parts = json.loads(array_content)
                # Check if it's a valid plate entry (should have at least 7 elements)
                if len(parts) >= 7 and parts[0].strip().isdigit():
                    plate_num = parts[0]
                    year = (
                        parts[-1] if parts[-1] else parts[-2]
                    )  # Try last element, then second-to-last
                    if year and str(year).strip().isdigit():
                        json_table_entries.append({"plate": plate_num, "year": year})
            except:
                pass
            start_idx = array_end + 1

        # Add JSON entries as a single table if any were found
        if json_table_entries:
            processed_tables.append(json_table_entries)

        # Then process traditional HTML tables
        soup = BeautifulSoup(wiki_html, "html.parser")
        tables = soup.find_all("table", class_="wikitable")

        for table in tables:
            # Get headers
            headers = []
            header_row = table.find("tr")
            if header_row:
                headers = [
                    th.get_text(strip=True).lower()
                    for th in header_row.find_all(["th", "td"])
                ]

                # Find plate and year columns
                plate_col = next(
                    (
                        i
                        for i, h in enumerate(headers)
                        if (h.lower() == "plate" or "plate #" in h.lower())
                        and "full" not in h.lower()
                    ),
                    None,
                )
                year_col = next(
                    (
                        i
                        for i, h in enumerate(headers)
                        if "year" in h.lower() and "full" not in h.lower()
                    ),
                    None,
                )

                if plate_col is not None and year_col is not None:
                    table_data = []
                    for row in table.find_all("tr")[1:]:  # Skip header
                        cells = row.find_all(["td", "th"])
                        if len(cells) > max(plate_col, year_col):
                            plate = cells[plate_col].get_text(strip=True)
                            year = cells[year_col].get_text(strip=True)
                            if (
                                plate or year
                            ):  # Only add rows where at least one value exists
                                table_data.append({"plate": plate, "year": year})
                    if table_data:  # Only add tables that have data
                        processed_tables.append(table_data)

        # Add table type information for display
        tables_with_info = []
        for idx, table in enumerate(processed_tables):
            is_json = idx < len(processed_tables) - len(
                [t for t in tables if has_plate_year_cols(t)]
            )
            tables_with_info.append(
                {
                    "data": table,
                    "type": "JSON Array" if is_json else "HTML Table",
                    "index": idx + 1,
                }
            )

        return render_template(
            "view_tables.html",
            publisher=found_publisher["name"],
            publisher_url=found_publisher["href"],
            tables=tables_with_info,
        )

    return render_template("view_tables.html")


def has_plate_year_cols(table):
    """Helper function to check if a BeautifulSoup table has plate and year columns"""
    header_row = table.find("tr")
    if not header_row:
        return False

    headers = [
        th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])
    ]

    has_plate = any(
        h for h in headers if (h == "plate" or "plate #" in h) and "full" not in h
    )
    has_year = any(h for h in headers if "year" in h and "full" not in h)

    return has_plate and has_year


def clean_plate_number(plate: str) -> str:
    """Clean plate numbers, handling Excel date conversion and other issues.

    Args:
        plate: Raw plate number string

    Returns:
        Cleaned plate number string
    """
    if not plate or pd.isna(plate):
        return ""

    plate = str(plate).strip()

    # Handle Excel date conversion (e.g. "1979-07-01 00:00:00")
    if " 00:00:00" in plate:
        # Extract just the date part and convert back to original format
        date_part = plate.split(" ")[0]  # e.g. "1979-07-01"
        parts = date_part.split("-")
        if len(parts) >= 2:
            # Convert back to original format (e.g. "1979-7")
            plate = f"{parts[0]}-{int(parts[1])}"

    # Remove any trailing .0 from Excel number conversion
    if plate.endswith(".0"):
        plate = plate[:-2]

    return plate


@app.route("/formula-generator", methods=["GET", "POST"])

def formula_generator():
    """Generate Excel formulas for date range checking."""
    if request.method == "POST":
        cell_reference = request.form.get("cellReference", "").strip().upper()

        # Validate cell reference format
        import re

        if not re.match(r"^[A-Z]+[0-9]+$", cell_reference):
            return render_template(
                "formula_generator.html", error="Invalid cell reference format"
            )

        # Generate the formula
        years = list(range(193, 203))  # 193 to 202
        search_conditions = []

        for year in years:
            search_conditions.append(
                f'ISNUMBER(SEARCH("{year}",TEXT({cell_reference},"@")))'
            )

        formula = f'=IF(OR({",".join(search_conditions)}),"R","")'

        return render_template("formula_generator.html", formula=formula)

    return render_template("formula_generator.html")


# Image transcription functionality - exact copy from Colab notebook
def encode_image(path):
    """Encode image to base64."""
    from io import BytesIO
    img = Image.open(path)
    try:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG")
        encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
        return encoded
    finally:
        img.close()




def _call_harvard(base64_image, prompt, api_key, model):
    import time as _time
    data = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                    },
                ],
            }
        ],
        "max_completion_tokens": 3000,
    }
    endpoint_count = len(HARVARD_OPENAI_CHAT_ENDPOINTS)
    for endpoint_index, (url, api_key_header) in enumerate(HARVARD_OPENAI_CHAT_ENDPOINTS):
        headers = {"Content-Type": "application/json", api_key_header: api_key}
        last_exc = None
        for attempt in range(3):
            t0 = _time.time()
            try:
                response = requests.post(url, headers=headers, json=data, timeout=120)
            except requests.exceptions.Timeout:
                logger.warning("Harvard HUIT timeout after %.1fs (attempt %d)", _time.time() - t0, attempt + 1)
                last_exc = Exception("API request timed out. Please try again.")
                _time.sleep(2 ** attempt)
                continue
            except requests.exceptions.ConnectionError:
                raise Exception("Could not connect to the API. Please check your network connection.")
            except requests.exceptions.RequestException as e:
                raise Exception("API request failed. Please try again.") from None
            elapsed = _time.time() - t0
            logger.info(
                "Harvard HUIT response from %s: %d in %.1fs (attempt %d)",
                url,
                response.status_code,
                elapsed,
                attempt + 1,
            )
            if response.status_code in (502, 503, 504):
                last_exc = Exception(f"API gateway returned {response.status_code}. Retrying…")
                _time.sleep(2 ** attempt)
                continue
            break
        else:
            raise last_exc
        if response.status_code not in (401, 403) or endpoint_index == endpoint_count - 1:
            break
        logger.info("Harvard HUIT endpoint unauthorized; trying legacy endpoint")
    if response.status_code != 200:
        if response.status_code in (401, 403):
            raise Exception('Your API key is invalid or not approved. <a href="/settings">Update it in Settings</a>.')
        try:
            error_data = response.json()
            if "error" in error_data:
                error_msg = error_data["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                raise Exception(f"API error ({response.status_code}): {error_msg}")
        except json.JSONDecodeError:
            pass
        raise Exception(f"API returned status code {response.status_code}: {response.text[:200]}")
    try:
        response_data = response.json()
    except json.JSONDecodeError as e:
        raise Exception(f"Failed to parse API response as JSON: {str(e)}")
    if "choices" not in response_data:
        if "error" in response_data:
            error_msg = response_data["error"]
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise Exception(f"API error: {error_msg}")
        raise Exception(f"Unexpected API response structure: {list(response_data.keys())}")
    if not response_data["choices"]:
        raise Exception("API returned empty choices array")
    try:
        return response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise Exception(f"Could not extract content from API response: {str(e)}")


def _call_openai_standard(base64_image, prompt, api_key, model):
    import time as _time
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    data = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                    },
                ],
            }
        ],
        "max_completion_tokens": 3000,
    }
    last_exc = None
    for attempt in range(3):
        t0 = _time.time()
        try:
            response = requests.post(url, headers=headers, json=data, timeout=120)
        except requests.exceptions.Timeout:
            logger.warning("OpenAI timeout after %.1fs (attempt %d)", _time.time() - t0, attempt + 1)
            last_exc = Exception("API request timed out. Please try again.")
            _time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError:
            raise Exception("Could not connect to the OpenAI API. Please check your network connection.")
        except requests.exceptions.RequestException as e:
            raise Exception("API request failed. Please try again.") from None
        elapsed = _time.time() - t0
        logger.info("OpenAI response: %d in %.1fs (attempt %d)", response.status_code, elapsed, attempt + 1)
        if response.status_code in (502, 503, 504):
            last_exc = Exception(f"API gateway returned {response.status_code}. Retrying…")
            _time.sleep(2 ** attempt)
            continue
        break
    else:
        raise last_exc
    if response.status_code != 200:
        if response.status_code in (401, 403):
            raise Exception('Your OpenAI API key is invalid. <a href="/settings">Update it in Settings</a>.')
        try:
            error_data = response.json()
            if "error" in error_data:
                error_msg = error_data["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                raise Exception(f"OpenAI API error ({response.status_code}): {error_msg}")
        except json.JSONDecodeError:
            pass
        raise Exception(f"OpenAI API returned status {response.status_code}: {response.text[:200]}")
    try:
        response_data = response.json()
    except json.JSONDecodeError as e:
        raise Exception(f"Failed to parse API response as JSON: {str(e)}")
    if "choices" not in response_data:
        if "error" in response_data:
            error_msg = response_data["error"]
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise Exception(f"OpenAI API error: {error_msg}")
        raise Exception(f"Unexpected API response structure: {list(response_data.keys())}")
    if not response_data["choices"]:
        raise Exception("API returned empty choices array")
    try:
        return response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise Exception(f"Could not extract content from API response: {str(e)}")


def _call_anthropic(base64_image, prompt, api_key, model):
    import time as _time
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    data = {
        "model": model,
        "max_tokens": 3000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64_image,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    last_exc = None
    for attempt in range(3):
        t0 = _time.time()
        try:
            response = requests.post(url, headers=headers, json=data, timeout=120)
        except requests.exceptions.Timeout:
            logger.warning("Anthropic timeout after %.1fs (attempt %d)", _time.time() - t0, attempt + 1)
            last_exc = Exception("API request timed out. Please try again.")
            _time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError:
            raise Exception("Could not connect to the Anthropic API. Please check your network connection.")
        except requests.exceptions.RequestException as e:
            raise Exception("API request failed. Please try again.") from None
        elapsed = _time.time() - t0
        logger.info("Anthropic response: %d in %.1fs (attempt %d)", response.status_code, elapsed, attempt + 1)
        if response.status_code in (502, 503, 504, 529):
            last_exc = Exception(f"API returned {response.status_code}. Retrying…")
            _time.sleep(2 ** attempt)
            continue
        break
    else:
        raise last_exc
    if response.status_code != 200:
        if response.status_code in (401, 403):
            raise Exception('Your Anthropic API key is invalid. <a href="/settings">Update it in Settings</a>.')
        try:
            error_data = response.json()
            if "error" in error_data:
                error_msg = error_data["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                raise Exception(f"Anthropic API error ({response.status_code}): {error_msg}")
        except json.JSONDecodeError:
            pass
        raise Exception(f"Anthropic API returned status {response.status_code}: {response.text[:200]}")
    try:
        response_data = response.json()
    except json.JSONDecodeError as e:
        raise Exception(f"Failed to parse Anthropic response as JSON: {str(e)}")
    if "content" not in response_data or not response_data["content"]:
        if "error" in response_data:
            error_msg = response_data["error"]
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise Exception(f"Anthropic API error: {error_msg}")
        raise Exception(f"Unexpected Anthropic response structure: {list(response_data.keys())}")
    return response_data["content"][0]["text"]


def _call_gemini(base64_image, prompt, api_key, model):
    import time as _time
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    data = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": base64_image}},
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {"maxOutputTokens": 3000},
    }
    last_exc = None
    for attempt in range(3):
        t0 = _time.time()
        try:
            response = requests.post(url, headers=headers, json=data, timeout=120)
        except requests.exceptions.Timeout:
            logger.warning("Gemini timeout after %.1fs (attempt %d)", _time.time() - t0, attempt + 1)
            last_exc = Exception("API request timed out. Please try again.")
            _time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError:
            raise Exception("Could not connect to the Gemini API. Please check your network connection.")
        except requests.exceptions.RequestException as e:
            raise Exception("API request failed. Please try again.") from None
        elapsed = _time.time() - t0
        logger.info("Gemini response: %d in %.1fs (attempt %d)", response.status_code, elapsed, attempt + 1)
        if response.status_code in (502, 503, 504):
            last_exc = Exception(f"API returned {response.status_code}. Retrying…")
            _time.sleep(2 ** attempt)
            continue
        break
    else:
        raise last_exc
    if response.status_code != 200:
        if response.status_code in (401, 403):
            raise Exception('Your Gemini API key is invalid. <a href="/settings">Update it in Settings</a>.')
        try:
            error_data = response.json()
            if "error" in error_data:
                error_msg = error_data["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                raise Exception(f"Gemini API error ({response.status_code}): {error_msg}")
        except json.JSONDecodeError:
            pass
        raise Exception(f"Gemini API returned status {response.status_code}: {response.text[:200]}")
    try:
        response_data = response.json()
    except json.JSONDecodeError as e:
        raise Exception(f"Failed to parse Gemini response as JSON: {str(e)}")
    try:
        candidates = response_data.get("candidates", [])
        if not candidates:
            raise Exception("Gemini returned no candidates")
        return candidates[0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise Exception(f"Could not extract content from Gemini response: {str(e)}")


def _call_harvard_gemini(base64_image, prompt, api_key, model):
    import time as _time
    url = HARVARD_GEMINI_ENDPOINT_TEMPLATE.format(model=model)
    headers = {"Content-Type": "application/json", "X-Api-Key": api_key}
    data = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": base64_image}},
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {"maxOutputTokens": 3000},
    }
    last_exc = None
    for attempt in range(3):
        t0 = _time.time()
        try:
            response = requests.post(url, headers=headers, json=data, timeout=120)
        except requests.exceptions.Timeout:
            logger.warning("Harvard Gemini timeout after %.1fs (attempt %d)", _time.time() - t0, attempt + 1)
            last_exc = Exception("API request timed out. Please try again.")
            _time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError:
            raise Exception("Could not connect to the Harvard Gemini API. Please check your network connection.")
        except requests.exceptions.RequestException as e:
            raise Exception("API request failed. Please try again.") from None
        elapsed = _time.time() - t0
        logger.info("Harvard Gemini response: %d in %.1fs (attempt %d)", response.status_code, elapsed, attempt + 1)
        if response.status_code in (502, 503, 504):
            last_exc = Exception(f"API returned {response.status_code}. Retrying…")
            _time.sleep(2 ** attempt)
            continue
        break
    else:
        raise last_exc
    if response.status_code != 200:
        if response.status_code in (401, 403):
            raise Exception('Your saved Harvard HUIT key is invalid or not approved for Gemini. <a href="/settings">Update it in Settings</a>.')
        try:
            error_data = response.json()
            if "error" in error_data:
                error_msg = error_data["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                raise Exception(f"Harvard Gemini API error ({response.status_code}): {error_msg}")
        except json.JSONDecodeError:
            pass
        raise Exception(f"Harvard Gemini API returned status {response.status_code}: {response.text[:200]}")
    try:
        response_data = response.json()
    except json.JSONDecodeError as e:
        raise Exception(f"Failed to parse Harvard Gemini response as JSON: {str(e)}")
    try:
        candidates = response_data.get("candidates", [])
        if not candidates:
            raise Exception("Harvard Gemini returned no candidates")
        return candidates[0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise Exception(f"Could not extract content from Harvard Gemini response: {str(e)}")


def image_to_text(image_path, api_key, custom_prompt=None, provider=None, model=None):
    default_prompt = get_default_prompt()
    prompt_to_use = custom_prompt if custom_prompt else default_prompt
    base64_image = encode_image(image_path)

    if provider is None:
        provider = session.get("active_provider", "harvard")
    if model is None:
        model = get_active_model()

    if provider == "anthropic":
        return _call_anthropic(base64_image, prompt_to_use, api_key, model)
    elif provider == "harvard_gemini":
        return _call_harvard_gemini(base64_image, prompt_to_use, api_key, model)
    elif provider == "gemini":
        return _call_gemini(base64_image, prompt_to_use, api_key, model)
    elif provider == "openai":
        return _call_openai_standard(base64_image, prompt_to_use, api_key, model)
    else:
        return _call_harvard(base64_image, prompt_to_use, api_key, model)


def _clean_transcription_value(value):
    if value is None:
        return ""
    return str(value).strip()


def _parse_transcription_result(raw_response, image_name):
    data = json.loads(raw_response)
    if not isinstance(data, dict):
        raise ValueError("Response JSON was not an object")
    result = {
        field: _clean_transcription_value(data.get(field, ""))
        for field in TRANSCRIPTION_FIELDS
    }
    result["image_name"] = image_name
    return result


def _comparison_model_options():
    options = []
    for provider in COMPARISON_PROVIDERS:
        provider_info = API_PROVIDERS[provider]
        for model, model_name in provider_info.get("models", {}).items():
            options.append(
                {
                    "provider": provider,
                    "provider_name": provider_info["name"],
                    "model": model,
                    "model_name": model_name,
                    "default": model == provider_info.get("default_model"),
                }
            )
    return options


def _comparison_key_options():
    return [
        {
            "provider": "harvard",
            "name": "Saved Harvard HUIT OpenAI key",
            "description": "Use the key saved under Settings > Harvard HUIT for both Harvard OpenAI and Harvard Gemini.",
        }
    ]


def _parse_comparison_model_selection(raw_models):
    selections = []
    seen = set()
    for item in raw_models or []:
        if isinstance(item, str):
            provider, sep, model = item.partition("|")
            if not sep:
                provider, sep, model = item.partition(":")
        elif isinstance(item, dict):
            provider = item.get("provider", "")
            model = item.get("model", "")
        else:
            continue
        provider = str(provider).strip()
        model = str(model).strip()
        key = (provider, model)
        if key in seen:
            continue
        if provider not in COMPARISON_PROVIDERS:
            raise ValueError("Unsupported comparison provider")
        if model not in API_PROVIDERS[provider].get("models", {}):
            raise ValueError("Unsupported comparison model")
        seen.add(key)
        selections.append(
            {
                "provider": provider,
                "provider_name": API_PROVIDERS[provider]["name"],
                "model": model,
                "model_name": API_PROVIDERS[provider]["models"][model],
            }
        )
    return selections


# Helper function to get the default prompt for display in UI
def get_default_prompt():
    """Return the default prompt used for image transcription"""
    return (
        "You are an archivist working on creating structured data from an image."
        "You will need to manually transcribe the data from the images."
        "You will extract the information from the image and present it as a JSON object with eight fields: 'title', 'statement_of_responsibility', 'edition', 'city', 'publisher_name', 'address', 'plate_number', 'date'."
        "Please don't use markdown to present the result."
        "Use ISBD punctuation and RDA (Resource Description & Access) capitalization rules. For instance, initials and acronyms are to be transcribed without internal spaces, regardless of how they are presented on the image. For example, 'F.K.E. Kennedy', NOT 'F. K. E. Kennedy'. "
        "Your response should only contain the data requested. No additional explanation and no extra information. Only transcribe information which is directly stated in the scan. Do not add any outside information, speculation, or generative text. Stick strictly to the text presented in the scan. If you cannot read, or cannot correctly transcribe something, you may return a result of '[illegible]' particularly for text in very small fonts. "
        "Do not use superscript at any time. "
        "First extract the title. The title field should be COMPLETE, including the subtitles, instrumentation, and any phrases beginning with 'Sung by …', or 'Crée par …', in the order they appear on the image. If the image has subtitles, please put a ' : ' between them and the title, such as 'Andante et mazurka : pour flûte avec accompagnement de piano'. "
        "Capitalization: only the first letter of the first word of each title or subtitle should be a capital even when the title has been changed from all caps, and the first letter of the first word of parallel titles, alternative titles, or section titles should also be a capital, again, even when the title has been changed from all caps. Other capitalization should follow the appropriate usage for the language used in the title. For instance, in German, be sure to capitalize nouns. "
        "If there is an opus number anywhere in the image, put it at the end of the title field separated from the titles by an ellipsis, following the format 'Le réveil des fées : étude pour piano ... op. 41'. "
        "If there is anything resembling a series title, or series-like title, transcribe this field at the end of the title field, and enclose it in parentheses. Do not supply a series title if you don't find one. "
        "Second, extract the statement_of_responsibility field, should also transcribe words like 'by', 'par', etc. exactly as they appear in the image. Do NOT add any publication statements into the statement_of_responsibility, even when no statement_of_responsibility is found. "
        "Third, extract the 'edition' field, if there is any kind of statement on the image with the word 'edition' or 'ed.' in it, in any language. Please look carefully for these edition statements, as they can be found at the head of the title, anywhere within the main title information, or even at the foot of the page. If you cannot find an edition statement, leave this field blank. "
        "Fourth, extract the 'city' field. This will be the predominant city of publication."
        "Fifth, extract the 'publisher_name' field. If 'city', or 'publisher_name', are not found, DO NOT SUPPLY OR PREDICT. Prefer publisher from caption title rather than copyright statement. "
        "Transcribe the publisher's address into the 'address' field, which should follow the 'publisher_name' field and precede the 'plate_number' field."
        "Sixth, extract any numbers, or initials with subsequent numbers found at bottom of pages of music, into the 'plate_number' field. They should be transcribed exactly as found without any additional text or prefixes. So for instance, the phrase 'A. C. 36782' would appear in the plate_number field as 'A.C. 36782'. The phrase '482' would appear in the plate_number field as '482' . If no such numbers are not found, DO NOT SUPPLY OR PREDICT, but leave the 'plate_number' field! "
        "Seventh, extract date field. Dates sometimes appear in small print at the bottom of the page, or under a caption title, in copyright statements like this one: 'Entered according to act of Congress in the year 1867 ...' where 1867 would be the date. Transcribe dates as found in this format without square brackets, prefaced by '©'; in this example you would supply '©1867'. Supply 'undated' if no dates are found. Please look carefully for this kind of copyright statement, as it can be in a very small font. "
        "When a copyright date is presented in Roman numerals, please transcribe the Roman numeral as it appears, then include the Arabic numerals in parentheses. '©MDCCCXCIV' should be transcribed as '©MDCCCXCIV (1894)' ."
        "Finally, please format the output as a JSON object with keys: 'title', 'statement_of_responsibility', 'edition', 'city', 'publisher_name', 'address', 'plate_number', 'date' ."
    )


@app.route("/image-transcription", methods=["GET", "POST"])

def image_transcription():
    """Process a single image for bibliographic data extraction"""
    if request.method == "POST":
        image_path = normalize_path(request.form.get("image_path", ""))
        custom_prompt = request.form.get("custom_prompt", "").strip()
        uploaded_file = request.files.get("image_file")
        uploaded_path = None

        cloud_source = request.form.get("cloud_source", "").strip()
        cloud_item_id = request.form.get("cloud_item_id", "").strip()
        cloud_filename = request.form.get("cloud_filename", "").strip()

        if uploaded_file and uploaded_file.filename:
            allowed_ext = {"jpg", "jpeg", "png", "tiff", "tif", "bmp", "gif", "webp"}
            ext = uploaded_file.filename.rsplit(".", 1)[-1].lower() if "." in uploaded_file.filename else ""
            if ext not in allowed_ext:
                return render_template(
                    "image_transcription.html",
                    error=f"Unsupported file type: .{ext}. Use JPG, PNG, or TIFF.",
                    default_prompt=get_default_prompt(),
                )
            filename = secure_filename(uploaded_file.filename)
            upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], "images")
            os.makedirs(upload_dir, exist_ok=True)
            uploaded_path = os.path.join(upload_dir, filename)
            uploaded_file.save(uploaded_path)
            image_path = uploaded_path
        elif cloud_source and cloud_item_id:
            token = onedrive_get_token()
            if not token:
                return render_template("image_transcription.html", error="Microsoft account not connected. Please connect via Batch Transcription first.", default_prompt=get_default_prompt())
            if cloud_source == "sharepoint" and ":" in cloud_item_id:
                site_id, item_id = cloud_item_id.split(":", 1)
                dl_url = f"{GRAPH_API}/sites/{site_id}/drive/items/{item_id}/content"
            else:
                dl_url = f"{GRAPH_API}/me/drive/items/{cloud_item_id}/content"
            resp = requests.get(dl_url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
            if resp.status_code != 200:
                return render_template("image_transcription.html", error="Could not download image from cloud storage.", default_prompt=get_default_prompt())
            fname = secure_filename(cloud_filename or "cloud_image.jpg")
            cache_dir = os.path.join(app.config["UPLOAD_FOLDER"], "cloud_cache")
            os.makedirs(cache_dir, exist_ok=True)
            image_path = os.path.join(cache_dir, fname)
            with open(image_path, "wb") as f:
                f.write(resp.content)

        api_key, active_provider = get_active_api_key()

        if not image_path:
            return render_template(
                "image_transcription.html",
                error="Please upload an image or provide a file path.",
                default_prompt=get_default_prompt(),
            )

        if not api_key:
            return render_template(
                "image_transcription.html",
                error=f'No API key found for {API_PROVIDERS.get(active_provider, {}).get("name", active_provider)}. <a href="/settings">Add one in Settings</a>.',
                default_prompt=get_default_prompt(),
            )

        if not os.path.exists(image_path):
            return render_template(
                "image_transcription.html",
                error="Image file not found at the specified path.",
                default_prompt=get_default_prompt(),
            )

        try:
            # Process the image using the exact same function from Colab
            # Use custom prompt if provided, otherwise use default
            prompt_to_use = custom_prompt if custom_prompt else None
            raw_response = image_to_text(image_path, api_key, prompt_to_use, provider=active_provider)

            # Parse the JSON response
            try:
                data = json.loads(raw_response)

                # Extract the fields exactly as in the Colab notebook
                result = {
                    "title": data.get("title", "").strip(),
                    "statement_of_responsibility": data.get(
                        "statement_of_responsibility", ""
                    ).strip(),
                    "edition": data.get("edition", "").strip(),
                    "city": data.get("city", "").strip(),
                    "publisher_name": data.get("publisher_name", "").strip(),
                    "address": data.get("address", "").strip(),
                    "plate_number": data.get("plate_number", "").strip(),
                    "date": data.get("date", "").strip(),
                    "image_name": os.path.basename(image_path),
                }

                return render_template(
                    "image_transcription.html",
                    result=result,
                    raw_response=raw_response,
                    image_path=image_path,
                    show_edit_form=True,
                    default_prompt=get_default_prompt(),
                    used_prompt=(
                        prompt_to_use if prompt_to_use else get_default_prompt()
                    ),
                )

            except json.JSONDecodeError as e:
                return render_template(
                    "image_transcription.html",
                    error=f"Error parsing JSON response: {e}",
                    raw_response=raw_response,
                    image_path=image_path,
                    default_prompt=get_default_prompt(),
                )

        except Exception as e:
            return render_template(
                "image_transcription.html",
                error=f"Error processing image: {e}",
                image_path=image_path,
                default_prompt=get_default_prompt(),
            )

    return render_template(
        "image_transcription.html", default_prompt=get_default_prompt()
    )


@app.route("/save-transcription", methods=["POST"])

def save_transcription():
    """Save edited transcription data as CSV"""
    try:
        # Get edited data from form
        edited_data = {
            "title": request.form.get("edit_title", "").strip(),
            "statement_of_responsibility": request.form.get(
                "edit_statement_of_responsibility", ""
            ).strip(),
            "edition": request.form.get("edit_edition", "").strip(),
            "city": request.form.get("edit_city", "").strip(),
            "publisher_name": request.form.get("edit_publisher_name", "").strip(),
            "address": request.form.get("edit_address", "").strip(),
            "plate_number": request.form.get("edit_plate_number", "").strip(),
            "date": request.form.get("edit_date", "").strip(),
            "image_name": request.form.get("edit_image_name", "").strip(),
        }

        # Create CSV content with the exact same headers as Colab notebook
        csv_headers = [
            "Title",
            "Statement of responsibility",
            "Edition",
            "City",
            "Publisher name",
            "Address",
            "Plate number",
            "Date",
            "Image name",
        ]

        csv_values = [
            edited_data["title"],
            edited_data["statement_of_responsibility"],
            edited_data["edition"],
            edited_data["city"],
            edited_data["publisher_name"],
            edited_data["address"],
            edited_data["plate_number"],
            edited_data["date"],
            edited_data["image_name"],
        ]

        # Create CSV content
        csv_content = ",".join(csv_headers) + "\n"
        # Escape double quotes in CSV values and wrap in quotes
        escaped_values = [
            '"' + str(value).replace('"', '""') + '"' for value in csv_values
        ]
        csv_content += ",".join(escaped_values)

        # Generate filename based on image name
        image_name = edited_data["image_name"]
        safe_filename = re.sub(r"[^\w\-_\.]", "_", image_name.split(".")[0])
        csv_filename = f"transcription_{safe_filename}.csv"

        # Create response with CSV download
        response = Response(
            csv_content,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{csv_filename}"'},
        )

        return response

    except Exception as e:
        return render_template(
            "image_transcription.html", error=f"Error saving transcription: {e}"
        )


@app.route("/batch-transcription", methods=["GET", "POST"])

def batch_transcription():
    """Batch image transcription with folder monitoring"""
    if request.method == "POST":
        action = request.form.get("action")

        if action == "set_folder":
            folder_path = normalize_path(request.form.get("folder_path", ""))
            if not folder_path:
                return render_template(
                    "batch_transcription.html",
                    **_batch_template_context(error="Please provide a folder path"),
                )

            if not os.path.exists(folder_path):
                return render_template(
                    "batch_transcription.html",
                    **_batch_template_context(error="Folder path does not exist"),
            )

            session["batch_folder_path"] = folder_path
            return render_template("batch_transcription.html", **_batch_template_context())

        elif action == "save_prompt":
            batch_prompt = request.form.get("batch_prompt", "").strip()
            if batch_prompt:
                session["batch_prompt"] = batch_prompt
                return "OK"  # Simple response for AJAX
            else:
                return "Error: Empty prompt", 400

        elif action == "change_folder":
            session.pop("current_run", None)
            session.pop("batch_folder_path", None)
            return render_template("batch_transcription.html", **_batch_template_context())

        elif action == "end_run":
            session.pop("current_run", None)
            return render_template("batch_transcription.html", **_batch_template_context())

        elif action == "start_run":
            if "batch_folder_path" not in session:
                return render_template(
                    "batch_transcription.html",
                    **_batch_template_context(error="Please set a folder path first"),
                )

            run_id = _new_batch_run_id()
            session["current_run"] = {
                "id": run_id,
                "started_at": datetime.now().isoformat(),
            }
            _save_batch_results(run_id, [])

            return render_template("batch_transcription.html", **_batch_template_context())

    return render_template("batch_transcription.html", **_batch_template_context())


@app.route("/batch-upload-images", methods=["POST"])
def batch_upload_images():
    files = request.files.getlist("batch_images")
    if not files:
        flash("No images selected.", "error")
        return redirect(url_for("batch_transcription"))
    allowed_ext = {"jpg", "jpeg", "png", "tiff", "tif", "bmp", "gif", "webp"}
    upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], "batch_uploads", datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(upload_dir, exist_ok=True)
    count = 0
    for f in files:
        if f and f.filename:
            ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
            if ext in allowed_ext:
                f.save(os.path.join(upload_dir, secure_filename(f.filename)))
                count += 1
    if count == 0:
        flash("No valid image files found. Use JPG, PNG, or TIFF.", "error")
        return redirect(url_for("batch_transcription"))
    session["batch_source"] = "local"
    session["batch_folder_path"] = upload_dir
    session.modified = True
    flash(f"Uploaded {count} images.", "success")
    return redirect(url_for("batch_transcription"))


@app.route("/get-default-prompt")

def get_default_prompt_api():
    """Return the default prompt for AJAX requests"""
    return jsonify({"default_prompt": get_default_prompt()})


def _comparison_template_context(error=None, **extra):
    context = {
        "error": error,
        "folder_path": session.get("comparison_folder_path"),
        "default_prompt": get_default_prompt(),
        "key_options": _comparison_key_options(),
        "model_options": _comparison_model_options(),
        "max_images": MAX_COMPARISON_IMAGES,
    }
    context.update(extra)
    return context


@app.route("/model-comparison", methods=["GET", "POST"])
def model_comparison():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "set_folder":
            folder_path = normalize_path(request.form.get("folder_path", ""))
            if not folder_path:
                return render_template(
                    "model_comparison.html",
                    **_comparison_template_context(error="Please provide a folder path"),
                )
            if not os.path.isdir(folder_path):
                return render_template(
                    "model_comparison.html",
                    **_comparison_template_context(error="Folder path does not exist"),
                )
            session["comparison_folder_path"] = folder_path
            session.modified = True
        elif action == "change_folder":
            session.pop("comparison_folder_path", None)
            session.modified = True
        return render_template("model_comparison.html", **_comparison_template_context())

    return render_template("model_comparison.html", **_comparison_template_context())


@app.route("/model-comparison/images")
def model_comparison_images():
    folder_path = session.get("comparison_folder_path")
    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"error": "Invalid folder path"}), 400
    images = _list_local_folder_images(folder_path)
    return jsonify({"images": images, "count": len(images)})


@app.route("/model-comparison/run", methods=["POST"])
def model_comparison_run():
    payload = request.get_json(silent=True) or {}
    image_paths = payload.get("image_paths") or []
    if not isinstance(image_paths, list) or not image_paths:
        return jsonify({"error": "Select at least one image"}), 400
    if len(image_paths) > MAX_COMPARISON_IMAGES:
        return jsonify({"error": f"Select no more than {MAX_COMPARISON_IMAGES} images"}), 400

    try:
        selected_models = _parse_comparison_model_selection(payload.get("models") or [])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not selected_models:
        return jsonify({"error": "Select at least one model"}), 400

    key_provider = str(payload.get("key_provider") or "harvard").strip()
    if key_provider not in COMPARISON_KEY_PROVIDERS:
        return jsonify({"error": "Unsupported comparison API key"}), 400

    folder_path = session.get("comparison_folder_path")
    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"error": "Invalid folder path"}), 400

    available_images = {
        os.path.abspath(img["path"]): img
        for img in _list_local_folder_images(folder_path)
    }
    selected_images = []
    for raw_path in image_paths:
        image_path = os.path.abspath(normalize_path(str(raw_path)))
        image_info = available_images.get(image_path)
        if not image_info or not _path_is_inside(folder_path, image_path):
            return jsonify({"error": "Selected image is not in the current folder"}), 400
        selected_images.append(image_info)

    api_key = get_api_key_for_provider(key_provider)
    if not api_key:
        return jsonify({"error": 'No saved Harvard HUIT OpenAI key found. <a href="/settings">Add one under Harvard HUIT in Settings</a>, then reuse it for all selected Harvard OpenAI and Harvard Gemini models.'}), 400

    prompt = str(payload.get("prompt") or "").strip() or get_default_prompt()
    combination_count = len(selected_images) * len(selected_models)

    rows = []
    for image_info in selected_images:
        image_path = image_info["path"]
        cells = []
        for selected in selected_models:
            provider = selected["provider"]
            model = selected["model"]
            cell = dict(selected)
            raw_response = ""
            try:
                raw_response = image_to_text(
                    image_path,
                    api_key,
                    prompt,
                    provider=provider,
                    model=model,
                )
                cell["raw_response"] = raw_response
                cell["result"] = _parse_transcription_result(raw_response, image_info["name"])
                cell["success"] = True
            except (json.JSONDecodeError, ValueError) as exc:
                cell["success"] = False
                cell["error"] = f"Error parsing JSON response: {exc}"
                cell["raw_response"] = raw_response
            except Exception as exc:
                cell["success"] = False
                cell["error"] = f"Error processing image: {exc}"
            cells.append(cell)
        rows.append({"image": image_info, "cells": cells})

    return jsonify(
        {
            "success": True,
            "results": rows,
            "models": selected_models,
            "combination_count": combination_count,
            "result_fields": list(TRANSCRIPTION_FIELDS) + ["image_name"],
        }
    )


@app.route("/get-images")
def get_images():
    """Get list of images from monitored folder, sorted by most recently added"""
    folder_path = session.get("batch_folder_path")
    if not folder_path or not os.path.exists(folder_path):
        return jsonify({"error": "Invalid folder path"})

    image_info = _list_local_folder_images(folder_path)
    return jsonify({"images": image_info, "count": len(image_info)})


@app.route("/transcribe-batch-image", methods=["POST"])

def transcribe_batch_image():
    """Transcribe a single image in batch mode.

    A space is sent every 10 seconds while awaiting the provider; the final
    JSON payload is sent as one chunk that the client parses.
    """
    image_path = request.form.get("image_path", "").strip()

    if not image_path or not os.path.exists(image_path):
        return jsonify({"error": "Invalid image path"})

    api_key, active_provider = get_active_api_key()
    if not api_key:
        return jsonify({"error": f'No API key found for {API_PROVIDERS.get(active_provider, {}).get("name", active_provider)}. <a href="/settings">Add one in Settings</a>.'})

    custom_prompt = session.get("batch_prompt")
    # Capture request-context state up front. The streaming generator below runs
    # while (or after) the request context is torn down, so session
    # are not bound inside it -- calling get_active_model()/session there crashed
    # outside the request context.
    active_model = get_active_model()
    default_prompt = get_default_prompt()
    current_run = session.get("current_run")

    def _stream():
        import time as _time

        result_holder = {}
        error_holder = {}
        done = threading.Event()

        def _call_api():
            try:
                raw = image_to_text(image_path, api_key, custom_prompt, provider=active_provider, model=active_model)
                result_holder["raw"] = raw
            except Exception as exc:
                error_holder["msg"] = str(exc)
            finally:
                done.set()

        t = threading.Thread(target=_call_api, daemon=True)
        t.start()

        while not done.wait(timeout=10):
            yield " "

        if "msg" in error_holder:
            yield json.dumps({"error": f"Error processing image: {error_holder['msg']}"})
            return

        raw_response = result_holder["raw"]
        try:
            result = _parse_transcription_result(raw_response, os.path.basename(image_path))
            yield json.dumps({"success": True, "result": result, "raw_response": raw_response})
        except (json.JSONDecodeError, ValueError) as e:
            yield json.dumps({"error": f"Error parsing JSON response: {e}", "raw_response": raw_response})

    return Response(_stream(), content_type="application/json")




@app.route("/add-to-run", methods=["POST"])

def add_to_run():
    """Add transcription result to current run"""
    if "current_run" not in session:
        return jsonify({"error": "No active run"})

    # Get the edited (final, human-verified) data
    result = {
        "title": request.form.get("title", "").strip(),
        "statement_of_responsibility": request.form.get(
            "statement_of_responsibility", ""
        ).strip(),
        "edition": request.form.get("edition", "").strip(),
        "city": request.form.get("city", "").strip(),
        "publisher_name": request.form.get("publisher_name", "").strip(),
        "address": request.form.get("address", "").strip(),
        "plate_number": request.form.get("plate_number", "").strip(),
        "date": request.form.get("date", "").strip(),
        "image_name": request.form.get("image_name", "").strip(),
        "dedupe_key": request.form.get("dedupe_key", "").strip(),
        "processed_at": datetime.now().isoformat(),
    }

    model_prediction = {
        k: v
        for k, v in _parse_json_object(request.form.get("model_prediction")).items()
        if k in TRANSCRIPTION_FIELDS and isinstance(v, str)
    }
    # Per-field source label, recorded for every predicted field so each row is
    # self-describing:
    #   "model"                  -> prediction kept as-is (default)
    #   "human_edited"           -> cataloguer corrected it from the image
    #   "human_edited_external"  -> corrected using outside/expert knowledge,
    #                               with an optional free-text note naming it
    #   "estimator_provided"     -> date filled by the deterministic estimator
    raw_provenance = _parse_json_object(request.form.get("field_provenance"))
    field_provenance = {}
    for key in TRANSCRIPTION_FIELDS:
        info = raw_provenance.get(key)
        info = info if isinstance(info, dict) else {}
        source = info.get("source")
        if source not in FIELD_PROVENANCE_SOURCES:
            source = "model"
        field_provenance[key] = {
            "model": str(info.get("model", model_prediction.get(key, ""))),
            "final": str(info.get("final", result.get(key, ""))),
            "source": source,
            "note": (
                str(info.get("note", ""))[:2000]
                if source == "human_edited_external"
                else ""
            ),
        }
    _, active_provider = get_active_api_key()
    result["model_prediction"] = model_prediction
    result["field_provenance"] = field_provenance
    result["provider"] = active_provider
    result["model"] = get_active_model()

    run_id = session["current_run"]["id"]
    try:
        run_count = _append_batch_result(run_id, result)
    except Exception as e:
        return jsonify({"error": f"Could not save scan: {e}"})


    return jsonify(
        {
            "success": True,
            "message": f"Added {result['image_name']} to run",
            "run_count": run_count,
        }
    )


@app.route("/export-run/<format>")

def export_run(format):
    """Export current or saved run results as CSV or XLSX"""
    requested_run_id = request.args.get("run_id", "").strip()
    if requested_run_id:
        if not _valid_batch_run_id(requested_run_id):
            return render_template(
                "batch_transcription.html",
                **_batch_template_context(error="Invalid run selected for export"),
            )
        run_id = requested_run_id
    elif "current_run" in session:
        run_id = session["current_run"]["id"]
    else:
        return render_template(
            "batch_transcription.html",
            **_batch_template_context(error="No run data to export"),
        )

    results = _load_batch_results(run_id)
    if not results:
        return render_template(
            "batch_transcription.html",
            **_batch_template_context(error="No run data to export"),
        )

    # CSV headers matching Colab notebook
    headers = [
        "Title",
        "Statement of responsibility",
        "Edition",
        "City",
        "Publisher name",
        "Address",
        "Plate number",
        "Date",
        "Image name",
    ]

    if format == "csv":
        # Create CSV content
        csv_content = ",".join(headers) + "\n"
        for result in results:
            values = [
                result["title"],
                result["statement_of_responsibility"],
                result["edition"],
                result["city"],
                result["publisher_name"],
                result["address"],
                result["plate_number"],
                result["date"],
                result["image_name"],
            ]
            # Escape double quotes in CSV values and wrap in quotes
            escaped_values = [
                '"' + str(value).replace('"', '""') + '"' for value in values
            ]
            csv_content += ",".join(escaped_values) + "\n"

        filename = f"batch_transcription_{run_id}.csv"
        return Response(
            csv_content,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    elif format == "xlsx":
        # Create XLSX using same logic as Colab notebook
        filename = f"batch_transcription_{run_id}.xlsx"
        temp_path = os.path.join("uploads", filename)
        os.makedirs("uploads", exist_ok=True)

        try:
            # Create DataFrame
            df_data = []
            for result in results:
                df_data.append(
                    [
                        result["title"],
                        result["statement_of_responsibility"],
                        result["edition"],
                        result["city"],
                        result["publisher_name"],
                        result["address"],
                        result["plate_number"],
                        result["date"],
                        result["image_name"],
                    ]
                )

            df = pd.DataFrame(df_data, columns=headers)

            # Use xlsxwriter to set column format (same as Colab)
            with pd.ExcelWriter(temp_path, engine="xlsxwriter") as writer:
                df.to_excel(writer, index=False, sheet_name="Sheet1")
                workbook = writer.book
                worksheet = writer.sheets["Sheet1"]

                # Find the column index for "Plate number"
                plate_col_idx = headers.index("Plate number")
                text_format = workbook.add_format({"num_format": "@"})

                # Set the format for the entire "Plate number" column
                worksheet.set_column(plate_col_idx, plate_col_idx, 20, text_format)

            return send_file(temp_path, as_attachment=True, download_name=filename)

        except Exception as e:
            return render_template(
                "batch_transcription.html",
                **_batch_template_context(error=f"Error creating XLSX: {e}"),
            )

    else:
        return render_template(
            "batch_transcription.html",
            **_batch_template_context(error="Invalid export format"),
        )


@app.route("/preview-image")

def preview_image():
    """Serve image file for preview"""
    image_path = request.args.get("path")

    if not image_path:
        return "Image path required", 400

    # Security check: ensure the path exists and is a file
    if not os.path.exists(image_path) or not os.path.isfile(image_path):
        return "Image not found", 404

    allowed_folders = [
        folder for folder in (
            session.get("batch_folder_path"),
            session.get("comparison_folder_path"),
        )
        if folder and os.path.isdir(folder)
    ]
    if not any(_path_is_inside(folder, image_path) for folder in allowed_folders):
        return "Unauthorized", 403

    try:
        # Determine content type based on file extension
        ext = os.path.splitext(image_path)[1].lower()
        content_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".webp": "image/webp",
        }

        content_type = content_types.get(ext, "image/jpeg")

        return send_file(image_path, mimetype=content_type)

    except Exception as e:
        return f"Error serving image: {e}", 500


@app.route("/estimate-date", methods=["POST"])

def estimate_date():
    """Estimate date for transcription data using existing dating program"""
    try:
        # Get transcription data from form
        publisher = request.form.get("publisher_name", "").strip()
        plate = request.form.get("plate_number", "").strip()
        address = request.form.get("address", "").strip()
        city = request.form.get("city", "").strip()

        # Combine address and city (same as main upload route)
        full_address = f"{address}, {city}".strip(", ")

        # Use the existing dating functionality
        result = estimate_year(publisher, plate, full_address)

        return jsonify(
            {
                "success": True,
                "input_data": {
                    "publisher": publisher,
                    "plate": plate,
                    "address": address,
                    "city": city,
                },
                "result": result,
                "error": result.get("error"),
            }
        )

    except Exception as e:
        return jsonify({"success": False, "error": f"Error estimating date: {str(e)}"})


@app.route("/codebase_documentation")

def codebase_documentation():
    """Render the codebase documentation page."""
    try:
        # Read the markdown file
        docs_path = os.path.join(
            os.path.dirname(__file__), "docs", "CODEBASE_DOCUMENTATION.md"
        )
        with open(docs_path, "r", encoding="utf-8") as f:
            md_content = f.read()

        # Convert markdown to HTML with extensions for better rendering
        html_content = markdown.markdown(
            md_content,
            extensions=[
                "tables",
                "fenced_code",
                "codehilite",
                "toc",
                "nl2br",
            ],
        )

        return render_template("codebase_documentation.html", content=html_content)
    except FileNotFoundError:
        return render_template(
            "codebase_documentation.html",
            content="<p class='error'>Documentation file not found.</p>",
        )
    except Exception as e:
        return render_template(
            "codebase_documentation.html",
            content=f"<p class='error'>Error loading documentation: {str(e)}</p>",
        )


@app.route("/oauth/onedrive/start")
def onedrive_start():
    if not ONEDRIVE_CLIENT_ID:
        flash("OneDrive is not configured.", "error")
        return redirect(url_for("batch_transcription"))
    redirect_uri = url_for("onedrive_callback", _external=True)
    session["onedrive_oauth_state"] = secrets.token_urlsafe(32)
    return redirect(onedrive_auth_url(redirect_uri))


@app.route("/oauth/onedrive/callback")
def onedrive_callback():
    expected_state = session.pop("onedrive_oauth_state", None)
    if not expected_state or not secrets.compare_digest(expected_state, request.args.get("state", "")):
        flash("Microsoft authorization expired or had an invalid state. Please reconnect.", "error")
        return redirect(url_for("batch_transcription"))
    code = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        flash(f"OneDrive login failed: {request.args.get('error_description', error)}", "error")
        return redirect(url_for("batch_transcription"))
    redirect_uri = url_for("onedrive_callback", _external=True)
    data = onedrive_exchange_code(code, redirect_uri)
    if "access_token" not in data:
        flash(f"OneDrive token error: {data.get('error_description', 'Unknown error')}", "error")
        return redirect(url_for("batch_transcription"))
    import time
    session["onedrive"] = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + data.get("expires_in", 3600),
    }
    session.modified = True
    flash("OneDrive connected.", "success")
    return redirect(url_for("batch_transcription"))


@app.route("/oauth/onedrive/disconnect")
def onedrive_disconnect():
    session.pop("onedrive", None)
    session.pop("onedrive_folder", None)
    if session.get("batch_source") == "onedrive":
        session.pop("batch_source", None)
        session.pop("batch_folder_path", None)
    session.modified = True
    flash("OneDrive disconnected.", "success")
    return redirect(url_for("batch_transcription"))


@app.route("/onedrive/browse")
def onedrive_browse():
    token = onedrive_get_token()
    if not token:
        return jsonify({"error": "Not connected to OneDrive"}), 401
    item_id = request.args.get("id", "")
    if item_id:
        path = f"/me/drive/items/{item_id}/children"
    else:
        path = "/me/drive/root/children"
    data = onedrive_api(path, token)
    if not data:
        return jsonify({"error": "Could not fetch folders"}), 500
    folders = []
    for item in data.get("value", []):
        if "folder" in item:
            folders.append({"id": item["id"], "name": item["name"]})
    return jsonify({"folders": folders, "parent_id": item_id})


@app.route("/onedrive/browse-all")
def onedrive_browse_all():
    token = onedrive_get_token()
    if not token:
        return jsonify({"error": "Not connected"}), 401
    item_id = request.args.get("id", "")
    path = f"/me/drive/items/{item_id}/children" if item_id else "/me/drive/root/children"
    data = onedrive_api(path, token)
    if not data:
        return jsonify({"error": "Could not fetch files"}), 500
    image_ext = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}
    folders = []
    images = []
    for item in data.get("value", []):
        if "folder" in item:
            folders.append({"id": item["id"], "name": item["name"]})
        elif "file" in item and os.path.splitext(item["name"])[1].lower() in image_ext:
            images.append({"id": item["id"], "name": item["name"]})
    return jsonify({"folders": folders, "images": images})


@app.route("/sharepoint/browse-all")
def sharepoint_browse_all():
    token = onedrive_get_token()
    if not token:
        return jsonify({"error": "Not connected"}), 401
    site_id = request.args.get("site_id", "")
    item_id = request.args.get("id", "")
    if not site_id:
        return jsonify({"error": "Site ID required"}), 400
    path = f"/sites/{site_id}/drive/items/{item_id}/children" if item_id else f"/sites/{site_id}/drive/root/children"
    data = onedrive_api(path, token)
    if not data:
        return jsonify({"error": "Could not fetch files"}), 500
    image_ext = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}
    folders = []
    images = []
    for item in data.get("value", []):
        if "folder" in item:
            folders.append({"id": item["id"], "name": item["name"]})
        elif "file" in item and os.path.splitext(item["name"])[1].lower() in image_ext:
            images.append({"id": item["id"], "name": item["name"]})
    return jsonify({"folders": folders, "images": images, "site_id": site_id})


@app.route("/onedrive/select-folder", methods=["POST"])
def onedrive_select_folder():
    folder_id = request.form.get("folder_id")
    folder_name = request.form.get("folder_name")
    if not folder_id:
        return jsonify({"error": "No folder selected"}), 400
    session["batch_source"] = "onedrive"
    session["onedrive_folder"] = {"id": folder_id, "name": folder_name}
    session["batch_folder_path"] = f"onedrive:{folder_name}"
    session.modified = True
    return jsonify({"success": True})


@app.route("/onedrive/images")
def onedrive_images():
    token = onedrive_get_token()
    if not token:
        return jsonify({"error": "Not connected to OneDrive"}), 401
    od_folder = session.get("onedrive_folder", {})
    folder_id = od_folder.get("id")
    if not folder_id:
        return jsonify({"error": "No folder selected"}), 400
    data = onedrive_api(f"/me/drive/items/{folder_id}/children", token)
    if not data:
        return jsonify({"error": "Could not fetch images"}), 500
    image_ext = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}
    images = []
    for item in data.get("value", []):
        if "file" in item:
            name = item["name"]
            ext = os.path.splitext(name)[1].lower()
            if ext in image_ext:
                images.append({
                    "name": name,
                    "path": f"onedrive:{item['id']}",
                    "modified": item.get("lastModifiedDateTime", ""),
                    "modified_str": item.get("lastModifiedDateTime", "")[:19].replace("T", " "),
                    "size": item.get("size", 0),
                })
    images.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify({"images": images, "count": len(images)})


@app.route("/onedrive/preview")
def onedrive_preview():
    token = onedrive_get_token()
    if not token:
        return "Not connected to OneDrive", 401
    item_id = request.args.get("id")
    if not item_id:
        return "Item ID required", 400
    resp = requests.get(
        f"{GRAPH_API}/me/drive/items/{item_id}/content",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        return "Could not fetch image", 500
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return Response(resp.content, mimetype=content_type)


@app.route("/onedrive/download-for-transcription", methods=["POST"])
def onedrive_download_for_transcription():
    token = onedrive_get_token()
    if not token:
        return jsonify({"error": "Not connected to OneDrive"}), 401
    item_id = request.form.get("item_id")
    filename = request.form.get("filename", "image.jpg")
    if not item_id:
        return jsonify({"error": "Item ID required"}), 400
    resp = requests.get(
        f"{GRAPH_API}/me/drive/items/{item_id}/content",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if resp.status_code != 200:
        return jsonify({"error": "Could not download image from OneDrive"}), 500
    temp_dir = os.path.join(app.config["UPLOAD_FOLDER"], "onedrive_cache")
    os.makedirs(temp_dir, exist_ok=True)
    local_path = os.path.join(temp_dir, secure_filename(filename))
    with open(local_path, "wb") as f:
        f.write(resp.content)
    api_key, active_provider = get_active_api_key()
    if not api_key:
        return jsonify({"error": f'No API key found for {API_PROVIDERS.get(active_provider, {}).get("name", active_provider)}. <a href="/settings">Add one in Settings</a>.'})
    try:
        custom_prompt = session.get("batch_prompt")
        raw_response = image_to_text(local_path, api_key, custom_prompt, provider=active_provider)
        data = json.loads(raw_response)
        result = {
            "title": data.get("title", "").strip(),
            "statement_of_responsibility": data.get("statement_of_responsibility", "").strip(),
            "edition": data.get("edition", "").strip(),
            "city": data.get("city", "").strip(),
            "publisher_name": data.get("publisher_name", "").strip(),
            "address": data.get("address", "").strip(),
            "plate_number": data.get("plate_number", "").strip(),
            "date": data.get("date", "").strip(),
            "image_name": filename,
        }
        return jsonify({"success": True, "result": result, "raw_response": raw_response})
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


@app.route("/sharepoint/sites")
def sharepoint_sites():
    token = onedrive_get_token()
    if not token:
        return jsonify({"error": "Not connected"}), 401
    data = onedrive_api("/sites?search=*", token)
    if not data:
        return jsonify({"error": "Could not fetch sites"}), 500
    sites = [{"id": s["id"], "name": s.get("displayName", s.get("name", "Unknown"))} for s in data.get("value", [])]
    return jsonify({"sites": sites})


@app.route("/sharepoint/browse")
def sharepoint_browse():
    token = onedrive_get_token()
    if not token:
        return jsonify({"error": "Not connected"}), 401
    site_id = request.args.get("site_id", "")
    item_id = request.args.get("id", "")
    if not site_id:
        return jsonify({"error": "Site ID required"}), 400
    if item_id:
        path = f"/sites/{site_id}/drive/items/{item_id}/children"
    else:
        path = f"/sites/{site_id}/drive/root/children"
    data = onedrive_api(path, token)
    if not data:
        return jsonify({"error": "Could not fetch folders"}), 500
    folders = [{"id": item["id"], "name": item["name"]} for item in data.get("value", []) if "folder" in item]
    return jsonify({"folders": folders, "site_id": site_id, "parent_id": item_id})


@app.route("/sharepoint/select-folder", methods=["POST"])
def sharepoint_select_folder():
    site_id = request.form.get("site_id")
    folder_id = request.form.get("folder_id")
    folder_name = request.form.get("folder_name")
    if not site_id or not folder_id:
        return jsonify({"error": "Missing site or folder"}), 400
    session["batch_source"] = "sharepoint"
    session["sharepoint_folder"] = {"site_id": site_id, "folder_id": folder_id, "name": folder_name}
    session["batch_folder_path"] = f"sharepoint:{folder_name}"
    session.modified = True
    return jsonify({"success": True})


@app.route("/sharepoint/images")
def sharepoint_images():
    token = onedrive_get_token()
    if not token:
        return jsonify({"error": "Not connected"}), 401
    sp = session.get("sharepoint_folder", {})
    site_id = sp.get("site_id")
    folder_id = sp.get("folder_id")
    if not site_id or not folder_id:
        return jsonify({"error": "No folder selected"}), 400
    data = onedrive_api(f"/sites/{site_id}/drive/items/{folder_id}/children", token)
    if not data:
        return jsonify({"error": "Could not fetch images"}), 500
    image_ext = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}
    images = []
    for item in data.get("value", []):
        if "file" in item:
            ext = os.path.splitext(item["name"])[1].lower()
            if ext in image_ext:
                images.append({
                    "name": item["name"],
                    "path": f"sharepoint:{site_id}:{item['id']}",
                    "modified": item.get("lastModifiedDateTime", ""),
                    "modified_str": item.get("lastModifiedDateTime", "")[:19].replace("T", " "),
                    "size": item.get("size", 0),
                })
    images.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify({"images": images, "count": len(images)})


@app.route("/sharepoint/preview")
def sharepoint_preview():
    token = onedrive_get_token()
    if not token:
        return "Not connected", 401
    site_id = request.args.get("site_id")
    item_id = request.args.get("id")
    if not site_id or not item_id:
        return "Site ID and item ID required", 400
    resp = requests.get(
        f"{GRAPH_API}/sites/{site_id}/drive/items/{item_id}/content",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        return "Could not fetch image", 500
    return Response(resp.content, mimetype=resp.headers.get("Content-Type", "image/jpeg"))


@app.route("/sharepoint/download-for-transcription", methods=["POST"])
def sharepoint_download_for_transcription():
    token = onedrive_get_token()
    if not token:
        return jsonify({"error": "Not connected"}), 401
    site_id = request.form.get("site_id")
    item_id = request.form.get("item_id")
    filename = request.form.get("filename", "image.jpg")
    if not site_id or not item_id:
        return jsonify({"error": "Missing site or item ID"}), 400
    resp = requests.get(
        f"{GRAPH_API}/sites/{site_id}/drive/items/{item_id}/content",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if resp.status_code != 200:
        return jsonify({"error": "Could not download image from SharePoint"}), 500
    temp_dir = os.path.join(app.config["UPLOAD_FOLDER"], "sharepoint_cache")
    os.makedirs(temp_dir, exist_ok=True)
    local_path = os.path.join(temp_dir, secure_filename(filename))
    with open(local_path, "wb") as f:
        f.write(resp.content)
    api_key, active_provider = get_active_api_key()
    if not api_key:
        return jsonify({"error": f'No API key found for {API_PROVIDERS.get(active_provider, {}).get("name", active_provider)}. <a href="/settings">Add one in Settings</a>.'})
    try:
        custom_prompt = session.get("batch_prompt")
        raw_response = image_to_text(local_path, api_key, custom_prompt, provider=active_provider)
        data = json.loads(raw_response)
        result = {
            "title": data.get("title", "").strip(),
            "statement_of_responsibility": data.get("statement_of_responsibility", "").strip(),
            "edition": data.get("edition", "").strip(),
            "city": data.get("city", "").strip(),
            "publisher_name": data.get("publisher_name", "").strip(),
            "address": data.get("address", "").strip(),
            "plate_number": data.get("plate_number", "").strip(),
            "date": data.get("date", "").strip(),
            "image_name": filename,
        }
        return jsonify({"success": True, "result": result, "raw_response": raw_response})
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)




@app.route("/user-guide")
def user_guide():
    return render_template("user_guide.html")




@app.route("/cleanup", methods=["POST"])

def manual_cleanup():
    """Manually trigger cleanup of old files."""
    try:
        cleanup_old_files(app.config["UPLOAD_FOLDER"], max_age_hours=24)
        return jsonify({"success": True, "message": "Cleanup completed"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500




@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        action = request.form.get("action")
        provider = request.form.get("provider")
        if provider not in API_PROVIDERS:
            flash("Select a valid provider.", "error")
            return redirect(url_for("settings"))

        key_provider = _provider_key_id(provider)
        if action == "save_key":
            api_key = request.form.get("api_key", "").strip()
            if api_key:
                keys = dict(session.get("api_keys", {}))
                keys[key_provider] = encrypt_api_key(api_key)
                session["api_keys"] = keys
                flash(f"{API_PROVIDERS[provider]['name']} API key saved locally.", "success")
            else:
                flash("API key cannot be empty.", "error")
        elif action == "remove_key":
            keys = dict(session.get("api_keys", {}))
            keys.pop(key_provider, None)
            session["api_keys"] = keys
            flash("Saved key removed. A local environment key, if configured, remains available.", "success")
        elif action == "set_active":
            session["active_provider"] = provider
            flash(f"Active provider set to {API_PROVIDERS[provider]['name']}.", "success")
        elif action == "set_model":
            model = request.form.get("model", "").strip()
            if model in API_PROVIDERS[provider]["models"]:
                models = dict(session.get("selected_models", {}))
                models[provider] = model
                session["selected_models"] = models
                flash(f"Model set to {API_PROVIDERS[provider]['models'][model]}.", "success")
            else:
                flash("Select a valid model.", "error")
        else:
            flash("Unknown settings action.", "error")
        return redirect(url_for("settings"))

    masked_keys = {}
    env_keys = {}
    key_status = {}
    encrypted_keys = session.get("api_keys", {})
    for provider, info in API_PROVIDERS.items():
        encrypted = encrypted_keys.get(_provider_key_id(provider))
        if encrypted and decrypt_api_key(encrypted):
            masked_keys[provider] = "****"
            key_status[provider] = "session"
        elif os.getenv(info["env_var"]):
            env_keys[provider] = True
            key_status[provider] = "env"

    _, active_provider = get_active_api_key()
    selected_models = {
        provider: session.get("selected_models", {}).get(provider, info["default_model"])
        for provider, info in API_PROVIDERS.items()
    }
    return render_template(
        "settings.html",
        providers=API_PROVIDERS,
        masked_keys=masked_keys,
        env_keys=env_keys,
        key_status=key_status,
        active_provider=active_provider,
        selected_models=selected_models,
    )



def find_free_port(start=5001, end=5099):
    import socket
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


if __name__ == "__main__":
    cleanup_old_files(app.config["UPLOAD_FOLDER"], max_age_hours=24)

    port = int(os.environ.get("PORT", 0))
    if not port:
        port = find_free_port()
        if not port:
            raise SystemExit(1)
        os.environ["PORT"] = str(port)

    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Open {url} in your browser.\n")

    app.run(host="127.0.0.1", port=port, debug=debug)
