"""Tests for the Open Library-backed search endpoint (/api/books/search).

Network calls to openlibrary.org are mocked so these run offline and
deterministically.
"""


def fake_response(json_payload=None, status_code=200):
    class _R:
        def raise_for_status(self):
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")

        def json(self):
            if json_payload is None:
                raise ValueError("no json")
            return json_payload

    r = _R()
    r.status_code = status_code
    return r


def test_search_requires_title(client, monkeypatch):
    resp = client.post("/api/books/search", json={})
    assert resp.status_code == 400
    assert "Title is required" in resp.get_json()["error"]


def test_search_returns_results(client, monkeypatch):
    payload = {"docs": [
        {"title": "The Overstory", "author_name": ["Richard Powers"], "isbn": ["9780393635522"], "cover_id": 7},
        {"title": "Overstory, The", "author_name": ["R. Powers"], "isbn": []},
    ]}

    def fake_get(url, **kwargs):
        return fake_response(json_payload=payload)

    monkeypatch.setattr("app.requests.get", fake_get)

    resp = client.post("/api/books/search", json={"title": "The Overstory", "author": "Richard Powers"})
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert results[0]["title"] == "The Overstory"
    assert results[0]["isbn"] == "9780393635522"
    assert results[0]["author"] == "Richard Powers"
    assert results[0]["year"] is None


def test_search_defaults_unknown_author(client, monkeypatch):
    payload = {"docs": [{"title": "No Author"}]}

    def fake_get(url, **kwargs):
        return fake_response(json_payload=payload)

    monkeypatch.setattr("app.requests.get", fake_get)

    resp = client.post("/api/books/search", json={"title": "No Author"})
    assert resp.get_json()["results"][0]["author"] == "Unknown"


def test_search_handles_invalid_json_as_503(client, monkeypatch):
    def fake_get(url, **kwargs):
        # json_payload=None -> .json() raises ValueError, as if the body
        # isn't valid JSON.
        return fake_response(json_payload=None)

    monkeypatch.setattr("app.requests.get", fake_get)

    resp = client.post("/api/books/search", json={"title": "X"})
    assert resp.status_code == 503
    assert "temporarily unavailable" in resp.get_json()["error"]


def test_search_handles_http_error_as_503(client, monkeypatch):
    from requests import RequestException

    def fake_get(url, **kwargs):
        class _R:
            def raise_for_status(self):
                raise RequestException("HTTP 503")
        r = _R()
        r.status_code = 503
        return r

    monkeypatch.setattr("app.requests.get", fake_get)

    resp = client.post("/api/books/search", json={"title": "X"})
    assert resp.status_code == 503


def test_search_limits_and_truncates_results(client, monkeypatch):
    docs = [{"title": f"Book {i}", "author_name": ["Author"], "isbn": [str(i)]} for i in range(20)]

    def fake_get(url, **kwargs):
        return fake_response(json_payload={"docs": docs})

    monkeypatch.setattr("app.requests.get", fake_get)

    resp = client.post("/api/books/search", json={"title": "Book"})
    results = resp.get_json()["results"]
    assert len(results) == 10
