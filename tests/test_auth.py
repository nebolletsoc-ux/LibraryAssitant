"""Tests for the multi-user auth layer: registration, login, sessions,
per-user data isolation, and CSRF protection on state-changing routes."""

import pytest


def test_registration_and_login_and_logout(raw_client):
    resp = raw_client.post("/api/auth/register", json={
        "username": "alice",
        "password": "secret1",
    })
    assert resp.status_code == 201
    assert resp.get_json()["user"]["username"] == "alice"

    resp = raw_client.get("/api/auth/me")
    assert resp.get_json()["user"]["username"] == "alice"

    resp = raw_client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert raw_client.get("/api/auth/me").status_code == 401

    resp = raw_client.post("/api/auth/login", json={
        "username": "alice",
        "password": "secret1",
    })
    assert resp.status_code == 200
    assert raw_client.get("/api/auth/me").status_code == 200


def test_register_requires_both_fields(raw_client):
    assert raw_client.post("/api/auth/register", json={"username": "a"}).status_code == 400
    assert raw_client.post("/api/auth/register", json={"password": "secret1"}).status_code == 400
    assert raw_client.post("/api/auth/register", json={}).status_code == 400


def test_register_rejects_short_password(raw_client):
    resp = raw_client.post("/api/auth/register", json={"username": "bob", "password": "short"})
    assert resp.status_code == 400


def test_register_rejects_duplicate_username(raw_client):
    payload = {"username": "carol", "password": "secret1"}
    assert raw_client.post("/api/auth/register", json=payload).status_code == 201
    resp = raw_client.post("/api/auth/register", json=payload)
    assert resp.status_code == 409


def test_login_wrong_password(raw_client):
    raw_client.post("/api/auth/register", json={"username": "dave", "password": "secret1"})
    resp = raw_client.post("/api/auth/login", json={"username": "dave", "password": "wrong!"})
    assert resp.status_code == 401


def test_login_unknown_user(raw_client):
    resp = raw_client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


def test_unauthenticated_api_is_rejected(raw_client):
    for method, path in (
        ("GET", "/api/books"),
        ("GET", "/api/libraries"),
        ("POST", "/api/books"),
        ("POST", "/api/libraries"),
    ):
        assert getattr(raw_client, method.lower())(path).status_code == 401


def test_healthz_stays_public(raw_client):
    assert raw_client.get("/healthz").status_code == 200


@pytest.mark.no_auth
def test_registration_adopts_legacy_rows_when_first_account_is_id_1(raw_client, app_context):
    """First account on a fresh DB gets id=1, which already owns the legacy rows."""
    from models import Book, LibraryConfig, UserBook, db

    with app_context.app.app_context():
        legacy_book = Book(title="Legacy Title", author="Legacy Author", isbn="legacy-isbn")
        db.session.add(legacy_book)
        db.session.flush()
        db.session.add(UserBook(user_id=1, book_id=legacy_book.id, status="tbr"))
        db.session.add(LibraryConfig(
            user_id=1, library_key="oakland", label="Oakland Public Library",
            bibliocommons="oaklandlibrary", enabled=True,
        ))
        db.session.commit()

    resp = raw_client.post("/api/auth/register", json={
        "username": "first",
        "password": "secret1",
    })
    assert resp.status_code == 201
    assert resp.get_json()["user"]["id"] == 1

    # The legacy rows are the first account's own from the start.
    assert len(raw_client.get("/api/books").get_json()) == 1
    assert raw_client.get("/api/books").get_json()[0]["book"]["title"] == "Legacy Title"
    keys = [lib["library_key"] for lib in raw_client.get("/api/libraries").get_json()]
    assert "oakland" in keys


@pytest.mark.no_auth
def test_adopt_legacy_user_rows_moves_to_non_one_id(app_context):
    """Legacy sentinel rows move to an account whose id isn't 1."""
    from app import _adopt_legacy_user_rows
    from models import Book, LibraryConfig, UserBook, db

    with app_context.app.app_context():
        legacy_book = Book(title="Legacy Title", author="Legacy Author", isbn="legacy-isbn")
        db.session.add(legacy_book)
        db.session.flush()
        db.session.add(UserBook(user_id=1, book_id=legacy_book.id, status="tbr"))
        db.session.add(LibraryConfig(
            user_id=1, library_key="oakland", label="Oakland Public Library",
            bibliocommons="oaklandlibrary", enabled=True,
        ))
        db.session.commit()

        _adopt_legacy_user_rows(77)

        assert UserBook.query.filter_by(user_id=1).count() == 0
        assert UserBook.query.filter_by(user_id=77).count() == 1
        assert LibraryConfig.query.filter_by(user_id=1).count() == 0
        assert LibraryConfig.query.filter_by(user_id=77).count() == 1


@pytest.mark.no_auth
def test_adopt_legacy_user_rows_noop_for_id_one(app_context):
    from app import _adopt_legacy_user_rows
    from models import UserBook, LibraryConfig, Book, db

    with app_context.app.app_context():
        b = Book(title="L", author="A", isbn="x")
        db.session.add(b)
        db.session.flush()
        db.session.add(UserBook(user_id=1, book_id=b.id, status="tbr"))
        db.session.commit()

        _adopt_legacy_user_rows(1)

        # id-1 account already owns the sentinel rows; nothing should change.
        assert UserBook.query.filter_by(user_id=1).count() == 1
        assert LibraryConfig.query.filter_by(user_id=1).count() == 0


@pytest.mark.no_auth
def test_second_user_gets_their_own_empty_list(client):
    def register(username):
        resp = client.post("/api/auth/register", json={"username": username, "password": "secret1"})
        assert resp.status_code == 201
        return resp.get_json()["user"]

    register("user_a")
    book_payload = {"title": "User A's Book", "author": "Them"}
    assert client.post("/api/books", json=book_payload).status_code == 201
    assert len(client.get("/api/books").get_json()) == 1

    register("user_b")
    assert client.get("/api/books").get_json() == []
    assert client.get("/api/libraries").get_json() == []


def test_csrf_blocks_mutation_without_token(client, app_context):
    libs = client.get("/api/libraries").get_json()
    lapl = next(l for l in libs if l["library_key"] == "lapl")

    # Simulate a client that never received the csrf cookie.
    client.csrf_token = None
    resp = client.patch(f"/api/libraries/{lapl['id']}", json={"enabled": True})
    assert resp.status_code == 403


def test_state_change_with_token_succeeds(client):
    libs = client.get("/api/libraries").get_json()
    lapl = next(l for l in libs if l["library_key"] == "lapl")
    resp = client.patch(f"/api/libraries/{lapl['id']}", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is True