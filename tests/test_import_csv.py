"""Tests for CSV import (/api/books/import-csv) and the load_books helper."""

import csv
import io

import pytest


def storygraph_csv(rows):
    headers = ["Title", "Authors", "Read Status", "ISBN/UID", "Tags"]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return io.BytesIO(out.getvalue().encode("utf-8"))


def goodreads_csv(rows):
    headers = ["Book Id", "Title", "Author", "ISBN", "ISBN13", "Exclusive Shelf"]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return io.BytesIO(out.getvalue().encode("utf-8"))


def upload(client, fileobj, filename="books.csv"):
    return client.post(
        "/api/books/import-csv",
        data={"books_file": (fileobj, filename)},
        content_type="multipart/form-data",
    )


# --- load_books / validation helpers (imported directly from app.py) ---

def test_load_storygraph_to_read_only(app_context):
    from app import load_books

    csv_file = storygraph_csv([
        ['The Overstory', 'Powers, Richard', 'to-read', '9780393635522', 'Fiction'],
        ['Old Book', 'Nobody', 'read', '9780000000001', 'Nonfiction'],
    ])
    books = load_books(csv_file)
    assert len(books) == 1
    assert books[0]["title"] == "The Overstory"
    assert books[0]["isbn"] == "9780393635522"
    assert books[0]["genre"] == "Fiction"
    assert books[0]["author"] == "Powers, Richard"


def test_load_goodreads_strips_isbn_cell_wrapper(app_context):
    from app import load_books

    csv_file = goodreads_csv([
        ['1', 'Dune', 'Frank Herbert', '="9780262384254"', '="9780262384254"', 'to-read'],
    ])
    books = load_books(csv_file)
    assert len(books) == 1
    assert books[0]["isbn"] == "9780262384254"


def test_load_goodreads_rejects_non_toread(app_context):
    from app import load_books

    csv_file = goodreads_csv([
        ['1', 'Dune', 'Frank Herbert', '', '', 'read'],
    ])
    with pytest.raises(ValueError, match="No 'to-read' books"):
        load_books(csv_file)


def test_validate_csv_structure_accepts_storygraph(app_context):
    from app import validate_csv_structure

    valid, err = validate_csv_structure(["Title", "Authors", "Read Status", "ISBN/UID"])
    assert valid is True and err is None


def test_validate_csv_structure_accepts_goodreads(app_context):
    from app import validate_csv_structure

    valid, err = validate_csv_structure(["Title", "Author", "Exclusive Shelf"])
    assert valid is True and err is None


def test_validate_csv_structure_rejects_unknown(app_context):
    from app import validate_csv_structure

    valid, err = validate_csv_structure(["Foo", "Bar"])
    assert valid is False
    assert "Unrecognized CSV format" in err


def test_validate_csv_structure_rejects_empty(app_context):
    from app import validate_csv_structure

    valid, err = validate_csv_structure([])
    assert valid is False
    assert "empty" in err


# --- /api/books/import-csv endpoint ---

def test_import_requires_file(client):
    resp = client.post("/api/books/import-csv", data={})
    assert resp.status_code == 400


def test_import_rejects_non_csv_extension(client):
    resp = upload(client, io.BytesIO(b"Title,Author\nx,y\n"), filename="books.txt")
    assert resp.status_code == 400
    assert "CSV" in resp.get_json()["error"]


def test_import_storygraph_adds_to_read(client):
    resp = upload(client, storygraph_csv([
        ['The Overstory', 'Powers, Richard', 'to-read', '9780393635522', 'Fiction'],
    ]))
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["total"] == 1 and data["added"] == 1 and data["skipped"] == 0

    books = client.get("/api/books").get_json()
    assert books[0]["book"]["title"] == "The Overstory"


def test_import_goodreads_adds_to_read(client):
    resp = upload(client, goodreads_csv([
        ['1', 'Dune', 'Frank Herbert', '="9780262384254"', '="9780262384254"', 'to-read'],
    ]))
    assert resp.status_code == 201
    assert resp.get_json()["added"] == 1


def test_import_skips_duplicates_by_isbn(client):
    first = upload(client, goodreads_csv([
        ['1', 'Dune', 'Frank Herbert', '="9780262384254"', '="9780262384254"', 'to-read'],
    ]))
    assert first.status_code == 201

    second = upload(client, goodreads_csv([
        ['2', 'Dune', 'Frank Herbert', '="9780262384254"', '="9780262384254"', 'to-read'],
    ]))
    assert second.status_code == 201
    data = second.get_json()
    assert data["added"] == 0 and data["skipped"] == 1

    assert len(client.get("/api/books").get_json()) == 1


def test_import_adds_multiple_distinct_books(client):
    resp = upload(client, goodreads_csv([
        ['1', 'Dune', 'Herbert', '="9780262384254"', '="9780262384254"', 'to-read'],
        ['2', 'It', 'King', '', '', 'to-read'],
    ]))
    assert resp.get_json()["added"] == 2
    assert len(client.get("/api/books").get_json()) == 2


def test_import_no_to_read_returns_error(client):
    resp = upload(client, goodreads_csv([
        ['1', 'Dune', 'Herbert', '', '', 'read'],
    ]))
    assert resp.status_code == 400
