"""Tests for language capture, English-only filtering, and free-source searches.

Covers:
- Language extraction from Libby / Bibliocommons / Hoopla parser paths.
- The ``only_english`` user preference and its effect on list_books / get_availability.
- Gutenberg and OpenLibrary search functions with mocked HTTP responses.
- Preset sub-labels for the free sources.
"""

import json
import re

from library.oakland import (
    _extract_bibliocommons_results,
    _gutenberg_language,
    _libby_languages,
    search_gutenberg,
    search_openlibrary,
)
from app import _language_allowed


# ---------------------------------------------------------------------------
# Libby language parsing
# ---------------------------------------------------------------------------

def test_libby_languages_extracts_names():
    item = {"languages": [{"id": "en", "name": "English"}, {"id": "es", "name": "Spanish"}]}
    assert _libby_languages(item) == "English, Spanish"


def test_libby_languages_single_language():
    item = {"languages": [{"id": "en", "name": "English"}]}
    assert _libby_languages(item) == "English"


def test_libby_languages_missing_key():
    assert _libby_languages({}) is None
    assert _libby_languages(None) is None


# ---------------------------------------------------------------------------
# Bibliocommons language extraction (direct parser call)
# ---------------------------------------------------------------------------

_BIBLIO_BLOCK_LANG = """
<li class="row cp-search-result-item">
  <div class="title-content">The Silent Patient</div>
  <div class="manifestation-item cp-manifestation-list-item">
    <a href="/v2/record/ABC123">link</a>
    <div class="display-info-primary">Book, 2023.</div>
    <div class="cp-screen-reader-message">Book, 2023. Language: Spanish. Call number: SP FIC</div>
    <div class="manifestation-item-availability-block-wrap"><div>Available</div></div>
    <div class="manifestation-item-format-call-wrap available">available</div>
  </div>
  <div class="manifestation-item cp-manifestation-list-item">
    <a href="/v2/record/ABC456">link</a>
    <div class="display-info-primary">Book, 2023.</div>
    <div class="cp-screen-reader-message">Book, 2023. Language: English. Call number: FIC</div>
    <div class="manifestation-item-availability-block-wrap"><div>Unavailable</div></div>
    <div class="manifestation-item-format-call-wrap unavailable">unavailable</div>
  </div>
</li>
"""


def test_bibliocommons_extracts_language_per_manifestation():
    results = _extract_bibliocommons_results(
        _BIBLIO_BLOCK_LANG,
        "oaklandlibrary",
        "oakland",
        "The Silent Patient",
        "Alex Michaelides",
    )
    langs = {r.url: r.language for r in results}
    assert langs["https://oaklandlibrary.bibliocommons.com/v2/record/ABC123"] == "Spanish"
    assert langs["https://oaklandlibrary.bibliocommons.com/v2/record/ABC456"] == "English"


_BIBLIO_BLOCK_NO_LANG = """
<li class="row cp-search-result-item">
  <div class="title-content">Mystery Book</div>
  <div class="manifestation-item cp-manifestation-list-item">
    <a href="/v2/record/XYZ789">link</a>
    <div class="display-info-primary">Book, 2022.</div>
    <div class="manifestation-item-availability-block-wrap"><div>Available</div></div>
    <div class="manifestation-item-format-call-wrap available">available</div>
  </div>
</li>
"""


def test_bibliocommons_returns_none_when_language_missing():
    results = _extract_bibliocommons_results(
        _BIBLIO_BLOCK_NO_LANG,
        "oaklandlibrary",
        "oakland",
        "Mystery Book",
        "Jane Author",
    )
    assert len(results) == 1
    assert results[0].language is None


# ---------------------------------------------------------------------------
# _language_allowed helper
# ---------------------------------------------------------------------------

class _FakeUser:
    def __init__(self, only_english=False):
        self.only_english = only_english


class _Row:
    def __init__(self, language=None):
        self.language = language


def test_language_allowed_returns_true_when_pref_off():
    assert _language_allowed(None, _Row("Spanish")) is True
    assert _language_allowed(_FakeUser(False), _Row("Spanish")) is True


def test_language_allowed_keeps_unknown_language():
    assert _language_allowed(_FakeUser(True), _Row(None)) is True
    assert _language_allowed(_FakeUser(True), _Row("")) is True


def test_language_allowed_keeps_english():
    assert _language_allowed(_FakeUser(True), _Row("English")) is True
    assert _language_allowed(_FakeUser(True), _Row("English, French")) is True
    assert _language_allowed(_FakeUser(True), _Row("eng")) is True


def test_language_allowed_hides_non_english():
    assert _language_allowed(_FakeUser(True), _Row("Spanish")) is False
    assert _language_allowed(_FakeUser(True), _Row("Chinese")) is False
    assert _language_allowed(_FakeUser(True), _Row("French, Korean")) is False


# ---------------------------------------------------------------------------
# only_english preference round-trip and list_books filtering
# ---------------------------------------------------------------------------

def test_only_english_preference_persists(client):
    resp = client.patch("/api/user/preferences", json={"only_english": True})
    assert resp.status_code == 200
    assert resp.get_json()["only_english"] is True

    resp = client.get("/api/user/preferences")
    assert resp.get_json()["only_english"] is True

    resp = client.patch("/api/user/preferences", json={"only_english": False})
    assert resp.get_json()["only_english"] is False


def test_list_books_hides_non_english_rows_when_pref_on(client, make_result, _mock_network, app_context):
    # Add a book and store availability rows directly.
    resp = client.post("/api/books", json={"title": "Test Book", "author": "Author"})
    assert resp.status_code == 201
    ub_id = resp.get_json()["id"]

    from models import Book, Availability, UserBook, User, db

    with app_context.app.app_context():
        ub = UserBook.query.get(ub_id)
        book_id = ub.book_id
        db.session.add(Availability(
            book_id=book_id,
            library="oakland",
            provider="Catalog",
            format="eBook",
            available=True,
            url="https://example.com/spanish",
            language="Spanish",
        ))
        db.session.add(Availability(
            book_id=book_id,
            library="berkeley",
            provider="Libby",
            format="eBook",
            available=True,
            url="https://example.com/english",
            language="English",
        ))
        db.session.commit()

    # Default: both rows visible.
    rows = client.get("/api/books").get_json()[0]["availability"]
    assert {r["library"] for r in rows} == {"oakland", "berkeley"}

    # Enable only_english.
    client.patch("/api/user/preferences", json={"only_english": True})
    rows = client.get("/api/books").get_json()[0]["availability"]
    assert len(rows) == 1
    assert rows[0]["library"] == "berkeley"
    assert rows[0]["language"] == "English"

    # get_availability also filters.
    resp = client.get(f"/api/books/{ub_id}/availability").get_json()
    assert len(resp["availability"]) == 1
    assert resp["availability"][0]["library"] == "berkeley"

    # Disable only_english → Spanish returns.
    client.patch("/api/user/preferences", json={"only_english": False})
    rows = client.get("/api/books").get_json()[0]["availability"]
    assert {r["library"] for r in rows} == {"oakland", "berkeley"}


# ---------------------------------------------------------------------------
# Gutenberg search (mocked)
# ---------------------------------------------------------------------------

_GUTENBERG_HTML = """
<html><body>
<ol>
  <li class="booklink">
    <a href="/ebooks/1234"><img src="cover.jpg"></a>
    <span class="title"><a href="/ebooks/1234">The Picture of Dorian Gray</a></span>
    <span class="author">by Oscar Wilde</span>
  </li>
  <li class="booklink">
    <a href="/ebooks/5678"><img src="cover2.jpg"></a>
    <span class="title"><a href="/ebooks/5678">The Secret Garden</a></span>
    <span class="author">by Frances Hodgson Burnett</span>
  </li>
</ol>
</body></html>
"""


def test_search_gutenberg_matches_and_returns_free_rows(monkeypatch):
    class _FakeResp:
        status_code = 200
        text = _GUTENBERG_HTML

    monkeypatch.setattr("library.oakland.requests.get", lambda *a, **kw: _FakeResp())
    results = search_gutenberg("gutenberg", "The Picture of Dorian Gray", "Oscar Wilde")
    assert len(results) == 1
    r = results[0]
    assert r.url == "https://www.gutenberg.org/ebooks/1234"
    assert r.provider == "Gutenberg"
    assert r.format == "eBook"
    assert r.available is True
    assert r.language == "English"


def test_search_gutenberg_skips_non_matches(monkeypatch):
    class _FakeResp:
        status_code = 200
        text = _GUTENBERG_HTML

    monkeypatch.setattr("library.oakland.requests.get", lambda *a, **kw: _FakeResp())
    results = search_gutenberg("gutenberg", "Dune", "Frank Herbert")
    assert results == []


def test_gutenberg_language_detects_translation():
    assert _gutenberg_language("Frankenstein Volume 1 (of 3) (French)") == "French"
    assert _gutenberg_language("Der Kleine Prinz (German)") == "German"
    assert _gutenberg_language("Frankenstein; or, the modern prometheus") == "English"


# ---------------------------------------------------------------------------
# OpenLibrary search (mocked)
# ---------------------------------------------------------------------------

_OL_JSON = {
    "docs": [
        {
            "key": "/works/OL123W",
            "title": "The Silent Patient",
            "author_name": ["Alex Michaelides"],
            "access": "public",
        },
        {
            "key": "/works/OL456W",
            "title": "The Silent Patient",
            "author_name": ["Alex Michaelides"],
            "access": "borrowable",
        },
        {
            "key": "/works/OL789W",
            "title": "The Silent Patient",
            "author_name": ["Alex Michaelides"],
            "ebook_access": "yes",
        },
    ]
}


def test_search_openlibrary_returns_only_free_editions(monkeypatch):
    class _FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return _OL_JSON

    monkeypatch.setattr("library.oakland.requests.get", lambda *a, **kw: _FakeResp())
    results = search_openlibrary("openlibrary", "The Silent Patient", "Alex Michaelides")
    # "public" (access) and "yes" (ebook_access) count as free; "borrowable" does not.
    urls = sorted(r.url for r in results)
    assert urls == [
        "https://openlibrary.org/works/OL123W",
        "https://openlibrary.org/works/OL789W",
    ]
    r = results[0]
    assert r.provider == "OpenLibrary"
    assert r.format == "eBook"
    assert r.available is True
    assert r.language == "English"


def test_search_openlibrary_skips_no_match(monkeypatch):
    class _FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {"docs": []}

    monkeypatch.setattr("library.oakland.requests.get", lambda *a, **kw: _FakeResp())
    results = search_openlibrary("openlibrary", "Nonexistent Book", "Nobody")
    assert results == []


# ---------------------------------------------------------------------------
# Preset sub-labels
# ---------------------------------------------------------------------------

def test_available_libraries_shows_free_sub_label(client, current_user_id, app_context):
    from models import LibraryConfig, db

    with app_context.app.app_context():
        LibraryConfig.query.filter_by(user_id=current_user_id, library_key="openlibrary").delete()
        db.session.commit()

    available = {
        item["library_key"]: item["sub"]
        for item in client.get("/api/libraries/available").get_json()
    }
    assert available["openlibrary"] == "Free ebooks"
