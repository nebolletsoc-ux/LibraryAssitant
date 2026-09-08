import csv
import hmac
import io
import json
import os
import secrets
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import requests
from requests import RequestException
from concurrent.futures import ThreadPoolExecutor

# Optional exception monitoring (roadmap Step 10). Enabling is purely env-led:
# without SENTRY_DSN nothing is imported or sent, so local runs and the test
# suite stay untouched.
SENTRY_DSN = os.environ.get("SENTRY_DSN")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.0,
        send_default_pii=False,
    )

from flask import Flask, Response, render_template, request, jsonify, redirect, url_for, session, g
from sqlalchemy.orm import joinedload
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

from models import db, Book, UserBook, Availability, LibraryConfig, User
from library.isbn import find_isbn
from library.oakland import search_libraries
import mailer


app = Flask(__name__)

# Render (and most hosts) terminate TLS in front of the app, so the client IP
# arrives as X-Forwarded-For and the scheme as X-Forwarded-Proto. Let Werkzeug
# trust one hop so request.remote_addr / request.is_secure are real values
# (needed for per-IP rate limiting and the Secure session cookie below).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Session signing key is resolved inside the app context below (after the DB
# is ready), from SECRET_KEY env var or a random per-deployment secret stored
# in the database.

def _normalize_database_url(raw):
    """Normalize a DATABASE_URL for SQLAlchemy.

    Accepts bare "postgres://" (common in go/env hosting) and converts it to
    the "postgresql+psycopg2://" scheme. Returns None when unset so callers
    keep their SQLite fallback.
    """
    url = (raw or "").strip()
    if not url:
        return None
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


# Database configuration
#
# Set DATABASE_URL to a persistent database (e.g. a hosted Postgres from
# Neon/Supabase) so the list survives restarts and redeploys. Without it we
# fall back to the local SQLite file in the instance folder, which is
# ephemeral on Render's free tier (wiped on every deploy/restart).
database_url = _normalize_database_url(os.environ.get("DATABASE_URL"))
USE_POSTGRES = bool(database_url)
if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    # Postgres: recycle long-lived pooled connections and re-check them.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_recycle": 300}
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///library_assistant.db"
    # Allow concurrent scan threads to wait out SQLite write locks.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"timeout": 30},
    }
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Initialize database
db.init_app(app)

# Shipped library presets. No library is auto-added or enabled: the app
# starts with an empty configuration and the Add-library screen offers
# these as opt-in choices (plus a custom-library form). This matches the
# "start with no libraries selected" default state.
LIBRARY_PRESETS = {
    "lapl": {"key": "lapl", "label": "Los Angeles Public Library", "overdrive": "lapl"},
    "oakland": {"key": "oakland", "label": "Oakland Public Library", "bibliocommons": "oaklandlibrary"},
    "berkeley": {"key": "berkeley", "label": "Berkeley Public Library", "overdrive": "berkeleypubliclibrary"},
    "redwood_city": {"key": "redwood_city", "label": "Redwood City Public Library", "bibliocommons": "rcpl"},
    "hoopla": {"key": "hoopla", "label": "Hoopla", "hoopla": True},
    "sfpl": {"key": "sfpl", "label": "San Francisco Public Library", "bibliocommons": "sfpl"},
    "ssfpl": {"key": "ssfpl", "label": "South San Francisco Public Library", "bibliocommons": "ssfpl"},
    "alameda_county": {"key": "alameda_county", "label": "Alameda County Library", "bibliocommons": "aclibrary"},
    "contra_costa_county": {"key": "contra_costa_county", "label": "Contra Costa County Library", "bibliocommons": "ccclib"},
}

# Create all tables on app startup
with app.app_context():
    db.create_all()

    # No libraries are seeded by default — the app starts with an empty
    # configuration (see LIBRARY_PRESETS above). Nothing to initialize.

    # Lightweight additive migrations for columns added after a table first
    # shipped (the users table exists on the deployed Postgres without the
    # notification columns). create_all() only creates missing tables, so
    # existing databases need these ALTERs.
    from sqlalchemy import inspect as _inspect, text as _text

    _users_cols = {c["name"] for c in _inspect(db.engine).get_columns("users")}
    _user_alters = []
    if "email" not in _users_cols:
        _user_alters.append("ALTER TABLE users ADD COLUMN email VARCHAR(255)")
    if "notify_on_available" not in _users_cols:
        _user_alters.append(
            "ALTER TABLE users ADD COLUMN notify_on_available BOOLEAN NOT NULL DEFAULT TRUE"
        )
    if "weekly_digest" not in _users_cols:
        _user_alters.append(
            "ALTER TABLE users ADD COLUMN weekly_digest BOOLEAN NOT NULL DEFAULT FALSE"
        )
    for _stmt in _user_alters:
        db.session.execute(_text(_stmt))
    if _user_alters:
        db.session.commit()

    # Session signing key: prefer SECRET_KEY from the environment; otherwise
    # keep a random per-deployment secret in the database so login sessions
    # survive restarts/redeploys on Render's ephemeral filesystem and can't
    # be forged from a known fallback value.
    db.session.execute(_text(
        "CREATE TABLE IF NOT EXISTS app_settings "
        "(key VARCHAR(100) PRIMARY KEY, value TEXT NOT NULL)"
    ))
    env_secret = os.environ.get("SECRET_KEY")
    if env_secret:
        app.config["SECRET_KEY"] = env_secret
    else:
        row = db.session.execute(_text(
            "SELECT value FROM app_settings WHERE key = 'secret_key'"
        )).first()
        if row:
            app.config["SECRET_KEY"] = row[0]
        else:
            secret = secrets.token_urlsafe(48)
            db.session.execute(_text(
                "INSERT INTO app_settings (key, value) VALUES ('secret_key', :v)"
            ), {"v": secret})
            db.session.commit()
            app.config["SECRET_KEY"] = secret


# Optional shared-password gate. Off by default (no env var set = no prompt,
# same as running locally today). Set APP_PASSWORD once this has a public
# URL if you want to keep it to just the people you've shared it with —
# anyone without the password gets a browser login prompt, any username works.
APP_PASSWORD = os.environ.get("APP_PASSWORD")

# STEP 2 of the public-release roadmap: the web tier must actually send mail.
# The code already sends via Resend when RESEND_API_KEY is set; warn loudly
# if that key is configured but no verified sender address was provided, so a
# deployment can't silently send from the restricted onboarding@resend.dev.
if mailer._resend_key() and not mailer._from_addr():
    print(
        "WARNING: RESEND_API_KEY is set but EMAIL_FROM is not. Emails will be "
        "sent from onboarding@resend.dev (Resend 'Restricted' mode). Set "
        "EMAIL_FROM once your sender domain is verified (see cron.example)."
    )


@app.after_request
def no_cache(response):
    """Never let browsers/caches serve a stale page or stale bundles.

    The UI (templates/tbr.html) is the whole frontend and changes every
    revision, and the API data is constantly refreshed, so any HTTP cache -
    especially mobile Safari's heuristic caching - can leave users running
    an old bundle (this caused "Recheck does nothing" reports).

    Also sets the CSRF double-submit cookie if the client doesn't have one
    yet. It lives in a plain (non-HttpOnly) cookie so the JS can read it back
    and echo it as the X-CSRF-Token header on state-changing API requests.

    Security headers live here too: CSP as tight as the inline-script UI
    allows, plus busy-header / referrer hardening for the public release.
    """
    if "csrf_token" not in request.cookies:
        response.set_cookie(
            "csrf_token",
            secrets.token_urlsafe(32),
            httponly=False,
            samesite="Lax",
            secure=request.is_secure,
            max_age=60 * 60 * 24 * 30,
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "font-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
    )
    # Read the Secure flag here, after app.after_request handlers run but
    # before Flask persists the session cookie: HTTPS (i.e. any real
    # deployment) gets Secure=True, local HTTP and the test client stay plain.
    # Set SESSION_COOKIE_SECURE=0 to force it off behind a plain-HTTP proxy.
    app.config["SESSION_COOKIE_SECURE"] = bool(
        request.is_secure and os.environ.get("SESSION_COOKIE_SECURE", "1") != "0"
    )
    return response


PUBLIC_PATHS = {"/healthz", "/", "/tbr", "/favicon.ico", "/terms", "/privacy"}


def _is_public_path(path):
    if path in PUBLIC_PATHS:
        return True
    # Auth endpoints must stay reachable before a user has a session.
    if path.startswith("/api/auth/"):
        return True
    # Cron/webhook endpoints authenticate with their own secret token.
    return path.startswith("/api/system/")


def _not_software_wanting_password(path):
    """Paths that stay open even when the optional APP_PASSWORD gate is on."""
    if path == "/healthz":
        return True
    if path.startswith("/api/system/"):
        return True
    return path in ("/terms", "/privacy")


def _current_user_id():
    """The logged-in user's id inside a request (auth-gated routes only)."""
    user = getattr(g, "user", None)
    return user.id if user else None


@app.before_request
def _load_current_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        user = db.session.get(User, user_id)
        if user:
            g.user = user
        else:
            session.pop("user_id", None)
    return None


@app.before_request
def _require_api_auth():
    """Only the API carries data; every /api route except public ones needs a session."""
    if not request.path.startswith("/api/"):
        return None
    if _is_public_path(request.path):
        return None
    if getattr(g, "user", None) is None:
        return jsonify({"error": "Authentication required"}), 401
    return None


@app.before_request
def _csrf_protect():
    """Double-submit cookie CSRF defense for mutating /api routes.

    The server drops a csrf_token cookie; the frontend echoes it back in the
    X-CSRF-Token header. An attacking site can't read that cookie (same-origin
    policy) so a cross-site forged request won't carry a matching header.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if not request.path.startswith("/api/"):
        return None
    if _is_public_path(request.path):
        return None
    header_token = request.headers.get("X-CSRF-Token") or ""
    cookie_token = request.cookies.get("csrf_token") or ""
    if not header_token or not cookie_token or not hmac.compare_digest(header_token, cookie_token):
        return jsonify({"error": "Invalid or missing CSRF token"}), 403
    return None


def _adopt_legacy_user_rows(user_id):
    """Claim the pre-accounts data for the very first account.

    Before accounts existed everything was stored under the implied
    user_id=1 (books, per-book row, library config). When the FIRST account
    is registered on a legacy database this moves those rows to them, but
    ONLY when the operator opted in with ADOPT_LEGACY_ROWS=1 — that flag is
    deliberately off by default so a stranded pre-accounts list can't be
    claimed by an unrelated first signup on a public deployment.
    """
    if user_id == 1:
        return
    moved = False
    for model in (UserBook, LibraryConfig):
        if model.query.filter_by(user_id=1).first():
            model.query.filter_by(user_id=1).update(
                {"user_id": user_id}, synchronize_session=False
            )
            moved = True
    if moved:
        db.session.commit()


# Per-IP throttle for the auth endpoints so public signups don't invite bots.
# In-memory per-process is fine on single-worker deploys; note it in runbook.
_AUTH_RATE_LIMIT_PER_WINDOW = 8
_AUTH_RATE_WINDOW_SECONDS = 60
_rate_hits = defaultdict(list)  # "{ip}|{path}" -> [timestamps]


def _auth_rate_limited(bucket):
    now = time.time()
    hits = [t for t in _rate_hits[bucket] if now - t < _AUTH_RATE_WINDOW_SECONDS]
    _rate_hits[bucket] = hits
    if len(hits) >= _AUTH_RATE_LIMIT_PER_WINDOW:
        return True
    _rate_hits[bucket].append(now)
    return False


def _rate_limited_response():
    resp = jsonify({"error": "Too many attempts. Please wait a minute and try again."})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(_AUTH_RATE_WINDOW_SECONDS)
    return resp


# Account lockout persisted in app_settings so it survives multi-worker and
# restarts (unlike the per-IP throttle above).
_MAX_FAILED_LOGINS = 10
_LOCKOUT_SECONDS = 15 * 60


def _login_fail_record(username):
    row = db.session.execute(db.text(
        "SELECT value FROM app_settings WHERE key = :k"
    ), {"k": f"login_fail:{username}"}).first()
    if not row:
        return {"count": 0, "until": 0}
    try:
        return json.loads(row[0])
    except ValueError:
        return {"count": 0, "until": 0}


def _save_login_fail(username, record):
    db.session.execute(db.text(
        "INSERT INTO app_settings (key, value) VALUES (:k, :v) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    ), {"k": f"login_fail:{username}", "v": json.dumps(record)})
    db.session.commit()


def _clear_login_fail(username):
    db.session.execute(db.text(
        "DELETE FROM app_settings WHERE key = :k"
    ), {"k": f"login_fail:{username}"})
    db.session.commit()


def _record_login_failure(username):
    record = _login_fail_record(username)
    now = time.time()
    if record.get("until") and now < record["until"]:
        return  # already locked; keep the lock
    record["count"] = record.get("count", 0) + 1
    if record["count"] >= _MAX_FAILED_LOGINS:
        record["until"] = now + _LOCKOUT_SECONDS
    _save_login_fail(username, record)


def _account_locked(username):
    record = _login_fail_record(username)
    until = record.get("until") or 0
    if until and time.time() < until:
        return True
    if until:
        _clear_login_fail(username)
    return False


@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json() or {}

    # Honeypot: real browsers never send "website" (it's hidden off-screen).
    # Bots that auto-fill every field get a fake success and no account.
    if (data.get("website") or "").strip():
        return jsonify({"user": {"id": 0, "username": (data.get("username") or "")[:20]}}), 201

    if _auth_rate_limited(f"{request.remote_addr}|register"):
        return _rate_limited_response()

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if len(username) > 80:
        return jsonify({"error": "Username must be 80 characters or fewer"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "That username is taken"}), 409

    first_user = User.query.count() == 0
    user = User(username=username, password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.flush()
    # Claiming a pre-accounts reading list is opt-in (ADOPT_LEGACY_ROWS=1) so a
    # public deployment can't hand a stranded user_id=1 list to a stranger.
    if first_user and os.environ.get("ADOPT_LEGACY_ROWS", "0") == "1":
        _adopt_legacy_user_rows(user.id)
    db.session.commit()

    session["user_id"] = user.id
    return jsonify({"user": user.to_dict()}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    if _auth_rate_limited(f"{request.remote_addr}|login"):
        return _rate_limited_response()

    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if _account_locked(username):
        resp = jsonify({"error": "Account temporarily locked due to too many failed attempts."})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(_LOCKOUT_SECONDS)
        return resp

    user = User.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password_hash, password):
        if user:
            _record_login_failure(username)
        return jsonify({"error": "Invalid username or password"}), 401

    _clear_login_fail(username)
    session["user_id"] = user.id
    return jsonify({"user": user.to_dict()}), 200


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True}), 200


@app.route("/api/auth/account", methods=["DELETE"])
def delete_account():
    """Permanently delete the current account and its data.

    Removes the user, their reading-list rows, library config, and any Book
    records that no other account references (their availability cache goes
    with them). Requires {"confirm": "delete_account"} so it can't be fired
    by accident; CSRF-protected like every mutating /api route.
    """
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "delete_account":
        return jsonify({"error": "Confirmation required"}), 400

    user = getattr(g, "user", None)
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    try:
        removed_book_ids = {
            ub.book_id
            for ub in UserBook.query.filter_by(user_id=user.id).all()
        }
        UserBook.query.filter_by(user_id=user.id).delete()
        LibraryConfig.query.filter_by(user_id=user.id).delete()

        # Drop Book rows that now have no owners left (their Availability rows
        # cascade with them); books shared with other accounts are kept.
        orphans = (
            Book.query
            .outerjoin(UserBook, UserBook.book_id == Book.id)
            .group_by(Book.id)
            .having(db.func.count(UserBook.id) == 0)
            .all()
        )
        orphan_ids = {b.id for b in orphans}
        for book in orphans:
            db.session.delete(book)

        db.session.delete(user)
        db.session.commit()
        session.clear()
        return jsonify({
            "ok": True,
            "deleted_books": len(orphan_ids),
            "removed_from_list": len(removed_book_ids),
        }), 200
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting account: {e}")
        return jsonify({"error": "Failed to delete account"}), 500


@app.route("/api/auth/me", methods=["GET"])
def me():
    user = getattr(g, "user", None)
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"user": user.to_dict()}), 200


@app.route("/api/user/preferences", methods=["GET"])
def get_user_preferences():
    """Per-account notification settings."""
    user = getattr(g, "user", None)
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    return jsonify(_user_preferences(user))


@app.route("/api/user/preferences", methods=["PATCH"])
def update_user_preferences():
    """Update notification settings: {"email", "notify_on_available", "weekly_digest"}."""
    user = getattr(g, "user", None)
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json() or {}
    if "email" in data:
        email = (data.get("email") or "").strip()
        user.email = email or None
    if "notify_on_available" in data and isinstance(data["notify_on_available"], bool):
        user.notify_on_available = data["notify_on_available"]
    if "weekly_digest" in data and isinstance(data["weekly_digest"], bool):
        user.weekly_digest = data["weekly_digest"]
    db.session.commit()
    return jsonify(_user_preferences(user)), 200


@app.route("/api/user/preferences/send-test", methods=["POST"])
def send_test_email():
    """Email a test message to the current user's configured address.

    Sends synchronously so the UI can report delivery or the real error.
    """
    user = getattr(g, "user", None)
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    if not user.email:
        return jsonify({"error": "Add an email address first"}), 400
    if not mailer.is_enabled():
        return jsonify({"error": "Email isn't configured on this server"}), 400
    ok, detail = mailer.send_email_report(
        user.email,
        "MyNextRead test email",
        text=(
            "If you're reading this, email notifications are working.\n\n"
            "You'll get an email whenever a book on your list becomes "
            "available, plus your weekly digest if it's turned on."
        ),
    )
    if ok:
        return jsonify({"sent": True, "detail": detail}), 200
    return jsonify({"error": f"Test email failed: {detail}"}), 502


def _user_preferences(user):
    return {
        "email": user.email,
        "notify_on_available": user.notify_on_available,
        "weekly_digest": user.weekly_digest,
        "email_enabled": mailer.is_enabled(),
    }


def _run_weekly_digest():
    """Email each weekly-digest subscriber their currently available books.

    Returns the number of emails sent. Called by the send_digest.py CLI (and
    swappable into any scheduler). Non-subscribers/empties are skipped.
    """
    subscribers = (
        User.query.filter_by(weekly_digest=True)
        .filter(User.email.isnot(None))
        .all()
    )
    sent = 0
    for user in subscribers:
        enabled_keys = {
            lib.library_key
            for lib in LibraryConfig.query.filter_by(user_id=user.id, enabled=True).all()
        }
        user_books = (
            UserBook.query
            .options(joinedload(UserBook.book).joinedload(Book.availability))
            .filter_by(user_id=user.id, status="tbr")
            .all()
        )
        available = []
        for user_book in user_books:
            rows = user_book.book.availability or []
            if enabled_keys:
                rows = [a for a in rows if a.library in enabled_keys]
            avail_rows = [a for a in rows if a.available]
            if avail_rows:
                available.append((user_book.book, avail_rows))
        if not available:
            continue

        lines = []
        for book, avail_rows in available:
            opts = ", ".join(
                sorted({f"{a.library} · {a.format or 'book'}" for a in avail_rows})
            )
            lines.append(f"\u2022 {book.title}"
                         f"{' by ' + book.author if book.author else ''} — {opts}")
        subject = f"MyNextRead: {len(available)} book"
        subject += "" if len(available) == 1 else "s"
        subject += " available now"
        mailer.submit_email(
            user.email,
            subject,
            text="Books from your reading list that are available now:\n\n"
            + "\n".join(lines)
            + "\n\nBorrow them in MyNextRead.",
        )
        sent += 1
    return sent


@app.route("/api/system/digest", methods=["POST"])
def system_digest():
    """Cloud-scheduler friendly digest trigger (public-release step 3).

    Any scheduler that can send an HTTP POST with the shared CRON_TOKEN can
    fire the weekly digest without needing database or Resend credentials:
    cron-job.org, a Render Cron Job that curls this path, GitHub Actions, etc.
    Without CRON_TOKEN configured the endpoint is dead (404).
    """
    token = os.environ.get("CRON_TOKEN")
    if not token:
        return jsonify({"error": "Digest webhook not configured"}), 404

    supplied = (request.headers.get("X-Cron-Token") or "").strip()
    authorization = (request.headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip() or supplied
    if not supplied or not hmac.compare_digest(supplied, token):
        return jsonify({"error": "Unauthorized"}), 401

    sent = _run_weekly_digest()
    return jsonify({"digest_sent": sent}), 200


@app.before_request
def require_password():
    if not APP_PASSWORD:
        return None

    if _not_software_wanting_password(request.path):
        return None

    auth = request.authorization
    if not auth or auth.password != APP_PASSWORD:
        return (
            "Password required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Library Assistant"'},
        )

    return None


@app.route("/healthz")
def healthz():
    # Lightweight endpoint for hosting platforms to confirm the app is alive.
    # "database" reports the active backend so we can verify persistence is
    # configured (postgres) or that we're on the ephemeral local file
    # (sqlite), which gets wiped on redeploy.
    return jsonify(
        {
            "status": "ok",
            "database": "postgres" if USE_POSTGRES else "sqlite",
        }
    )


MAX_WORKERS = 8
JOB_TIMEOUT_SECONDS = 3600  # Clean up jobs after 1 hour

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Single active background scan at a time; protects job progress counters.
_scan_lock = threading.Lock()
_active_scan = None

jobs = {}

def validate_csv_structure(fieldnames):
    """Validate CSV has required columns. Returns (is_valid, error_message)."""
    if not fieldnames:
        return False, "CSV file is empty or cannot be read."

    fieldset = set(fieldnames)

    storygraph_required = {"Title", "Authors", "Read Status"}
    goodreads_required = {"Title", "Author", "Exclusive Shelf"}

    if storygraph_required <= fieldset or goodreads_required <= fieldset:
        return True, None

    return False, (
        "Unrecognized CSV format. Expected a StoryGraph export "
        f"(columns: {', '.join(sorted(storygraph_required))}) or a "
        f"Goodreads export (columns: {', '.join(sorted(goodreads_required))})."
    )


def categorize_error(error):
    """Categorize error to provide better user feedback."""
    error_str = str(error).lower()
    
    # Network/transient errors
    if any(term in error_str for term in ["timeout", "connection", "network", "refused"]):
        return "network", "Service unavailable. Please try again in a moment."
    
    # Not found errors
    if any(term in error_str for term in ["not found", "404"]):
        return "not_found", "Book not found in ISBN database or library."
    
    # Rate limiting
    if any(term in error_str for term in ["rate", "429", "too many"]):
        return "rate_limited", "Service rate limit exceeded. Please try again later."
    
    # Default
    return "unknown", f"Error: {error}"


def fetch_synopsis(isbn, title, author):
    """
    Fetch a synopsis and genre for a book from Open Library.

    Important: Open Library's edition-level record (what /isbn/{isbn}.json
    and the legacy /api/books?jscmd=data endpoint return) almost never
    carries a "description" — that field lives on the separate parent
    "work" record. So we look up the edition first (mainly to find its
    work key, and to grab any edition-level subjects as a genre fallback),
    then fetch the work record for the actual description/subjects.
    """
    if not isbn:
        return None, None

    headers = {
        # Open Library asks for a descriptive User-Agent; a generic/missing
        # one risks being deprioritized or blocked under load.
        "User-Agent": "LibraryAssistant/1.0 (personal reading-list tool)"
    }

    synopsis = None
    genre = None
    work_key = None

    # Step 1: edition lookup — gives us the work key, and sometimes subjects.
    try:
        edition_url = f"https://openlibrary.org/isbn/{isbn}.json"
        response = requests.get(edition_url, headers=headers, timeout=4)

        if response.status_code == 200:
            edition_data = response.json()

            works = edition_data.get("works") or []
            if works and isinstance(works[0], dict):
                work_key = works[0].get("key")

            subjects = edition_data.get("subjects")
            if subjects:
                genre = subjects[0] if isinstance(subjects[0], str) else str(subjects[0])

    except Exception as e:
        print(f"Edition lookup failed for '{title}' ({isbn}): {e}")

    # Step 1.5: if the ISBN lookup didn't resolve a work (e.g. synthetic
    # "synthetic-*" / "cover-*" ISBNs from the search/add flow), search
    # Open Library by title+author to find the work record.
    if not work_key:
        try:
            query = f"title:{title}"
            if author:
                query += f" author:{author}"
            response = requests.get(
                "https://openlibrary.org/search.json",
                params={"q": query, "limit": 3},
                headers=headers,
                timeout=4,
            )
            response.raise_for_status()
            docs = (response.json() or {}).get("docs") or []
            for doc in docs:
                for key in doc.get("seed") or []:
                    if isinstance(key, str) and key.startswith("/works/"):
                        work_key = key
                        break
                if work_key:
                    break
        except Exception as e:
            print(f"Title-search work lookup failed for '{title}': {e}")

    # Step 2: work lookup — this is where the description usually lives.
    if work_key:
        try:
            work_url = f"https://openlibrary.org{work_key}.json"
            response = requests.get(work_url, headers=headers, timeout=4)
            response.raise_for_status()

            work_data = response.json()

            desc = work_data.get("description")
            if isinstance(desc, dict) and "value" in desc:
                synopsis = desc["value"]
            elif isinstance(desc, str):
                synopsis = desc

            if not genre:
                subjects = work_data.get("subjects")
                if subjects:
                    genre = subjects[0] if isinstance(subjects[0], str) else str(subjects[0])

        except Exception as e:
            print(f"Work lookup failed for '{title}' ({work_key}): {e}")

    # Fallback: the legacy bibkeys endpoint occasionally carries a
    # description directly on the edition even when the above doesn't.
    if not synopsis:
        try:
            url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&jscmd=data&format=json"
            response = requests.get(url, headers=headers, timeout=4)
            response.raise_for_status()

            data = response.json()
            for key, book_data in data.items():
                if "description" in book_data:
                    desc = book_data["description"]
                    if isinstance(desc, dict) and "value" in desc:
                        synopsis = desc["value"]
                    else:
                        synopsis = str(desc) if desc else None

                if not genre:
                    subjects = book_data.get("subjects")
                    if subjects:
                        genre = subjects[0].get("name") if isinstance(subjects[0], dict) else str(subjects[0])

        except Exception as e:
            print(f"Bibkeys fallback failed for '{title}': {e}")

    return synopsis, genre

def _clean_goodreads_isbn(value):
    """Goodreads wraps ISBN cells like ="9780262384254" to stop spreadsheet
    apps from mangling leading zeros/formatting. Strip that wrapper."""
    value = (value or "").strip()
    if value.startswith('="') and value.endswith('"'):
        value = value[2:-1]
    return value.strip()


def load_books(csv_file):
    """Load and validate books from CSV. Raises ValueError on validation failure."""
    try:
        text = csv_file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV file is not valid UTF-8 encoded.")
    
    if not text.strip():
        raise ValueError("CSV file is empty.")

    reader = csv.DictReader(io.StringIO(text))

    # Validate CSV structure
    is_valid, error_msg = validate_csv_structure(reader.fieldnames)
    if not is_valid:
        raise ValueError(error_msg)

    fieldset = set(reader.fieldnames)
    is_goodreads = "Exclusive Shelf" in fieldset and "Read Status" not in fieldset

    print("CSV columns:", reader.fieldnames)
    print("Detected format:", "Goodreads" if is_goodreads else "StoryGraph")

    books = []

    try:
        for row_num, row in enumerate(reader, start=2):  # start=2 accounts for header
            title = (row.get("Title") or "").strip()

            if is_goodreads:
                author = (row.get("Author") or "").strip()
                read_status = (row.get("Exclusive Shelf") or "").strip().lower()
                isbn = (
                    _clean_goodreads_isbn(row.get("ISBN13"))
                    or _clean_goodreads_isbn(row.get("ISBN"))
                )
                genre = None  # Goodreads has no dedicated genre column
            else:
                author = (row.get("Authors") or "").strip()
                read_status = (row.get("Read Status") or "").strip().lower()
                isbn = (row.get("ISBN/UID") or "").strip()  # Extract ISBN from CSV if available
                genre = (row.get("Tags") or "").strip()  # StoryGraph has no dedicated genre column; Tags is closest

            # Only analyze books marked "to-read"
            if read_status != "to-read":
                continue

            if not title:
                print(f"Warning: Row {row_num} skipped (missing title)")
                continue

            books.append({
                "title": title,
                "author": author,
                "isbn": isbn or None,  # Use ISBN from CSV; will look up if empty
                "synopsis": None,  # Will be fetched during analysis
                "genre": genre or None,
                "oakland": [],
                "state": "waiting",
                "message": "Waiting to be analyzed",
            })
    except csv.Error as e:
        raise ValueError(f"Error parsing CSV: {e}")

    if not books:
        raise ValueError("No 'to-read' books found in CSV.")

    print(f"Imported {len(books)} to-read books")

    return books


def serialize_result(result):
    return {
        "library": getattr(result, "library", None),
        "provider": getattr(result, "provider", None),
        "format": getattr(result, "format", None),
        "available": getattr(result, "available", False),
        "wait": getattr(result, "wait", None),
        "url": getattr(result, "url", None),
        "holds": getattr(result, "holds", None),
        "waitWeeks": getattr(result, "wait_weeks", None),
    }


def serialize_book(book):
    return {
        "title": book["title"],
        "author": book["author"],
        "isbn": book["isbn"],
        "synopsis": book.get("synopsis"),
        "genre": book.get("genre"),
        "state": book["state"],
        "message": book["message"],
        "oakland": [
            serialize_result(result)
            for result in book["oakland"]
        ],
    }


def analyze_book(job_id, index):
    """Analyze a single book and update its state. Always completes (or marks error)."""
    job = jobs.get(job_id)
    if not job:
        return  # Job was cleaned up
    
    book = job["books"][index]
    title = book["title"]
    author = book["author"]

    try:
        book["state"] = "checking"
        book["message"] = "Finding ISBN…"

        print(f"Analyzing: {title} — {author}")

        # Only look up ISBN if not already provided in CSV
        if book["isbn"]:
            isbn = book["isbn"]
            print(f"ISBN (from CSV): {isbn}")
        else:
            try:
                isbn = find_isbn(title, author)
                book["isbn"] = isbn
                print(f"ISBN (lookup): {isbn}")
            except Exception as e:
                error_type, error_msg = categorize_error(e)
                book["state"] = "error"
                book["message"] = error_msg
                print(f"ISBN lookup failed for '{title}': {error_type} - {e}")
                return

        # Fetch synopsis/genre in the background so it never slows down library search
        if isbn:
            def apply_metadata(book=book, isbn=isbn, title=title, author=author):
                synopsis, genre = fetch_synopsis(isbn, title, author)
                book["synopsis"] = synopsis
                if not book.get("genre") and genre:  # Keep CSV genre if present
                    book["genre"] = genre

            threading.Thread(target=apply_metadata, daemon=True).start()

        book["message"] = "Checking libraries…"

        try:
            if isbn:
                # By this point the upload route has already rejected any
                # request with zero libraries selected, so this should
                # always be populated — job.get(...) here is just a
                # defensive fallback, not a real default library choice.
                library_configs = job.get("library_configs") or []
                results = search_libraries(title, author, library_configs)
            else:
                results = []
                book["message"] = "ISBN not found; skipped library search."
        except Exception as e:
            error_type, error_msg = categorize_error(e)
            book["state"] = "error"
            book["message"] = error_msg
            print(f"Library search failed for '{title}': {error_type} - {e}")
            return

        book["oakland"] = results
        print(f"Results: {results}")

        book["state"] = "complete"
        book["message"] = "Complete"

    except Exception as error:
        print(f"Unexpected error analyzing {title}: {error}")
        book["state"] = "error"
        book["message"] = "Unexpected error. Please try again."

    finally:
        with job["lock"]:
            job["completed"] += 1


@app.route("/", methods=["GET"])
def home():
    # The TBR list (templates/tbr.html) is now the app's home screen.
    return redirect(url_for("tbr"))


@app.route("/status")
def status():

    job_id = request.args.get("job_id")

    if not job_id:
        return jsonify({
            "error": "Missing job_id parameter."
        }), 400

    job = jobs.get(job_id)

    if not job:
        return jsonify({
            "error": "Job not found. It may have expired."
        }), 404

    # Check if job has expired
    if time.time() - job["created_at"] > JOB_TIMEOUT_SECONDS:
        del jobs[job_id]
        return jsonify({
            "error": "Job expired after 1 hour."
        }), 410  # 410 Gone

    with job["lock"]:
        completed = job["completed"]

    books = [
        serialize_book(book)
        for book in job["books"]
        if book["state"] != "waiting"
    ]

    finished = completed >= len(job["books"])
    
    # Clean up completed jobs after response is sent
    if finished:
        # Could add: del jobs[job_id]  # but keep for UI to poll a few more times
        pass

    return jsonify({
        "total": len(job["books"]),
        "completed": completed,
        "books": books,
        "finished": finished,
    })


@app.route("/tbr")
def tbr():
    """Render the Phase 1 standalone TBR list interface."""
    return render_template("tbr.html")


@app.route("/terms")
def terms_page():
    """Terms of use for the public deployment (reachable without logging in)."""
    return render_template("terms.html")


@app.route("/privacy")
def privacy_page():
    """Privacy policy for the public deployment (reachable without logging in)."""
    return render_template("privacy.html")


# ============================================================================
# PHASE 1: STANDALONE TBR LIST — NEW API ENDPOINTS
# ============================================================================

@app.route("/api/books", methods=["GET"])
def list_books():
    """
    Get the user's TBR list.
    
    Returns list of UserBook entries with full book data.
    """
    try:
        user_books = (
            UserBook.query.options(
                joinedload(UserBook.book).joinedload(Book.availability)
            )
            .filter_by(user_id=_current_user_id(), status="tbr")
            .all()
        )
        enabled_keys = {
            lib.library_key
            for lib in LibraryConfig.query.filter_by(user_id=_current_user_id(), enabled=True).all()
        }
        books = []
        for user_book in user_books:
            book_data = user_book.to_dict_with_book()
            availability = user_book.book.availability if user_book.book else []

            # Full cached rows for the frontend's borrow-options/status model:
            # one entry per library/provider/format with holds and wait info.
            # Only include libraries that are currently enabled so deselecting
            # a library hides its (possibly stale) cached results immediately.
            if enabled_keys:
                availability = [a for a in availability if a.library in enabled_keys]
            book_data["availability"] = [result.to_dict() for result in availability]

            # Per-format summary used by the list-view filters and icon dots.
            formats_by_name = {}
            for result in availability:
                format_name = result.format or "Unknown"
                current = formats_by_name.get(format_name)
                if current and current["available"]:
                    continue
                formats_by_name[format_name] = {
                    "format": format_name,
                    "available": result.available,
                    "wait_text": result.wait_text,
                }
            book_data["availability_summary"] = {
                "checked": user_book.last_checked_at is not None,
                "available_now": any(result.available for result in availability),
                "formats": list(formats_by_name.values()),
            }
            books.append(book_data)

        return jsonify(books)
    except Exception as e:
        print(f"Error listing books: {e}")
        return jsonify({"error": "Failed to load books"}), 500


@app.route("/api/books/export.csv", methods=["GET"])
def export_books_csv():
    """Download the user's list as a CSV the import endpoint can re-import.

    Uses the Goodreads export column shape (Title, Author, Exclusive Shelf,
    ISBN13) because load_books accepts it and it preserves the ISBN.
    """
    user_books = (
        UserBook.query.options(joinedload(UserBook.book))
        .filter_by(user_id=_current_user_id(), status="tbr")
        .all()
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Title", "Author", "Exclusive Shelf", "ISBN13"])
    for user_book in user_books:
        book = user_book.book
        if not book:
            continue
        writer.writerow([book.title, book.author, "to-read", book.isbn or ""])
    payload = buffer.getvalue()
    return Response(
        payload,
        mimetype="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="mynextread-list.csv"'
        },
    )


@app.route("/api/books/search", methods=["POST"])
def search_books():
    """
    Search for a book by title and author.
    
    Uses Open Library API to find matching books.
    
    Request body:
        {
            "title": "The Overstory",
            "author": "Richard Powers"
        }
    
    Returns a list of potential matches.
    """
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    author = (data.get("author") or "").strip()
    
    if not title:
        return jsonify({"error": "Title is required"}), 400
    
    try:
        # Use Open Library's search API
        query = " ".join(part for part in (title, author) if part)
        search_params = {"q": query, "limit": 10}

        response = requests.get(
            "https://openlibrary.org/search.json",
            params=search_params,
            timeout=5,
            headers={"User-Agent": "LibraryAssistant/1.0"}
        )

        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            return jsonify({
                "error": "Book search is temporarily unavailable. Please try again."
            }), 503

        if not isinstance(data, dict):
            return jsonify({
                "error": "Book search is temporarily unavailable. Please try again."
            }), 503

        results = []
        
        for doc in data.get("docs", []):
            isbn = None
            if doc.get("isbn"):
                isbn = doc["isbn"][0]  # Take first ISBN
            
            result = {
                "title": doc.get("title", ""),
                "author": doc.get("author_name", ["Unknown"])[0] if doc.get("author_name") else "Unknown",
                "isbn": isbn,
                "cover_id": doc.get("cover_id"),
                "year": doc.get("first_publish_year"),
            }
            
            # Include all results regardless of ISBN
            # (ISBN will be looked up when book is added to library search)
            results.append(result)
        
        return jsonify({"results": results[:10]})

    except RequestException as e:
        print(f"Open Library search error: {e}")
        return jsonify({
            "error": "Book search is temporarily unavailable. Please try again."
        }), 503

    except Exception as e:
        print(f"Search error: {e}")
        return jsonify({"error": "Search failed"}), 500


def _find_existing_book(title, author, isbn):
    """Return an existing Book to reuse, preferring exact ISBN then title+author.

    Editions of the same work (different ISBNs, or a synthetic ISBN from a
    no-ISBN add) would otherwise create duplicate Book rows for one title,
    which shows up as duplicate lines in the TBR list.
    """
    if isbn:
        book = Book.query.filter_by(isbn=isbn).first()
        if book:
            return book
    t = (title or "").strip().lower()
    a = (author or "").strip().lower()
    if not t:
        return None
    q = Book.query.filter(db.func.lower(Book.title) == t)
    if a:
        q = q.filter(db.func.lower(Book.author) == a)
    return q.first()


@app.route("/api/books", methods=["POST"])
def add_book():
    """
    Add a book to the user's TBR list.
    
    Request body:
        {
            "title": "The Overstory",
            "author": "Richard Powers",
            "isbn": "9780393635522",
            "cover_url": "https://...",
            "synopsis": "...",
            "genre": "Fiction"
        }
    
    If the book already exists in the database, reuse it.
    Then create a UserBook entry if not already in the user's list.
    """
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    author = (data.get("author") or "").strip()
    isbn = (data.get("isbn") or "").strip()

    if not title:
        return jsonify({"error": "Title is required"}), 400
    
    # Generate a synthetic ISBN if not provided (using cover_id or title+author hash)
    if not isbn:
        import hashlib
        cover_id = data.get("cover_id")
        if cover_id:
            # Use cover_id as part of the synthetic ISBN
            isbn = f"cover-{cover_id}"
        else:
            # Use hash of title + author
            hash_input = f"{title}|{author}"
            isbn = f"synthetic-{hashlib.md5(hash_input.encode()).hexdigest()[:12]}"
    
    try:
        # Reuse an existing book (same ISBN, or same title+author) instead of
        # minting a new Book row per edition.
        book = _find_existing_book(title, author, isbn)

        if not book:
            # Create new book record
            book = Book(
                isbn=isbn,
                title=title,
                author=author,
                cover_url=data.get("cover_url"),
                synopsis=data.get("synopsis"),
                genre=data.get("genre"),
            )
            db.session.add(book)
            db.session.commit()
        
        # Check if already in user's TBR
        user_book = UserBook.query.filter_by(
            user_id=_current_user_id(),
            book_id=book.id,
            status="tbr"
        ).first()
        
        if user_book:
            return jsonify({"error": "Book already in your TBR"}), 409
        
        # Add to user's TBR
        user_book = UserBook(
            user_id=_current_user_id(),
            book_id=book.id,
            status="tbr"
        )
        db.session.add(user_book)
        db.session.commit()
        
        return jsonify(user_book.to_dict_with_book()), 201
    
    except Exception as e:
        db.session.rollback()
        print(f"Error adding book: {e}")
        return jsonify({"error": "Failed to add book"}), 500


@app.route("/api/books/<int:user_book_id>/synopsis", methods=["POST"])
def book_synopsis(user_book_id):
    """
    Fetch and cache a synopsis for a book in the user's TBR list.

    Books added via CSV import or the online search never got a synopsis
    fetched at add time; this endpoint resolves it lazily (with Open Library
    and a persisted cache on the Book record) the first time the detail
    sheet is opened.
    """
    user_book = UserBook.query.filter_by(id=user_book_id, user_id=_current_user_id()).first()
    if not user_book or not user_book.book:
        return jsonify({"error": "Book not found"}), 404

    book = user_book.book
    if not book.synopsis and not book.genre:
        try:
            synopsis, genre = fetch_synopsis(book.isbn, book.title, book.author)
            if synopsis:
                book.synopsis = synopsis
            if genre:
                book.genre = genre
            db.session.commit()
        except Exception as e:
            print(f"Synopsis fetch failed for '{book.title}': {e}")

    return jsonify({
        "id": book.id,
        "title": book.title,
        "synopsis": book.synopsis,
        "genre": book.genre,
    })


@app.route("/api/books/import-csv", methods=["POST"])
def import_tbr_csv():
    """Import to-read books from a Goodreads or StoryGraph export."""
    csv_file = request.files.get("books_file")
    if not csv_file or not csv_file.filename:
        return jsonify({"error": "Choose a Goodreads or StoryGraph CSV file."}), 400

    if not csv_file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Upload a CSV file."}), 400

    try:
        imported_books = load_books(csv_file)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    added = 0
    skipped = 0
    try:
        for imported_book in imported_books:
            title = imported_book["title"]
            author = imported_book.get("author") or "Unknown"
            isbn = imported_book.get("isbn")

            if not isbn:
                import hashlib
                hash_input = f"{title}|{author}"
                isbn = f"synthetic-{hashlib.md5(hash_input.encode()).hexdigest()[:12]}"

            book = _find_existing_book(title, author, isbn)
            if not book:
                book = Book(
                    isbn=isbn,
                    title=title,
                    author=author,
                    synopsis=imported_book.get("synopsis"),
                    genre=imported_book.get("genre"),
                )
                db.session.add(book)
                db.session.flush()

            user_book = UserBook.query.filter_by(
                user_id=_current_user_id(),
                book_id=book.id,
                status="tbr",
            ).first()
            if user_book:
                skipped += 1
                continue

            db.session.add(UserBook(user_id=_current_user_id(), book_id=book.id, status="tbr"))
            added += 1

        db.session.commit()
        return jsonify({"total": len(imported_books), "added": added, "skipped": skipped}), 201
    except Exception as e:
        db.session.rollback()
        print(f"Error importing TBR CSV: {e}")
        return jsonify({"error": "Failed to import this CSV."}), 500


@app.route("/api/books/<int:user_book_id>", methods=["DELETE"])
def remove_book(user_book_id):
    """
    Remove a book from the user's TBR list.
    """
    try:
        user_book = UserBook.query.filter_by(id=user_book_id, user_id=_current_user_id()).first()
        
        if not user_book:
            return jsonify({"error": "Book not found"}), 404
        
        db.session.delete(user_book)
        db.session.commit()
        
        return jsonify({"message": "Book removed"}), 200
    
    except Exception as e:
        db.session.rollback()
        print(f"Error removing book: {e}")
        return jsonify({"error": "Failed to remove book"}), 500


@app.route("/api/books/clear", methods=["DELETE"])
def clear_tbr_list():
    """
    Remove every book from the user's TBR list.

    Guarded by a required "confirm" token so it can't be triggered
    accidentally; Book/Availability records are left in place.
    """
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "clear_all":
        return jsonify({"error": "Confirmation required"}), 400

    try:
        deleted = UserBook.query.filter_by(user_id=_current_user_id(), status="tbr").delete()
        db.session.commit()
        return jsonify({"message": "Reading list cleared", "deleted": deleted}), 200
    except Exception as e:
        db.session.rollback()
        print(f"Error clearing TBR list: {e}")
        return jsonify({"error": "Failed to clear list"}), 500


@app.route("/api/books/<int:user_book_id>/check", methods=["POST"])
def check_availability(user_book_id):
    """
    Trigger an availability check for a book and return results.
    
    This is a synchronous check (not queued like CSV import).
    Results are cached in the database.
    """
    try:
        user_book = UserBook.query.filter_by(id=user_book_id, user_id=_current_user_id()).first()
        
        if not user_book or not user_book.book:
            return jsonify({"error": "Book not found"}), 404
        
        _refresh_availability(user_book, _library_search_configs())
        
        # Return results
        availability_data = [
            availability.to_dict()
            for availability in Availability.query.filter_by(book_id=user_book.book.id).all()
        ]
        
        return jsonify({
            "book": user_book.book.to_dict(),
            "availability": availability_data,
        }), 200
    
    except Exception as e:
        db.session.rollback()
        print(f"Error checking availability: {e}")
        return jsonify({"error": "Failed to check availability"}), 500


def _library_search_configs():
    """Return enabled library settings in the catalog search format."""
    configs = []
    for library in LibraryConfig.query.filter_by(user_id=_current_user_id(), enabled=True).all():
        config = {"key": library.library_key}
        if library.bibliocommons:
            config["bibliocommons"] = library.bibliocommons
        if library.overdrive:
            config["overdrive"] = library.overdrive
        if library.hoopla:
            config["hoopla"] = True
        configs.append(config)
    return configs


def _refresh_availability(user_book, configs):
    """Replace cached availability for one TBR entry."""
    book = user_book.book
    results = search_libraries(book.title, book.author, configs)

    # Snapshot prior availability so we can detect a "became available" flip.
    previous = {
        (r.library, (r.format or "").lower()): bool(r.available)
        for r in Availability.query.filter_by(book_id=book.id).all()
    }

    Availability.query.filter_by(book_id=book.id).delete()

    new_rows = []
    for result in results:
        new_rows.append(Availability(
            book_id=book.id,
            library=getattr(result, "library", "unknown"),
            provider=getattr(result, "provider", "unknown"),
            format=getattr(result, "format", "unknown"),
            available=getattr(result, "available", False),
            wait_text=getattr(result, "wait", None),
            holds=getattr(result, "holds", None),
            wait_weeks=getattr(result, "wait_weeks", None),
            url=getattr(result, "url", None),
        ))
        db.session.add(new_rows[-1])

    user_book.last_checked_at = datetime.now(timezone.utc)
    db.session.commit()

    available_now = {
        (r.library, (r.format or "").lower()): r.available
        for r in new_rows
    }
    if _became_available(previous, available_now):
        _notify_available(
            book.id,
            book.title,
            book.author,
            [r for r in new_rows if r.available],
        )


def _became_available(previous, current):
    """True when something previously known-unavailable is now available.

    A key counts only if it had a recorded prior state (so the very first
    scan, where previous is empty, never triggers an alert).
    """
    return any(
        available
        and key in previous
        and previous[key] is False
        for key, available in current.items()
    )


def _notify_available(book_id, title, author, available_rows):
    """Email every subscriber who has this book on their list.

    Runs in (or pushes) an app context so worker threads and requests both
    work. Sending is delegated to mailer.submit_email (background daemon
    thread); tests monkeypatch it.
    """
    try:
        owners = (
            db.session.query(User)
            .join(UserBook, UserBook.user_id == User.id)
            .filter(
                UserBook.book_id == book_id,
                UserBook.status == "tbr",
                User.email.isnot(None),
                User.notify_on_available.is_(True),
            )
            .distinct()
            .all()
        )
        if not owners or not available_rows:
            return

        lines = []
        for row in available_rows:
            fmt = row.format if row.format else "book"
            lines.append(f"{row.library} · {fmt}")
        summary = ", ".join(sorted(set(lines)))

        subject = f"\u201c{title}\u201d is available now"
        text = (
            f"{title}{' by ' + author if author else ''} just became available "
            f"at your libraries.\n\nAvailable now: {summary}\n\n"
            "Open your reading list to borrow it."
        )
        for user in owners:
            mailer.submit_email(user.email, subject, text=text)
    except Exception as e:  # noqa: BLE001 - alerts must never break a scan
        print(f"Error sending availability alert for book {book_id}: {e}")


def _refresh_one_book(user_book_id, configs, on_change=None):
    """Refresh one TBR entry in a worker thread.

    Each worker pushes its own app context so Flask-SQLAlchemy hands it a
    private scoped session (no cross-thread session sharing).

    ``on_change(active, title)`` is called with True when the book begins
    scanning and False when it finishes (success or failure), so the scan
    supervisor can report which titles are currently in flight.
    """
    with app.app_context():
        user_book = UserBook.query.filter_by(id=user_book_id).first()
        if not user_book or not user_book.book:
            return {"id": user_book_id, "title": None, "error": "missing"}
        title = user_book.book.title
        if on_change:
            on_change(True, title)
        try:
            _refresh_availability(user_book, configs)
            return {"id": user_book_id, "title": title}
        except Exception as e:
            db.session.rollback()
            print(f"Error checking {title}: {e}")
            return {"id": user_book_id, "title": title, "error": str(e)}
        finally:
            if on_change:
                on_change(False, title)


@app.route("/api/books/check-all", methods=["POST"])
def start_check_all():
    """Start a background availability scan of every TBR entry.

    Returns immediately with a job id; the scan continues on the shared
    worker pool and progress is read via GET /api/books/scan-progress/<id>.
    This keeps the request short so slow scans can't time out mobile or
    desktop browsers (a full scan can take minutes on real networks).
    """
    global _active_scan

    configs = _library_search_configs()
    if not configs:
        return jsonify({"error": "No libraries configured"}), 400

    with _scan_lock:
        if _active_scan and not _active_scan["done"]:
            return jsonify({
                "job_id": _active_scan["id"],
                "total": _active_scan["total"],
                "already_running": True,
            }), 202

        # Drop cached results for libraries that are no longer enabled, so a
        # deselected library's stale availability can't resurface.
        enabled_keys = {c["key"] for c in configs}
        stale_libraries = {
            key
            for (key,) in db.session.query(Availability.library).distinct()
            if key not in enabled_keys
        }
        for key in stale_libraries:
            Availability.query.filter_by(library=key).delete(
                synchronize_session=False
            )
        if stale_libraries:
            db.session.commit()

        user_books = UserBook.query.filter_by(user_id=_current_user_id(), status="tbr").all()
        if not user_books:
            return jsonify({"total": 0, "checked": 0, "failures": [], "done": True}), 200

        job = {
            "id": uuid.uuid4().hex[:12],
            "total": len(user_books),
            "processed": 0,
            "checked": 0,
            "failures": [],
            "current": [],
            "done": False,
        }
        _active_scan = job

    scan_ids = [ub.id for ub in user_books]
    threading.Thread(target=_run_scan, args=(job, scan_ids, configs), daemon=True).start()

    return jsonify({"job_id": job["id"], "total": job["total"], "already_running": False}), 202


def _run_scan(job, user_book_ids, configs):
    """Fan out a scan across the worker pool and track progress on the job.

    Each _refresh_one_book pushes its own app context, so this supervisor
    thread never touches a scoped session.
    """
    try:
        def _track(active, title):
            with _scan_lock:
                if active:
                    if title not in job["current"]:
                        job["current"].append(title)
                elif title in job["current"]:
                    job["current"].remove(title)

        futures = [
            executor.submit(_refresh_one_book, user_book_id, configs, _track)
            for user_book_id in user_book_ids
        ]
        for future, user_book_id in zip(futures, user_book_ids):
            result = None
            try:
                result = future.result()
                if result.get("error"):
                    with _scan_lock:
                        job["failures"].append({"id": result["id"], "title": result["title"]})
            except Exception as e:
                result = {"error": str(e)}
                with _scan_lock:
                    job["failures"].append({"id": user_book_id, "title": None, "error": str(e)})
            finally:
                with _scan_lock:
                    job["processed"] += 1
                    if not (result or {}).get("error"):
                        job["checked"] += 1
    finally:
        with _scan_lock:
            job["done"] = True


@app.route("/api/books/scan-progress/<job_id>", methods=["GET"])
def scan_progress(job_id):
    """Progress of a running/finished availability scan."""
    with _scan_lock:
        if not _active_scan or _active_scan["id"] != job_id:
            return jsonify({"error": "Unknown scan"}), 404
        snapshot = dict(_active_scan)
        snapshot["failures"] = list(_active_scan["failures"])
        snapshot["current"] = list(_active_scan["current"])
    return jsonify(snapshot), 200


@app.route("/api/books/<int:user_book_id>/availability", methods=["GET"])
def get_availability(user_book_id):
    """
    Get cached availability for a book.
    """
    try:
        user_book = UserBook.query.filter_by(id=user_book_id, user_id=_current_user_id()).first()
        
        if not user_book or not user_book.book:
            return jsonify({"error": "Book not found"}), 404
        
        book = user_book.book
        availability = Availability.query.filter_by(book_id=book.id).all()
        
        return jsonify({
            "book": book.to_dict(),
            "availability": [a.to_dict() for a in availability],
            "last_checked_at": user_book.last_checked_at.isoformat() if user_book.last_checked_at else None,
        }), 200
    
    except Exception as e:
        print(f"Error getting availability: {e}")
        return jsonify({"error": "Failed to get availability"}), 500


@app.route("/api/libraries", methods=["GET"])
def get_libraries():
    """
    Get all available libraries and their current enabled status.
    """
    try:
        libraries = LibraryConfig.query.filter_by(user_id=_current_user_id()).all()
        return jsonify([lib.to_dict() for lib in libraries])
    except Exception as e:
        print(f"Error getting libraries: {e}")
        return jsonify({"error": "Failed to get libraries"}), 500


@app.route("/api/libraries/available", methods=["GET"])
def available_libraries():
    """Libraries the user has not added yet (from the shipped presets)."""
    try:
        existing = {
            c.library_key
            for c in LibraryConfig.query.filter_by(user_id=_current_user_id()).all()
        }
        result = [
            {
                "library_key": key,
                "label": preset["label"],
                "sub": (
                    "Unlimited borrows" if preset.get("hoopla")
                    else "Libby / OverDrive" if preset.get("overdrive")
                    else "Bibliocommons"
                ),
            }
            for key, preset in LIBRARY_PRESETS.items()
            if key not in existing
        ]
        return jsonify(result)
    except Exception as e:
        print(f"Error listing available libraries: {e}")
        return jsonify({"error": "Failed to list libraries"}), 500


@app.route("/api/libraries/<int:library_id>", methods=["PATCH"])
def update_library(library_id):
    """
    Enable or disable a library.
    
    Request body:
        {
            "enabled": true/false
        }
    """
    try:
        library = LibraryConfig.query.filter_by(id=library_id, user_id=_current_user_id()).first()
        
        if not library:
            return jsonify({"error": "Library not found"}), 404
        
        data = request.get_json() or {}
        library.enabled = bool(data.get("enabled", library.enabled))
        
        db.session.commit()
        return jsonify(library.to_dict()), 200
    
    except Exception as e:
        db.session.rollback()
        print(f"Error updating library: {e}")
        return jsonify({"error": "Failed to update library"}), 500


@app.route("/api/libraries", methods=["POST"])
def add_library():
    """
    Add a library to the user's configuration and enable it.

    Preset: {"library_key": "sfpl"}
    Custom: {"library_key": "my_lib", "label": "...", "bibliocommons": "subdomain"}
            or {"library_key": "my_lib", "label": "...", "overdrive": "subdomain"}
            or {"library_key": "my_lib", "label": "..."}  # label only; not searched
    """
    try:
        data = request.get_json() or {}
        library_key = (data.get("library_key") or "").strip().lower()
        if not library_key:
            return jsonify({"error": "library_key is required"}), 400

        existing = LibraryConfig.query.filter_by(user_id=_current_user_id(), library_key=library_key).first()
        if existing:
            return jsonify({"error": "That library is already configured"}), 409

        preset = LIBRARY_PRESETS.get(library_key)
        if preset:
            label = preset["label"]
            bibliocommons = preset.get("bibliocommons")
            overdrive = preset.get("overdrive")
            hoopla = preset.get("hoopla", False)
        else:
            label = (data.get("label") or "").strip()
            if not label:
                return jsonify({"error": "Unknown library_key; provide a label for a custom library"}), 400
            bibliocommons = (data.get("bibliocommons") or "").strip() or None
            overdrive = (data.get("overdrive") or "").strip() or None
            hoopla = bool(data.get("hoopla"))
            # A label-only custom library is allowed (it just isn't searched).

        new_library = LibraryConfig(
            user_id=_current_user_id(),
            library_key=library_key,
            label=label,
            bibliocommons=bibliocommons,
            overdrive=overdrive,
            hoopla=hoopla,
            enabled=True,
        )
        db.session.add(new_library)
        db.session.commit()
        return jsonify(new_library.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        print(f"Error adding library: {e}")
        return jsonify({"error": "Failed to add library"}), 500


if __name__ == "__main__":
    # PORT is set by hosting platforms (Render, Railway, etc.) at runtime;
    # 5001 is the local fallback, chosen to avoid colliding with macOS's
    # AirPlay Receiver, which uses 5000.
    port = int(os.environ.get("PORT", 5001))

    # debug=True must NEVER be on for anything reachable outside your own
    # machine — Flask's debugger allows arbitrary code execution to anyone
    # who can reach it. Set FLASK_DEBUG=1 locally if you want it back for
    # development; it's off by default now so a deploy can't accidentally
    # ship with it on.
    debug = os.environ.get("FLASK_DEBUG") == "1"

    # host="0.0.0.0" makes this reachable from other devices (e.g. your
    # phone) on the same network, and is also what hosting platforms expect.
    app.run(debug=debug, host="0.0.0.0", port=port)