"""Regression coverage for the MyNextRead frontend list logic.

The rules live in templates/tbr.html (renderBooks()/filteredBooks()/
statusOf()). This suite mirrors that logic as pure functions so it can be
tested offline, guarding the All / eBook / Audio / Available pills, the
title-or-author search, and the Availability/Title/Author/Newest/Oldest
sort behaviour.

Keep the mirror in sync with the JS: add a table-driven case here whenever
frontend filtering changes.
"""


def _fmt(v):
    return (v or "").lower()


def rows_for(ub, format_):
    rows = ub.get("availability") or []
    if format_ == "all":
        return rows
    if format_ == "ebook":
        return [r for r in rows if _fmt(r.get("format")) == "ebook"]
    return [r for r in rows if _fmt(r.get("format")) in ("audiobook", "audio")]


def status_of(ub, format_="all"):
    rows = rows_for(ub, format_)
    if len(rows) == 0:
        return "pending"
    if any(r.get("available") for r in rows):
        return "available"
    if any(r.get("wait_text") for r in rows):
        return "waitlist"
    return "unavailable"


_STATUS_ORDER = {"available": 0, "waitlist": 1, "unavailable": 2, "pending": 3}


def filtered_books(books, query="", active_format="all", available_only=False, sort_key="status"):
    q = query.lower()
    filtered = []
    for ub in books:
        title = (ub.get("book") or {}).get("title", "").lower()
        author = (ub.get("book") or {}).get("author", "").lower()
        if q and q not in title and q not in author:
            continue
        rows = rows_for(ub, active_format)
        if active_format != "all" and len(rows) == 0:
            continue
        if available_only and not any(r.get("available") for r in rows):
            continue
        filtered.append(ub)

    def key(ub):
        if sort_key == "title-asc":
            return ((ub.get("book") or {}).get("title", "").lower(),)
        if sort_key == "author-asc":
            return ((ub.get("book") or {}).get("author", "").lower(),)
        if sort_key == "date-desc":
            return (-_epoch(ub.get("added_at")),)
        if sort_key == "date-asc":
            return (_epoch(ub.get("added_at")),)
        return (_STATUS_ORDER[status_of(ub, active_format)],)

    return sorted(filtered, key=key)


def available_count(books, active_format="all"):
    return sum(1 for ub in books if any(r.get("available") for r in rows_for(ub, active_format)))


# ---------- book detail sheet mirror ---------------------------------------

def _row_status(r):
    if r.get("available"):
        return "available"
    if r.get("wait_text"):
        return "waitlist"
    return "unavailable"


def dedupe_detail_rows(rows):
    best = {}
    for row in rows:
        key = f"{_fmt(row.get('format'))}::{ (row.get('library') or '').lower()}"
        score = (_STATUS_ORDER[_row_status(row)] * 10000
                 + (0 if row.get("url") else 1000)
                 + (0 if row.get("holds") is not None else 100)
                 + (0 if row.get("wait_weeks") else 10))
        if key not in best or score < best[key][1]:
            best[key] = (row, score)
    return [entry[0] for entry in best.values()]


def provider_label(row):
    if (row.get("provider") or "").lower() == "hoopla" or (row.get("library") or "").lower() == "hoopla":
        return "Hoopla"
    provider = row.get("provider") or ""
    library = row.get("library")
    return f"{provider} · {library}" if provider else library


def detail_format(row):
    return row.get("format") or "Unknown"


def _epoch(s):
    if not s:
        return 0
    # Simple parse for the ISO dates used in fixtures.
    try:
        return int(s[:10].replace("-", ""))
    except ValueError:
        return 0


# ---------- fixture builders -----------------------------------------------

def book(id_, title="The Overstory", author="Powers", added="2024-02-14T00:00:00", availability=None):
    return {
        "id": id_,
        "added_at": added,
        "book": {"title": title, "author": author},
        "availability": availability or [],
    }


def row(format_, available, wait_text=None, holds=None, wait_weeks=None):
    return {"format": format_, "available": available, "wait_text": wait_text,
            "holds": holds, "wait_weeks": wait_weeks}


# ---------- status derivation ---------------------------------------------

def test_all_status_available_when_any_row_available():
    ub = book(1, availability=[row("Audiobook", False, "2-week wait"), row("eBook", True)])
    assert status_of(ub, "all") == "available"


def test_all_status_waitlist_when_only_waitlist_rows():
    ub = book(1, availability=[row("Audiobook", False, "2-week wait")])
    assert status_of(ub, "all") == "waitlist"


def test_all_status_pending_when_no_rows():
    ub = book(1, availability=[])
    assert status_of(ub, "all") == "pending"


def test_ebook_status_uses_only_ebook_rows():
    ub = book(1, availability=[row("Audiobook", False, "wait"), row("eBook", False, "wait")])
    assert status_of(ub, "ebook") == "waitlist"
    assert status_of(ub, "audio") == "waitlist"
    ub2 = book(2, availability=[row("Audiobook", True), row("eBook", False, "wait")])
    assert status_of(ub2, "ebook") == "waitlist"
    assert status_of(ub2, "audio") == "available"


# ---------- filtering -------------------------------------------------------

def test_pending_books_visible_even_when_no_rows():
    books = [book(1, availability=[row("eBook", True)]), book(2, availability=[])]
    assert sorted(b["id"] for b in filtered_books(books)) == [1, 2]


def test_all_shows_all_with_availability():
    books = [
        book(1, availability=[row("eBook", True)]),
        book(2, availability=[row("Audiobook", False, "wait")]),
    ]
    assert sorted(b["id"] for b in filtered_books(books)) == [1, 2]


def test_available_only_requires_available_row():
    books = [
        book(1, availability=[row("eBook", True)]),
        book(2, availability=[row("Audiobook", False, "wait")]),
    ]
    result = filtered_books(books, available_only=True)
    assert [b["id"] for b in result] == [1]


def test_ebook_filter_keeps_ebook_rows_only():
    books = [
        book(1, availability=[row("eBook", False)]),
        book(2, availability=[row("Audiobook", True)]),
        book(3, availability=[row("eBook", True)]),
    ]
    result = filtered_books(books, active_format="ebook")
    assert sorted(b["id"] for b in result) == [1, 3]


def test_audio_filter_keeps_audio_rows_only():
    books = [
        book(1, availability=[row("Audiobook", False)]),
        book(2, availability=[row("eBook", True)]),
        book(3, availability=[row("Audiobook", False, "wait")]),
        book(4, availability=[row("Digital", True)]),
    ]
    result = filtered_books(books, active_format="audio")
    assert sorted(b["id"] for b in result) == [1, 3]


def test_ebook_available_only_requires_available_ebook():
    books = [
        book(1, availability=[row("eBook", False), row("Audiobook", True)]),
        book(2, availability=[row("eBook", True)]),
    ]
    result = filtered_books(books, active_format="ebook", available_only=True)
    assert [b["id"] for b in result] == [2]


def test_provider_words_do_not_drive_format_filter():
    books = [book(1, availability=[row("Libby", True)]), book(2, availability=[row("Hoopla", True)])]
    assert filtered_books(books, active_format="ebook") == []
    assert filtered_books(books, active_format="audio") == []


# ---------- search ----------------------------------------------------------

def test_search_matches_title_case_insensitively():
    books = [book(1, title="The Overstory", availability=[row("eBook", True)]),
             book(2, title="Dune", availability=[row("eBook", True)])]
    result = filtered_books(books, query="overstory")
    assert [b["id"] for b in result] == [1]


def test_search_matches_author():
    books = [book(1, title="A", author="Richard Powers", availability=[row("eBook", True)]),
             book(2, title="B", author="Frank Herbert", availability=[row("eBook", True)])]
    result = filtered_books(books, query="powers")
    assert [b["id"] for b in result] == [1]


def test_search_applies_format_filter_together():
    books = [
        book(1, title="Orbital", availability=[row("Audiobook", True)]),
        book(2, title="Orbital", availability=[row("eBook", True)]),
    ]
    result = filtered_books(books, query="orbital", active_format="ebook")
    assert [b["id"] for b in result] == [2]


# ---------- sort ------------------------------------------------------------

def test_sort_status_orders_available_then_waitlist_then_unavailable():
    books = [
        book(1, title="Wait Me", availability=[row("eBook", False, "wait")]),
        book(2, title="Neither", availability=[row("eBook", False)]),
        book(3, title="Read Me", availability=[row("eBook", True)]),
    ]
    result = filtered_books(books, sort_key="status")
    assert [b["id"] for b in result] == [3, 1, 2]


def test_sort_title_asc():
    books = [
        book(1, title="Zebra", availability=[row("eBook", True)]),
        book(2, title="Apple", availability=[row("eBook", True)]),
    ]
    result = filtered_books(books, sort_key="title-asc")
    assert [b["id"] for b in result] == [2, 1]


def test_sort_author_asc():
    books = [
        book(1, title="A", author="Pitt", availability=[row("eBook", True)]),
        book(2, title="B", author="Abbott", availability=[row("eBook", True)]),
    ]
    result = filtered_books(books, sort_key="author-asc")
    assert [b["id"] for b in result] == [2, 1]


def test_sort_date_desc_then_asc():
    books = [
        book(1, title="Old", added="2024-01-01T00:00:00", availability=[row("eBook", True)]),
        book(2, title="New", added="2024-06-01T00:00:00", availability=[row("eBook", True)]),
    ]
    assert [b["id"] for b in filtered_books(books, sort_key="date-desc")] == [2, 1]
    assert [b["id"] for b in filtered_books(books, sort_key="date-asc")] == [1, 2]


# ---------- available count -------------------------------------------------

def test_available_count_respects_format_filter():
    books = [
        book(1, availability=[row("eBook", True)]),
        book(2, availability=[row("Audiobook", True), row("eBook", False, "wait")]),
    ]
    assert available_count(books, "all") == 2
    assert available_count(books, "ebook") == 1
    assert available_count(books, "audio") == 1


# ---------- book detail sheet -----------------------------------------------

def test_detail_dedupes_same_format_and_library():
    rows = [
        {"format": "eBook", "library": "hoopla", "provider": "Hoopla", "available": False, "wait_text": "wait"},
        {"format": "eBook", "library": "hoopla", "provider": "Hoopla", "available": False, "wait_text": "wait"},
        {"format": "eBook", "library": "hoopla", "provider": "Hoopla", "available": True},
    ]
    out = dedupe_detail_rows(rows)
    assert len(out) == 1
    assert out[0]["available"] is True


def test_detail_dedupe_prefers_available_over_waitlist():
    rows = [
        {"format": "eBook", "library": "berkeley", "available": False, "wait_text": "wait", "holds": 9},
        {"format": "eBook", "library": "berkeley", "available": True},
    ]
    out = dedupe_detail_rows(rows)
    assert len(out) == 1
    assert out[0]["available"] is True


def test_detail_keeps_distinct_formats_and_libraries():
    rows = [
        {"format": "eBook", "library": "oakland", "available": True},
        {"format": "eBook", "library": "berkeley", "available": False, "wait_text": "wait"},
        {"format": "Audiobook", "library": "berkeley", "available": True},
    ]
    assert len(dedupe_detail_rows(rows)) == 3


def test_detail_hoopla_label_is_single():
    row = {"format": "Digital", "library": "hoopla", "provider": "Hoopla", "available": True}
    assert provider_label(row) == "Hoopla"
    assert "Hoopla · Hoopla" not in (provider_label(row) + detail_format(row))


def test_detail_non_hoopla_label_keeps_provider_and_library():
    row = {"format": "eBook", "library": "berkeley", "provider": "Libby", "available": False, "wait_text": "wait"}
    assert provider_label(row) == "Libby · berkeley"
    assert detail_format(row) == "eBook"