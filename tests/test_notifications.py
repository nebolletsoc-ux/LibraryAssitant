"""Tests for email notifications: preferences + availability alerts + digest."""

import pytest


def add_book(client, title="The Overstory", author="Richard Powers", isbn="9780393635522"):
    resp = client.post("/api/books", json={"title": title, "author": author, "isbn": isbn})
    assert resp.status_code == 201
    return resp.get_json()


def set_prefs(client, **kwargs):
    resp = client.patch("/api/user/preferences", json=kwargs)
    assert resp.status_code == 200
    return resp.get_json()


def capture_emails(monkeypatch):
    import mailer

    calls = []
    monkeypatch.setattr(mailer, "submit_email",
                        lambda to, subject, text="", html=None: calls.append((to, subject, text)))
    return calls


# ---------- preferences endpoints ----------

def test_preferences_defaults(client):
    prefs = client.get("/api/user/preferences").get_json()
    assert prefs["email"] is None
    assert prefs["notify_on_available"] is True
    assert prefs["weekly_digest"] is False
    assert prefs["email_enabled"] is False  # no RESEND_API_KEY or SMTP_HOST in tests


def test_update_preferences(client):
    updated = set_prefs(client,
                        email="reader@example.com",
                        notify_on_available=False,
                        weekly_digest=True)
    assert updated["email"] == "reader@example.com"
    assert updated["notify_on_available"] is False
    assert updated["weekly_digest"] is True

    again = client.get("/api/user/preferences").get_json()
    assert again["email"] == "reader@example.com"
    assert again["weekly_digest"] is True


def test_update_preferences_blank_email_clears(client):
    set_prefs(client, email="reader@example.com", weekly_digest=True)
    cleared = set_prefs(client, email="   ")
    assert cleared["email"] is None


def test_update_preferences_ignores_non_boolean_toggles(client):
    set_prefs(client, email="reader@example.com", notify_on_available=False)
    resp = client.patch("/api/user/preferences",
                        json={"notify_on_available": "nope", "weekly_digest": 1})
    assert resp.status_code == 200
    prefs = resp.get_json()
    assert prefs["notify_on_available"] is False
    assert prefs["weekly_digest"] is False


def test_preferences_require_auth(raw_client):
    raw_client.set_cookie("csrf_token", "x")
    assert raw_client.get("/api/user/preferences").status_code == 401
    assert raw_client.patch("/api/user/preferences", json={"email": "a@b.c"},
                            headers={"X-CSRF-Token": "x"}).status_code == 401


def test_email_enabled_reflects_resend_key(client, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_123")
    assert client.get("/api/user/preferences").get_json()["email_enabled"] is True


def test_send_email_via_resend(monkeypatch):
    import mailer

    monkeypatch.setenv("RESEND_API_KEY", "re_test_123")
    captured = {}

    class FakeResp:
        status_code = 200
        content = b'{"id":"abc"}'

        def json(self):
            return {"id": "abc"}

    monkeypatch.setattr(mailer.requests, "post",
                        lambda url, **kw: (captured.update({"url": url, "kw": kw}), FakeResp())[1])

    ok, detail = mailer.send_email_report("reader@example.com", "Subj", text="Body")
    assert ok is True
    assert captured["kw"]["headers"]["Authorization"] == "Bearer re_test_123"
    payload = captured["kw"]["json"]
    assert payload["to"] == ["reader@example.com"]
    assert payload["subject"] == "Subj"
    assert payload["text"] == "Body"
    assert payload["from"] == "onboarding@resend.dev"


def test_send_email_via_resend_reports_api_error(monkeypatch):
    import mailer

    monkeypatch.setenv("RESEND_API_KEY", "re_test_123")

    class FakeResp:
        status_code = 401
        content = b'{"message":"missing api key"}'

        def json(self):
            return {"message": "missing api key"}

    monkeypatch.setattr(mailer.requests, "post", lambda url, **kw: FakeResp())

    ok, detail = mailer.send_email_report("reader@example.com", "Subj", text="Body")
    assert ok is False
    assert "401" in detail


# ---------- send test email ----------

def test_send_test_email(client, monkeypatch):
    import mailer

    sent = []
    monkeypatch.setattr(mailer, "is_enabled", lambda: True)
    monkeypatch.setattr(
        mailer, "send_email_report",
        lambda to, subject, html=None, text="": (sent.append((to, subject, text)), ("ok", True))[-1],
    )
    set_prefs(client, email="reader@example.com")

    resp = client.post("/api/user/preferences/send-test")
    assert resp.status_code == 200
    assert resp.get_json()["sent"] is True
    assert sent[0][0] == "reader@example.com"
    assert "test email" in sent[0][1].lower()


def test_send_test_email_reports_failure(client, monkeypatch):
    import mailer

    monkeypatch.setattr(mailer, "is_enabled", lambda: True)
    monkeypatch.setattr(mailer, "send_email_report",
                        lambda to, subject, html=None, text="": (False, "530 auth failed"))
    set_prefs(client, email="reader@example.com")

    resp = client.post("/api/user/preferences/send-test")
    assert resp.status_code == 502
    assert "530 auth failed" in resp.get_json()["error"]


def test_send_test_email_requires_address(client, monkeypatch):
    import mailer

    monkeypatch.setattr(mailer, "is_enabled", lambda: True)
    resp = client.post("/api/user/preferences/send-test")
    assert resp.status_code == 400
    assert "email" in resp.get_json()["error"].lower()


def test_send_test_email_requires_smtp(client, monkeypatch):
    import mailer

    monkeypatch.setattr(mailer, "is_enabled", lambda: False)
    set_prefs(client, email="reader@example.com")
    resp = client.post("/api/user/preferences/send-test")
    assert resp.status_code == 400
    assert "Email isn't configured" in resp.get_json()["error"]


def test_send_test_email_requires_auth(raw_client):
    raw_client.set_cookie("csrf_token", "x")
    assert raw_client.post("/api/user/preferences/send-test",
                           headers={"X-CSRF-Token": "x"}).status_code == 401


# ---------- availability alerts ----------

def test_alert_when_book_becomes_available(client, make_result, _mock_network, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com")
    book = add_book(client)
    book_id = book["id"]

    # First scan: known waitlist. No alert for a waitlist state.
    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=False, wait="2-week wait"))
    assert client.post(f"/api/books/{book_id}/check").status_code == 200
    assert calls == []

    # Second scan: the same format flips to available. Alert fires.
    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=True))
    assert client.post(f"/api/books/{book_id}/check").status_code == 200
    assert len(calls) == 1
    to, subject, text = calls[0]
    assert to == "reader@example.com"
    assert "The Overstory" in subject
    assert "The Overstory" in text


def test_no_alert_on_first_scan_even_if_available(client, make_result, _mock_network, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com")
    book = add_book(client)

    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=True))
    assert client.post(f"/api/books/{book['id']}/check").status_code == 200
    assert calls == []


def test_no_alert_when_available_stays_available(client, make_result, _mock_network, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com")
    book = add_book(client)

    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=True))
    client.post(f"/api/books/{book['id']}/check")
    client.post(f"/api/books/{book['id']}/check")
    assert calls == []


def test_no_alert_on_waitlist_to_waitlist(client, make_result, _mock_network, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com")
    book = add_book(client)

    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=False, wait="2-week wait"))
    client.post(f"/api/books/{book['id']}/check")
    client.post(f"/api/books/{book['id']}/check")
    assert calls == []


def test_no_alert_when_no_email_on_account(client, make_result, _mock_network, monkeypatch):
    calls = capture_emails(monkeypatch)
    book = add_book(client)
    book_id = book["id"]

    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=False, wait="2-week wait"))
    client.post(f"/api/books/{book_id}/check")

    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=True))
    client.post(f"/api/books/{book_id}/check")
    assert calls == []


def test_no_alert_when_alerts_disabled(client, make_result, _mock_network, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com", notify_on_available=False)
    book = add_book(client)
    book_id = book["id"]

    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=False, wait="2-week wait"))
    client.post(f"/api/books/{book_id}/check")

    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=True))
    client.post(f"/api/books/{book_id}/check")
    assert calls == []


def test_alert_goes_to_every_owner(client, make_result, _mock_network, monkeypatch, app_context):
    from werkzeug.security import generate_password_hash
    from models import User, UserBook, db

    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com")

    book = add_book(client)

    with app_context.app.app_context():
        other = User(
            username="other-reader",
            password_hash=generate_password_hash("whatever"),
            email="other@example.com",
            notify_on_available=True,
        )
        db.session.add(other)
        db.session.flush()
        db.session.add(UserBook(user_id=other.id, book_id=book["id"], status="tbr"))
        db.session.commit()

    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=False, wait="2-week wait"))
    client.post(f"/api/books/{book['id']}/check")

    _mock_network.clear()
    _mock_network.append(make_result(format="eBook", available=True))
    client.post(f"/api/books/{book['id']}/check")

    assert sorted(call[0] for call in calls) == ["other@example.com", "reader@example.com"]


# ---------- weekly digest ----------

def enable_library(client, library_key):
    libs = client.get("/api/libraries").get_json()
    target = next(lib for lib in libs if lib["library_key"] == library_key)
    resp = client.patch(f"/api/libraries/{target['id']}", json={"enabled": True})
    assert resp.status_code == 200
    return target


def seed_availability(app_context, book_id, library="lapl", available=True, format_="eBook"):
    from models import Availability, db

    with app_context.app.app_context():
        db.session.add(Availability(
            book_id=book_id,
            library=library,
            provider="Libby",
            format=format_,
            available=available,
            wait_text=None,
            holds=None,
            wait_weeks=None,
            url=f"https://example.com/{library}/{book_id}",
        ))
        db.session.commit()


def test_digest_emails_available_books(client, app_context, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com", weekly_digest=True)
    enable_library(client, "lapl")
    book = add_book(client)
    seed_availability(app_context, book["id"], library="lapl", available=True)

    import app as app_module
    with app_context.app.app_context():
        sent = app_module._run_weekly_digest()

    assert sent == 1
    assert len(calls) == 1
    assert calls[0][0] == "reader@example.com"
    assert "The Overstory" in calls[0][2]  # digest titles live in the body text


def test_digest_skips_books_without_availability(client, app_context, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com", weekly_digest=True)
    enable_library(client, "lapl")
    add_book(client)

    import app as app_module
    with app_context.app.app_context():
        sent = app_module._run_weekly_digest()

    assert sent == 0
    assert calls == []


def test_digest_skips_unavailable_books(client, app_context, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com", weekly_digest=True)
    enable_library(client, "lapl")
    book = add_book(client)
    seed_availability(app_context, book["id"], library="lapl", available=False)

    import app as app_module
    with app_context.app.app_context():
        sent = app_module._run_weekly_digest()

    assert sent == 0
    assert calls == []


def test_digest_skips_when_weekly_toggle_off(client, app_context, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com", weekly_digest=False)
    enable_library(client, "lapl")
    book = add_book(client)
    seed_availability(app_context, book["id"], library="lapl", available=True)

    import app as app_module
    with app_context.app.app_context():
        sent = app_module._run_weekly_digest()

    assert sent == 0
    assert calls == []


def test_digest_respects_enabled_libraries(client, app_context, monkeypatch):
    calls = capture_emails(monkeypatch)
    set_prefs(client, email="reader@example.com", weekly_digest=True)
    enable_library(client, "lapl")
    book = add_book(client)
    # Row exists but only under a disabled library -> excluded from the digest.
    seed_availability(app_context, book["id"], library="berkeley", available=True)

    import app as app_module
    with app_context.app.app_context():
        sent = app_module._run_weekly_digest()

    assert sent == 0
    assert calls == []