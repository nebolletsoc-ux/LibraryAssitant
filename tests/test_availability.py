"""Tests for availability checking: single check, check-all, and list summary."""


def add(client, title="The Overstory", author="Richard Powers", isbn="9780393635522"):
    resp = client.post("/api/books", json={"title": title, "author": author, "isbn": isbn})
    assert resp.status_code == 201
    return resp.get_json()


def test_check_availability_stores_and_returns_results(client, make_result, _mock_network):
    book = add(client)
    user_book_id = book["id"]

    _mock_network.append(make_result(
        library="berkeley", provider="Libby", format="eBook",
        available=True, wait=None, url="https://berkeley/media/abc",
    ))

    resp = client.post(f"/api/books/{user_book_id}/check")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["book"]["title"] == "The Overstory"
    assert len(data["availability"]) == 1
    result = data["availability"][0]
    assert result["library"] == "berkeley"
    assert result["provider"] == "Libby"
    assert result["format"] == "eBook"
    assert result["available"] is True


def test_list_summary_after_available_check(client, make_result, _mock_network):
    book = add(client)
    user_book_id = book["id"]
    _mock_network.append(make_result(format="eBook", available=True))
    client.post(f"/api/books/{user_book_id}/check")

    listing = client.get("/api/books").get_json()
    entry = listing[0]
    summary = entry["availability_summary"]
    assert summary["checked"] is True
    assert summary["available_now"] is True
    assert summary["formats"] == [{"format": "eBook", "available": True, "wait_text": None}]

    # The design's data model embeds full cached rows in the list response.
    assert len(entry["availability"]) == 1
    stored = entry["availability"][0]
    assert stored["library"] == "berkeley"
    assert stored["provider"] == "Libby"
    assert stored["format"] == "eBook"
    assert stored["available"] is True
    assert stored["url"] == "https://example.com/media/1"


def test_list_summary_after_waitlist_check(client, make_result, _mock_network):
    book = add(client)
    user_book_id = book["id"]
    _mock_network.append(make_result(format="Audiobook", available=False, wait="2-week wait"))
    client.post(f"/api/books/{user_book_id}/check")

    listing = client.get("/api/books").get_json()
    summary = listing[0]["availability_summary"]
    assert summary["checked"] is True
    assert summary["available_now"] is False
    assert summary["formats"] == [{"format": "Audiobook", "available": False, "wait_text": "2-week wait"}]


def test_list_summary_deduplicates_by_format_keeping_available(client, make_result, _mock_network):
    # Two availability rows for the same format but different libraries; the
    # first available one wins in the per-format summary.
    book = add(client)
    user_book_id = book["id"]
    _mock_network.extend([
        make_result(library="berkeley", format="eBook", available=False, wait="2-week wait"),
        make_result(library="hoopla", format="eBook", available=True),
    ])
    client.post(f"/api/books/{user_book_id}/check")

    listing = client.get("/api/books").get_json()
    formats = listing[0]["availability_summary"]["formats"]
    assert len(formats) == 1
    assert formats[0]["available"] is True


def test_check_missing_book_returns_404(client):
    resp = client.post("/api/books/999999/check")
    assert resp.status_code == 404


def enable_default_library(client):
    libs = client.get("/api/libraries").get_json()
    target = next(l for l in libs if l["library_key"] == "lapl")
    resp = client.patch(f"/api/libraries/{target['id']}", json={"enabled": True})
    assert resp.status_code == 200


def test_check_all_refreshes_every_tbr_entry(client, make_result, _mock_network):
    enable_default_library(client)
    book1 = add(client, title="A", isbn="9781111111111")
    book2 = add(client, title="B", isbn="9782222222222")

    _mock_network.append(make_result(library="berkeley", format="eBook", available=True))

    resp = client.post("/api/books/check-all")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 2
    assert data["checked"] == 2
    assert data["failures"] == []

    for entry in client.get("/api/books").get_json():
        assert entry["availability_summary"]["checked"] is True


def test_check_all_returns_error_when_no_libraries(client):
    # All default configs are seeded with enabled=False.
    resp = client.post("/api/books/check-all")
    assert resp.status_code == 400
    assert "No libraries" in resp.get_json()["error"]


# ---------- clear entire TBR list (DELETE /api/books/clear) ----------

def test_clear_tbr_requires_confirmation(client):
    resp = client.delete("/api/books/clear")
    assert resp.status_code == 400
    assert "Confirmation" in resp.get_json()["error"]


def test_clear_tbr_removes_every_entry(client, app_context):
    add(client, title="A", isbn="9781111111111")
    add(client, title="B", isbn="9782222222222")

    resp = client.delete("/api/books/clear", json={"confirm": "clear_all"})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 2
    assert client.get("/api/books").get_json() == []

    from models import Book
    with app_context.app.app_context():
        # Book records are retained (only the TBR rows are removed).
        assert Book.query.count() == 2


def test_clear_tbr_on_empty_list(client):
    resp = client.delete("/api/books/clear", json={"confirm": "clear_all"})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 0


def test_check_all_tracks_failures(client, make_result, _mock_network, monkeypatch):
    enable_default_library(client)
    book1 = add(client, title="A", isbn="9781111111111")
    book2 = add(client, title="B", isbn="9782222222222")

    _mock_network.append(make_result(format="eBook", available=True))

    # Force a failure on the second book.
    original_refresh = None
    import app as app_module

    calls = {"n": 0}

    def flaky_refresh(user_book, configs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return original_refresh(user_book, configs)

    from app import _refresh_availability as real_refresh
    original_refresh = real_refresh
    monkeypatch.setattr(app_module, "_refresh_availability", flaky_refresh)

    resp = client.post("/api/books/check-all")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 2
    assert data["checked"] == 1
    assert len(data["failures"]) == 1
    assert data["failures"][0]["title"] == "B"
