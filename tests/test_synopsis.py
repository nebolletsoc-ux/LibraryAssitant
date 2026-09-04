"""Tests for the on-demand synopsis endpoint (/api/books/<user_book_id>/synopsis).

Network calls to openlibrary.org are mocked so these run offline and
deterministically.
"""


def fake_response(json_payload=None, status_code=200, error=None):
    class _R:
        def __init__(self):
            self.status_code = status_code
            self.error = error

        def raise_for_status(self):
            if self.error:
                raise self.error
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            if json_payload is None:
                raise ValueError("no json")
            return json_payload

    return _R()


def _add_book(client, add_book, title="The Overstory", author="Richard Powers"):
    resp = add_book({"title": title, "author": author})
    assert resp.status_code in (200, 201)
    data = resp.get_json()
    return data["id"], data["book"]["id"]


def test_synopsis_fetches_from_work_and_caches(client, add_book, monkeypatch):
    user_book_id, _ = _add_book(client, add_book)

    def fake_get(url, **kwargs):
        if "/isbn/" in url:
            return fake_response({"works": [{"key": "/works/OL1W"}], "subjects": ["Fiction"]})
        if url.endswith("/works/OL1W.json"):
            return fake_response({"description": {"type": "/type/text", "value": "A sweeping novel."},
                                  "subjects": ["Fiction"]})
        return fake_response({})

    monkeypatch.setattr("app.requests.get", fake_get)

    resp = client.post(f"/api/books/{user_book_id}/synopsis")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["synopsis"] == "A sweeping novel."
    assert data["genre"] == "Fiction"

    # Second call must not touch the network again (synopsis is cached).
    def _boom(*args, **kwargs):
        raise AssertionError("network should not be hit for a cached synopsis")

    monkeypatch.setattr("app.requests.get", _boom)
    resp2 = client.post(f"/api/books/{user_book_id}/synopsis")
    assert resp2.get_json()["synopsis"] == "A sweeping novel."


def test_synopsis_synthetic_isbn_falls_back_to_title_search(client, add_book, monkeypatch):
    user_book_id, _ = _add_book(client, add_book)

    def fake_get(url, **kwargs):
        if "/isbn/" in url:
            return fake_response({"works": [], "subjects": ["Fiction"]})
        if "/search.json" in url:
            return fake_response({"docs": [{"title": "The Overstory",
                                            "seed": ["/works/OL2W"]}]})
        if url.endswith("/works/OL2W.json"):
            return fake_response({"description": "A story about trees."})
        return fake_response({})

    monkeypatch.setattr("app.requests.get", fake_get)

    resp = client.post(f"/api/books/{user_book_id}/synopsis")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["synopsis"] == "A story about trees."
    assert data["genre"] == "Fiction"


def test_synopsis_returns_none_when_no_description(client, add_book, monkeypatch):
    user_book_id, _ = _add_book(client, add_book)

    def fake_get(url, **kwargs):
        if "/isbn/" in url:
            return fake_response({"works": [], "subjects": ["Fiction"]})
        if "/search.json" in url:
            return fake_response({"docs": [{"seed": ["/works/OL3W"]}]})
        if url.endswith("/works/OL3W.json"):
            return fake_response({"subjects": ["Fiction"]})
        return fake_response({})

    monkeypatch.setattr("app.requests.get", fake_get)

    resp = client.post(f"/api/books/{user_book_id}/synopsis")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["synopsis"] is None
    assert data["genre"] == "Fiction"


def test_synopsis_tolerates_network_failure(client, add_book, monkeypatch):
    user_book_id, _ = _add_book(client, add_book)

    def fake_get(url, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("app.requests.get", fake_get)

    resp = client.post(f"/api/books/{user_book_id}/synopsis")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["synopsis"] is None


def test_synopsis_unknown_user_book_returns_404(client):
    resp = client.post("/api/books/999999/synopsis")
    assert resp.status_code == 404