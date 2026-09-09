"""Tests for the public-release hardening (wish-list Steps 2, 3, 4, 5, 6, 7, 11).

Covers the security headers and session-cookie flags, terms/privacy pages,
auth rate limiting and brute-force lockout, the registration honeypot, the
opt-in ADOPT_LEGACY_ROWS gate, the CSV export, account deletion, the digest
webhook, and per-user scoping of the synopsis endpoint.
"""

import csv
import io
import time

import pytest

from tests.test_notifications import (
    add_book,
    capture_emails,
    enable_library,
    seed_availability,
    set_prefs,
    set_reader_email,
)


# ---------- Step 6: headers + session cookie flags ----------

def test_security_headers(client):
    resp = client.get("/tbr")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "same-origin"
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def _session_cookie(resp):
    for value in resp.headers.get_all("Set-Cookie"):
        if value.split(";", 1)[0].strip().startswith("session="):
            return value
    return ""


def test_session_cookie_secure_on_https(raw_client):
    resp = raw_client.post(
        "/api/auth/register",
        json={"username": "httpsuser", "password": "secret1"},
        environ_overrides={"wsgi.url_scheme": "https"},
    )
    assert resp.status_code == 201
    cookie = _session_cookie(resp)
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_session_cookie_not_secure_on_plain_http(raw_client):
    resp = raw_client.post(
        "/api/auth/register",
        json={"username": "httpuser", "password": "secret1"},
    )
    assert resp.status_code == 201
    cookie = _session_cookie(resp)
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie


# ---------- Step 4: terms + privacy pages are public ----------

def test_terms_page_public(raw_client):
    resp = raw_client.get("/terms")
    assert resp.status_code == 200
    assert "MyNextRead" in resp.get_data(as_text=True)


def test_privacy_page_public(raw_client):
    resp = raw_client.get("/privacy")
    assert resp.status_code == 200
    assert "What we store" in resp.get_data(as_text=True)


# ---------- Step 5: auth abuse protection ----------

@pytest.mark.no_auth
def test_register_is_rate_limited(raw_client):
    for i in range(8):
        resp = raw_client.post(
            "/api/auth/register",
            json={"username": f"user{i}", "password": "secret1"},
        )
        assert resp.status_code == 201, resp.get_json()

    resp = raw_client.post(
        "/api/auth/register",
        json={"username": "spammy", "password": "secret1"},
    )
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "60"


@pytest.mark.no_auth
def test_login_locks_account_after_failures(raw_client, app_context, monkeypatch):
    import app as app_module

    # Raise the per-IP throttle so only the lockout under test fires, and lower
    # the failure threshold so the test stays quick.
    monkeypatch.setattr(app_module, "_AUTH_RATE_LIMIT_PER_WINDOW", 100)
    monkeypatch.setattr(app_module, "_MAX_FAILED_LOGINS", 3)

    assert raw_client.post(
        "/api/auth/register",
        json={"username": "locky", "password": "secret1"},
    ).status_code == 201

    for _ in range(3):
        resp = raw_client.post(
            "/api/auth/login",
            json={"username": "locky", "password": "wrong!"},
        )
        assert resp.status_code == 401

    resp = raw_client.post(
        "/api/auth/login",
        json={"username": "locky", "password": "secret1"},
    )
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "900"

    with app_context.app.app_context():
        app_module._clear_login_fail("locky")
    resp = raw_client.post(
        "/api/auth/login",
        json={"username": "locky", "password": "secret1"},
    )
    assert resp.status_code == 200


@pytest.mark.no_auth
def test_register_honeypot_silently_ignored(raw_client):
    resp = raw_client.post(
        "/api/auth/register",
        json={"username": "robot", "password": "secret1", "website": "http://spam.example"},
    )
    assert resp.status_code == 201
    assert resp.get_json()["user"]["id"] == 0

    assert raw_client.get("/api/books").status_code == 401
    from models import User
    assert User.query.count() == 0


# ---------- Step 7: opt-in legacy adoption ----------

@pytest.mark.no_auth
def test_second_account_cannot_claim_legacy_rows(raw_client, app_context):
    """The pre-accounts list lives under user_id=1 forever; a public signup
    (always id != 1 once another account exists) must never inherit it."""
    from werkzeug.security import generate_password_hash

    from models import Book, LibraryConfig, User, UserBook, db

    with app_context.app.app_context():
        owner = User(username="owner", password_hash=generate_password_hash("x"))
        db.session.add(owner)
        db.session.flush()
        assert owner.id == 1
        legacy_book = Book(title="Legacy Title", author="Legacy Author", isbn="legacy-isbn")
        db.session.add(legacy_book)
        db.session.flush()
        db.session.add(UserBook(user_id=owner.id, book_id=legacy_book.id, status="tbr"))
        db.session.add(LibraryConfig(
            user_id=owner.id, library_key="oakland", label="Oakland Public Library",
            bibliocommons="oaklandlibrary", enabled=True,
        ))
        db.session.commit()

    resp = raw_client.post(
        "/api/auth/register",
        json={"username": "stranger", "password": "secret1"},
    )
    assert resp.status_code == 201
    stranger_id = resp.get_json()["user"]["id"]
    assert stranger_id != 1

    with app_context.app.app_context():
        assert UserBook.query.filter_by(user_id=1).count() == 1
        assert LibraryConfig.query.filter_by(user_id=1).count() == 1
        assert UserBook.query.filter_by(user_id=stranger_id).count() == 0

    # The stranger also sees an empty list through the API.
    assert raw_client.get("/api/books").get_json() == []


@pytest.mark.no_auth
def test_adopt_flag_gate_only_matters_for_first_signup(raw_client, app_context, monkeypatch):
    """With ADOPT_LEGACY_ROWS=1 the very first signup may claim a stranded
    user_id=1 list; any later signup never does (regardless of the flag)."""
    from werkzeug.security import generate_password_hash

    from models import Book, LibraryConfig, User, UserBook, db

    monkeypatch.setenv("ADOPT_LEGACY_ROWS", "1")

    with app_context.app.app_context():
        owner = User(username="stakeholder", password_hash=generate_password_hash("x"))
        db.session.add(owner)
        db.session.flush()
        assert owner.id == 1
        legacy_book = Book(title="Legacy Title", author="Legacy Author", isbn="legacy-isbn")
        db.session.add(legacy_book)
        db.session.flush()
        db.session.add(UserBook(user_id=owner.id, book_id=legacy_book.id, status="tbr"))
        db.session.add(LibraryConfig(
            user_id=owner.id, library_key="oakland", label="Oakland Public Library",
            bibliocommons="oaklandlibrary", enabled=True,
        ))
        db.session.commit()

    resp = raw_client.post(
        "/api/auth/register",
        json={"username": "latecomer", "password": "secret1"},
    )
    assert resp.status_code == 201
    assert resp.get_json()["user"]["id"] != 1

    with app_context.app.app_context():
        assert UserBook.query.filter_by(user_id=1).count() == 1
        assert LibraryConfig.query.filter_by(user_id=1).count() == 1


# ---------- Step 11: export ----------

def test_export_csv_roundtrips(client):
    book = add_book(client)
    resp = client.get("/api/books/export.csv")
    assert resp.status_code == 200
    assert resp.headers.get("Content-Type", "").startswith("text/csv")
    assert "attachment" in resp.headers.get("Content-Disposition", "")

    reader = csv.DictReader(io.StringIO(resp.get_data(as_text=True)))
    rows = list(reader)
    assert rows[0]["Title"] == book["book"]["title"]
    assert rows[0]["Author"] == book["book"]["author"]
    assert rows[0]["Exclusive Shelf"] == "to-read"
    assert rows[0]["ISBN13"] == (book["book"].get("isbn") or "")


# ---------- Step 11: account deletion ----------

def test_delete_account_requires_confirmation(client, current_user_id, app_context):
    resp = client.delete("/api/auth/account", json={})
    assert resp.status_code == 400

    from models import User, db
    with app_context.app.app_context():
        assert db.session.get(User, current_user_id) is not None


def test_delete_account_keeps_shared_books(client, current_user_id, app_context):
    from werkzeug.security import generate_password_hash

    from models import Book, LibraryConfig, User, UserBook, db

    book = add_book(client)
    with app_context.app.app_context():
        other = User(username="otherreader", password_hash=generate_password_hash("x"))
        db.session.add(other)
        db.session.flush()
        db.session.add(UserBook(user_id=other.id, book_id=book["id"], status="tbr"))
        db.session.commit()
        other_id = other.id

    resp = client.delete("/api/auth/account", json={"confirm": "delete_account"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted_books"] == 0

    with app_context.app.app_context():
        assert db.session.get(User, current_user_id) is None
        assert UserBook.query.filter_by(user_id=current_user_id).count() == 0
        assert LibraryConfig.query.filter_by(user_id=current_user_id).count() == 0
        assert UserBook.query.filter_by(user_id=other_id).count() == 1
        assert Book.query.count() == 1

    assert client.get("/api/auth/me").status_code == 401


def test_delete_account_removes_orphan_books(client, current_user_id, app_context):
    from models import Book, UserBook

    add_book(client)
    resp = client.delete("/api/auth/account", json={"confirm": "delete_account"})
    assert resp.status_code == 200
    assert resp.get_json()["deleted_books"] == 1

    with app_context.app.app_context():
        assert Book.query.count() == 0
        assert UserBook.query.count() == 0


# ---------- Step 3: digest webhook ----------

def test_digest_webhook_404_when_not_configured(client):
    import os
    if os.environ.get("CRON_TOKEN"):
        pytest.skip("CRON_TOKEN is set in this environment")
    assert client.post("/api/system/digest").status_code == 404


def test_digest_webhook_rejects_bad_token(client, monkeypatch):
    monkeypatch.setenv("CRON_TOKEN", "right-token")
    resp = client.post("/api/system/digest", headers={"X-Cron-Token": "wrong-token"})
    assert resp.status_code == 401


def test_digest_webhook_runs_digest(client, app_context, monkeypatch):
    monkeypatch.setenv("CRON_TOKEN", "right-token")
    calls = capture_emails(monkeypatch)
    set_prefs(client, weekly_digest=True)
    set_reader_email(app_context)
    enable_library(client, "lapl")
    book = add_book(client)
    seed_availability(app_context, book["id"], library="lapl", available=True)

    resp = client.post(
        "/api/system/digest",
        headers={"Authorization": "Bearer right-token"},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"digest_sent": 1}
    assert len(calls) == 1


# ---------- Step: per-user scoping ----------

def test_synopsis_rejects_other_users_book(client, app_context):
    from werkzeug.security import generate_password_hash

    from models import User, UserBook, db

    book = add_book(client)
    with app_context.app.app_context():
        other = User(username="syndoiac", password_hash=generate_password_hash("x"))
        db.session.add(other)
        db.session.flush()
        db.session.add(UserBook(user_id=other.id, book_id=book["id"], status="tbr"))
        db.session.commit()
        other_row = UserBook.query.filter_by(user_id=other.id).one().id

    resp = client.post(f"/api/books/{other_row}/synopsis")
    assert resp.status_code == 404