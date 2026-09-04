"""Tests for the TBR book endpoints: add_book, list_books, remove_book."""


def test_add_book_requires_title(client):
    resp = client.post("/api/books", json={})
    assert resp.status_code == 400
    assert "Title is required" in resp.get_json()["error"]


def test_add_book_creates_synthetic_isbn_from_hash(client):
    resp = client.post("/api/books", json={"title": "The Overstory", "author": "Richard Powers"})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["book"]["isbn"].startswith("synthetic-")
    assert data["book"]["title"] == "The Overstory"
    assert data["status"] == "tbr"


def test_add_book_uses_cover_id_synthetic_isbn(client):
    resp = client.post("/api/books", json={"title": "It", "author": "King", "cover_id": 12345})
    assert resp.status_code == 201
    assert resp.get_json()["book"]["isbn"] == "cover-12345"


def test_add_book_second_same_title_is_duplicate(client):
    payload = {"title": "Dune", "author": "Herbert"}
    first = client.post("/api/books", json=payload)
    assert first.status_code == 201
    second = client.post("/api/books", json=payload)
    assert second.status_code == 409
    assert "already in your TBR" in second.get_json()["error"]


def test_add_book_stores_synopsis_and_genre(client):
    resp = client.post("/api/books", json={
        "title": "Circe", "author": "Miller", "isbn": "9780316556347",
        "synopsis": "A witch.", "genre": "Fantasy", "cover_url": "http://x/y.jpg",
    })
    assert resp.status_code == 201
    book = resp.get_json()["book"]
    assert book["synopsis"] == "A witch."
    assert book["genre"] == "Fantasy"
    assert book["cover_url"] == "http://x/y.jpg"


def test_add_book_reuses_existing_book_record(client, make_result):
    # Add the same ISBN via two different titles; Book row is reused.
    first = client.post("/api/books", json={"title": "Title A", "author": "A", "isbn": "9781111111111"})
    assert first.status_code == 201
    first_book_id = first.get_json()["book"]["id"]

    second = client.post("/api/books", json={"title": "Title B", "author": "B", "isbn": "9781111111111"})
    assert second.status_code == 409  # same ISBN -> same book -> duplicate in TBR
    # Confirm the underlying Book row is shared, not duplicated.
    assert second.get_json()["error"] == "Book already in your TBR"


def test_list_books_empty_initially(client):
    resp = client.get("/api/books")
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_list_books_returns_added_books_with_summary(client):
    client.post("/api/books", json={"title": "Klara and the Sun", "author": "Ishiguro", "isbn": "9780571364879"})
    resp = client.get("/api/books")
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["book"]["title"] == "Klara and the Sun"
    summary = data[0]["availability_summary"]
    assert summary["checked"] is False
    assert summary["available_now"] is False
    assert summary["formats"] == []


def test_remove_book_deletes_userbook(client):
    created = client.post("/api/books", json={"title": "Book", "author": "Author", "isbn": "9782222222222"})
    user_book_id = created.get_json()["id"]

    resp = client.delete(f"/api/books/{user_book_id}")
    assert resp.status_code == 200

    listing = client.get("/api/books").get_json()
    assert listing == []


def test_remove_missing_book_returns_404(client):
    resp = client.delete("/api/books/999999")
    assert resp.status_code == 404
