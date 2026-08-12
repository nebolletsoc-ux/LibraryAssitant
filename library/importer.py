import csv
import io
import re
from typing import Any


TITLE_KEYS = ["title", "book title", "book_title", "name", "book"]
AUTHOR_KEYS = ["author", "authors", "book author", "book_author", "writer"]
ISBN_KEYS = ["isbn", "isbn13", "isbn_13", "isbn10", "isbn_10", "isbn uid", "isbn uid", "isbn uid"]
STATUS_KEYS = ["read status", "status", "reading status"]


def _normalize_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip()


def _coalesce(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def parse_books_from_text(text: str, filename: str | None = None) -> list[dict[str, Any]]:
    if not text or not text.strip():
        return []

    try:
        dialect = csv.Sniffer().sniff(text, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        return []

    books: list[dict[str, Any]] = []

    for row in reader:
        if not row:
            continue

        normalized_row = {
            _normalize_header(key): value
            for key, value in row.items()
            if key is not None
        }

        title = _coalesce(normalized_row, TITLE_KEYS)
        author = _coalesce(normalized_row, AUTHOR_KEYS)
        isbn = _coalesce(normalized_row, ISBN_KEYS)
        status = _coalesce(normalized_row, STATUS_KEYS)

        if not title and not author:
            continue

        if status and "to-read" not in status.lower() and "want" not in status.lower():
            continue

        books.append({
            "title": title,
            "author": author,
            "isbn": isbn or None,
        })

    return books
