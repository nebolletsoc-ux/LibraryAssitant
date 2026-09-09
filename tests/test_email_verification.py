"""Tests for the click-link email verification flow.

A new/changed email in Settings triggers a one-time verification email whose
token is stored only as a SHA-256 hash; the link (GET /verify-email) marks the
address verified, and only verified addresses receive alerts and the digest.
"""

import re
from datetime import datetime, timedelta

import pytest

from test_notifications import add_book, enable_library, seed_availability, set_prefs


def capture_emails(monkeypatch):
    import mailer

    calls = []
    monkeypatch.setattr(
        mailer,
        "submit_email",
        lambda to, subject, text="", html=None: calls.append((to, subject, text, html)),
    )
    return calls


@pytest.fixture()
def enabled_mail(monkeypatch):
    import mailer

    monkeypatch.setattr(mailer, "is_enabled", lambda: True)


def _token_from_email(calls):
    html = calls[-1][3] or ""
    m = re.search(r"/verify-email\?token=([A-Za-z0-9_\-]+)", html)
    assert m, "verification email should contain a clickable token"
    return m.group(1)


def test_saving_email_sends_verification_link(client, app_context, enabled_mail, monkeypatch):
    calls = capture_emails(monkeypatch)

    prefs = set_prefs(client, email="reader@example.com")

    assert prefs["email"] == "reader@example.com"
    assert prefs["email_verified"] is False
    assert prefs["email_pending"] is True
    assert len(calls) == 1
    to, subject, text, html = calls[0]
    assert to == "reader@example.com"
    assert subject == "Verify your email for MyNextRead"
    assert "verify-email?token=" in html

    # Only the SHA-256 hash is persisted, never the raw token.
    from models import User

    with app_context.app.app_context():
        user = User.query.filter_by(username="tester").one()
        token = _token_from_email(calls)
        assert user.email_verify_hash == hashlib_hex(token)
        assert user.email_verify_hash != token
        assert user.email_verify_expires is not None


def test_verify_link_marks_email_verified(client, app_context, raw_client, enabled_mail, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com")

    resp = raw_client.get(f"/verify-email?token={_token_from_email(calls)}")
    assert resp.status_code == 200
    assert b"Email verified" in resp.data

    prefs = client.get("/api/user/preferences").get_json()
    assert prefs["email_verified"] is True
    assert prefs["email_pending"] is False

    # The used token is destroyed so it can't be replayed.
    from models import User

    with app_context.app.app_context():
        user = User.query.filter_by(username="tester").one()
        assert user.email_verify_hash is None
        assert user.email_verify_expires is None
    assert raw_client.get(f"/verify-email?token={_token_from_email(calls)}").status_code == 400


def test_verify_link_rejects_missing_or_garbage_token(raw_client):
    assert raw_client.get("/verify-email").status_code == 400
    assert raw_client.get("/verify-email?token=not-a-real-token").status_code == 400


def test_verify_link_rejects_expired_token(client, app_context, raw_client, enabled_mail, monkeypatch):
    from models import User

    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com")
    with app_context.app.app_context():
        user = User.query.filter_by(username="tester").one()
        user.email_verify_expires = datetime.utcnow() - timedelta(hours=1)
        from models import db

        db.session.commit()

    assert raw_client.get(f"/verify-email?token={_token_from_email(calls)}").status_code == 400
    assert client.get("/api/user/preferences").get_json()["email_verified"] is False


def test_resend_verification_replaces_token(client, raw_client, enabled_mail, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com")
    first_token = _token_from_email(calls)

    resp = client.post("/api/user/preferences/resend-verification")
    assert resp.status_code == 200
    assert len(calls) == 2
    assert calls[1][3] and "verify-email?token=" in calls[1][3]

    second_token = _token_from_email(calls)
    assert second_token != first_token

    # Rotating invalidates the old link.
    assert raw_client.get(f"/verify-email?token={first_token}").status_code == 400
    assert raw_client.get(f"/verify-email?token={second_token}").status_code == 200


def test_unchanged_email_does_not_verify_again(client, raw_client, enabled_mail, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com")
    assert raw_client.get(f"/verify-email?token={_token_from_email(calls)}").status_code == 200
    assert len(calls) == 1

    set_prefs(client, email="reader@example.com")
    assert len(calls) == 1  # same address, already verified -> nothing sent

    prefs = set_prefs(client, email="reader@example.com")
    assert prefs["email_verified"] is True
    assert prefs["email_pending"] is False


def test_clearing_email_clears_pending_verification(client):
    set_prefs(client, email="reader@example.com")
    cleared = set_prefs(client, email="   ")
    assert cleared["email"] is None
    assert cleared["email_pending"] is False
    assert cleared["email_verified"] is False


def test_digest_waits_for_verified_email(client, app_context, raw_client, enabled_mail, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com", weekly_digest=True)
    enable_library(client, "lapl")
    book = add_book(client)
    seed_availability(app_context, book["id"], library="lapl", available=True)

    import app as app_module

    # Unverified subscriber: digest is skipped (only the verification link
    # email itself has been sent so far).
    with app_context.app.app_context():
        sent = app_module._run_weekly_digest()
    assert sent == 0
    assert [to for to, _, _, _ in calls] == ["reader@example.com"]

    # Click the link -> the same setup now delivers.
    assert raw_client.get(f"/verify-email?token={_token_from_email(calls)}").status_code == 200
    with app_context.app.app_context():
        sent = app_module._run_weekly_digest()
    assert sent == 1
    assert [to for to, subject, _, _ in calls] == [
        "reader@example.com",
        "reader@example.com",
    ]
    assert calls[1][1] != "Verify your email for MyNextRead"


def hashlib_hex(token):
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()