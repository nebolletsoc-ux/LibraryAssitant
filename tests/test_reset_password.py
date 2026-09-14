"""Tests for the forgot/reset password flow and library-label capitalization.

The reset flow mints a time-limited token, emails a click-link, and lets the
user set a new password via GET /reset-password + POST /api/auth/reset-password.
Library labels now come from the user's LibraryConfig.label (or the shipped
preset label) rather than the raw key.
"""

import hashlib
import re
from datetime import datetime, timedelta

import pytest

from tests.test_notifications import add_book, enable_library, seed_availability, set_reader_email


@pytest.fixture()
def enabled_mail(monkeypatch):
    import mailer

    monkeypatch.setattr(mailer, "is_enabled", lambda: True)


def capture_emails(monkeypatch):
    import mailer

    calls = []
    monkeypatch.setattr(
        mailer,
        "submit_email",
        lambda to, subject, text="", html=None: calls.append((to, subject, text, html)),
    )
    return calls


def _token_from_email(calls):
    html = calls[-1][3] or ""
    m = re.search(r"/reset-password\?token=([A-Za-z0-9_\-]+)", html)
    assert m, "reset email should contain a clickable token link"
    return m.group(1)


def hashlib_hex(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------- forgot-password endpoint ----------

def test_forgot_password_sends_reset_email_and_sets_hashed_token(client, raw_client, app_context, enabled_mail, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_reader_email(app_context, email="reader@example.com")

    resp = raw_client.post(
        "/api/auth/forgot-password",
        json={"username": "tester"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"sent": True}
    assert len(calls) == 1

    to, subject, text, html = calls[0]
    assert to == "reader@example.com"
    assert subject == "Reset your MyNextRead password"
    assert "/reset-password?token=" in html

    token = _token_from_email(calls)
    with app_context.app.app_context():
        from models import User
        user = User.query.filter_by(username="tester").one()
        assert user.password_reset_hash == hashlib_hex(token)
        assert user.password_reset_hash != token
        assert user.password_reset_expires is not None


def test_forgot_password_unknown_username_is_generic(raw_client, enabled_mail, monkeypatch):
    calls = capture_emails(monkeypatch)
    resp = raw_client.post(
        "/api/auth/forgot-password",
        json={"username": "ghost"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"sent": True}
    assert calls == []


def test_forgot_password_no_email_gets_explicit_message(raw_client, enabled_mail):
    resp = raw_client.post(
        "/api/auth/forgot-password",
        json={"username": "tester"},
        content_type="application/json",
    )
    data = resp.get_json()
    assert data["sent"] is False
    assert "no email address on file" in data["message"]


def test_forgot_password_rejects_missing_username(raw_client, enabled_mail):
    resp = raw_client.post(
        "/api/auth/forgot-password",
        json={},
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "Enter your username" in resp.get_json()["error"]


def test_forgot_password_rate_limited(raw_client, app_context):
    for _ in range(8):
        resp = raw_client.post(
            "/api/auth/forgot-password",
            json={"username": "ghost"},
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.get_json()

    resp = raw_client.post(
        "/api/auth/forgot-password",
        json={"username": "ghost"},
        content_type="application/json",
    )
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "60"


# ---------- reset flow ----------

def _reset_token(client, raw_client, app_context, enabled_mail, monkeypatch):
    """Trigger a reset email and return the clickable token."""
    calls = capture_emails(monkeypatch)
    set_reader_email(app_context, email="reader@example.com")
    raw_client.post(
        "/api/auth/forgot-password",
        json={"username": "tester"},
        content_type="application/json",
    )
    return _token_from_email(calls)


def test_reset_password_sets_new_password(client, raw_client, app_context, enabled_mail, monkeypatch):
    token = _reset_token(client, raw_client, app_context, enabled_mail, monkeypatch)

    resp = raw_client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "newpass123"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    # Old password no longer works.
    assert raw_client.post(
        "/api/auth/login",
        json={"username": "tester", "password": "testpassword"},
        content_type="application/json",
    ).status_code == 401

    # New password works.
    login = raw_client.post(
        "/api/auth/login",
        json={"username": "tester", "password": "newpass123"},
        content_type="application/json",
    )
    assert login.status_code == 200

    # Token destroyed.
    with app_context.app.app_context():
        from models import User
        user = User.query.filter_by(username="tester").one()
        assert user.password_reset_hash is None
        assert user.password_reset_expires is None


def test_reset_password_rejects_short_password(client, raw_client, app_context, enabled_mail, monkeypatch):
    token = _reset_token(client, raw_client, app_context, enabled_mail, monkeypatch)

    resp = raw_client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "short"},
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "at least 6 characters" in resp.get_json()["error"]


def test_reset_password_rejects_expired_token(client, raw_client, app_context, enabled_mail, monkeypatch):
    token = _reset_token(client, raw_client, app_context, enabled_mail, monkeypatch)

    with app_context.app.app_context():
        from models import User
        user = User.query.filter_by(username="tester").one()
        user.password_reset_expires = datetime.utcnow() - timedelta(hours=1)
        from models import db
        db.session.commit()

    resp = raw_client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "newpass123"},
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "invalid or has expired" in resp.get_json()["error"]


def test_reset_password_rejects_garbage_token(raw_client):
    resp = raw_client.post(
        "/api/auth/reset-password",
        json={"token": "not-a-real-token", "password": "newpass123"},
        content_type="application/json",
    )
    assert resp.status_code == 400


# ---------- reset page ----------

def test_reset_page_missing_token_returns_400(raw_client):
    resp = raw_client.get("/reset-password")
    assert resp.status_code == 400
    assert b"Missing reset link" in resp.data


def test_reset_page_valid_token_returns_200(client, raw_client, app_context, enabled_mail, monkeypatch):
    token = _reset_token(client, raw_client, app_context, enabled_mail, monkeypatch)
    resp = raw_client.get(f"/reset-password?token={token}")
    assert resp.status_code == 200
    assert b"Reset password" in resp.data


# ---------- library label capitalization ----------

def test_digest_uses_readable_library_labels(client, app_context, monkeypatch):
    calls = []
    import mailer
    monkeypatch.setattr(
        mailer,
        "submit_email",
        lambda to, subject, text="", html=None: calls.append((to, subject, text, html)),
    )

    set_reader_email(app_context, email="reader@example.com")
    client.patch("/api/user/preferences", json={"weekly_digest": True}).get_json()
    enable_library(client, "gutenberg")
    book = add_book(client)
    seed_availability(app_context, book["id"], library="gutenberg", available=True, format_="eBook")

    import app as app_module
    with app_context.app.app_context():
        sent = app_module._run_weekly_digest()

    assert sent == 1
    body = calls[0][2]
    assert "Project Gutenberg" in body
    # Raw key should NOT appear in the human-readable digest text.
    assert "\ngutenberg " not in body.lower()


def test_library_labels_for_uses_config_label_then_preset(app_context):
    from app import _library_labels_for
    from models import LibraryConfig, User, db

    with app_context.app.app_context():
        user = User.query.filter_by(username="tester").one()
        # Custom config label overrides preset.
        config = LibraryConfig.query.filter_by(user_id=user.id, library_key="gutenberg").one()
        config.label = "Gutenberg Books"
        db.session.commit()

        labels = _library_labels_for(user)
        assert labels["gutenberg"] == "Gutenberg Books"
        # Preset fills in for keys without a user config (rare in prod).
        assert labels.get("openlibrary") == "Open Library (free ebooks)"
