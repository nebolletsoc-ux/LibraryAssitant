"""Tests for the Bibliocommons catalog parser in library/oakland.py.

The parser extracts the catalog's own manifestations (Book/eBook/
Audiobook/...) from a Bibliocommons search page. Network is driven by
fixture HTML through a stubbed requests.get so these run offline.
"""


def _item(title, author, manifestations, hoopla_href=None):
    """Build a single cp-search-result-item block for fixture pages."""

    hoopla = ""
    if hoopla_href:
        hoopla = (
            '<a class="cp-availability-bib-block" '
            f'href="{hoopla_href}" '
            'rel="noopener noreferrer">'
            '<span aria-hidden="true">Instantly available on hoopla'
            "</span></a>"
        )

    item = (
        '<li class="row cp-search-result-item">'
        '<a href="/v2/record/SITEM1" data-key="bib-image-link">cover</a>'
        '<h3 class="cp-title">'
        '<a href="/v2/record/SITEM1" data-key="bib-title">'
        f'<span class="title-content">{title}</span>'
        "</a></h3>"
        '<span class="cp-by-author-block --block">by '
        '<span class="cp-author-link"><span>'
        f'<a class="author-link" data-key="author-link" href="/v2/s">{author}</a>'
        "</span></span></span>"
        '<div class="info"><div class="cp-manifestation-list">'
        + "".join(manifestations)
        + hoopla
        + "</div></div></li>"
    )
    return item


def _manifestation(url, fmt_label, icon, wrap, avail_text):
    """Build one manifestation-item block."""
    svg = f'<svg aria-hidden="true" class="cp-svg icon-svg-{icon} icon"></svg>'
    return (
        '<div class="manifestation-item cp-manifestation-list-item row">'
        f'<div class="manifestation-item-format-call-wrap {wrap}">'
        '<div class="manifestation-item-format-call-wrap-inner">'
        + svg
        + '<div class="manifestation-item-format-info-wrap">'
        f'<a class="manifestation-item-link" href="{url}">'
        '<div class="cp-format-info">'
        '<span aria-hidden="true" class="display-info">'
        f'<span class="display-info-primary">{fmt_label}</span>'
        "</span></div></a>"
        '<div class="manifestation-item-availability-block-wrap">'
        f"{avail_text}</div>"
        "</div></div></div></div>"
    )


def _page(items):
    return (
        '<section class="results-list row"><ul class="results">'
        + "".join(items)
        + "</ul></section>"
    )


def test_extract_catalog_ebook_unavailable_with_holds():
    from library.oakland import _extract_bibliocommons_results

    html = _page([
        _item(
            "The Overstory",
            "Richard Powers",
            [
                _manifestation(
                    "/v2/record/SEBOOK1", "eBook, 2019", "ebook",
                    "unavailable",
                    "All copies in use Holds: 11 on 5 copies",
                )
            ],
        )
    ])

    results = _extract_bibliocommons_results(
        html, "oaklandlibrary", "oakland",
        "The Overstory", "Richard Powers",
    )

    assert len(results) == 1

    row = results[0]
    assert row.library == "oakland"
    assert row.provider == "Catalog"
    assert row.format == "eBook"
    assert row.available is False
    assert row.holds == 11
    assert row.url == "https://oaklandlibrary.bibliocommons.com/v2/record/SEBOOK1"
    assert "11 holds" in row.wait


def test_extract_catalog_book_available():
    from library.oakland import _extract_bibliocommons_results

    html = _page([
        _item(
            "The Overstory",
            "Richard Powers",
            [
                _manifestation(
                    "/v2/record/SBOOK1", "Book, 2018", "book",
                    "available", "Available",
                )
            ],
        )
    ])

    results = _extract_bibliocommons_results(
        html, "oaklandlibrary", "oakland",
        "The Overstory", "Richard Powers",
    )

    assert len(results) == 1
    row = results[0]
    assert row.format == "Book"
    assert row.available is True
    assert row.wait is None


def test_extract_catalog_hoopla_instant_available():
    from library.oakland import _extract_bibliocommons_results

    html = _page([
        _item(
            "The Overstory",
            "Richard Powers",
            [
                _manifestation(
                    "/v2/record/SAUD1", "eAudiobook, 2019", "audiobook",
                    "available",
                    '<a href="https://www.hoopladigital.com/title/123">'
                    "Check out now on Hoopla</a>",
                )
            ],
        )
    ])

    results = _extract_bibliocommons_results(
        html, "oaklandlibrary", "oakland",
        "The Overstory", "Richard Powers",
    )

    assert len(results) == 1
    assert results[0].format == "Audiobook"
    assert results[0].available is True


def test_extract_catalog_skips_unrelated_titles():
    from library.oakland import _extract_bibliocommons_results

    html = _page([
        _item(
            "Some Unrelated Book",
            "Someone Else",
            [
                _manifestation(
                    "/v2/record/SOTHER", "Book, 2020", "book",
                    "available", "Available",
                )
            ],
        )
    ])

    results = _extract_bibliocommons_results(
        html, "oaklandlibrary", "oakland",
        "The Overstory", "Richard Powers",
    )

    assert results == []


def test_search_bibliocommons_combines_catalog_and_hoopla(monkeypatch):
    from library.oakland import search_bibliocommons

    html = _page([
        _item(
            "The Overstory",
            "Richard Powers",
            [
                _manifestation(
                    "/v2/record/SEBOOK1", "eBook, 2019", "ebook",
                    "unavailable", "All copies in use Holds: 11 on 5 copies",
                )
            ],
            hoopla_href="https://www.hoopladigital.com/title/12337201",
        )
    ])

    class _FakeResponse:
        status_code = 200
        text = html

    def fake_get(url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(
        "library.oakland.requests.get",
        fake_get,
    )

    results = search_bibliocommons(
        "oaklandlibrary", "oakland",
        "The Overstory", "Richard Powers",
    )

    catalog_rows = [r for r in results if r.provider == "Catalog"]
    hoopla_rows = [r for r in results if r.provider == "Hoopla"]

    assert len(catalog_rows) == 1
    assert catalog_rows[0].library == "oakland"
    assert catalog_rows[0].format == "eBook"

    assert len(hoopla_rows) == 1
    assert hoopla_rows[0].library == "hoopla"
    assert hoopla_rows[0].available is True


def test_search_oakland_uses_catalog_and_hoopla(monkeypatch):
    from library.oakland import search_oakland

    html = _page([
        _item(
            "The Overstory",
            "Richard Powers",
            [
                _manifestation(
                    "/v2/record/SBOOK1", "Book, 2018", "book",
                    "available", "Available",
                )
            ],
        )
    ])

    class _FakeResponse:
        status_code = 200
        text = html

    def fake_get(url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(
        "library.oakland.requests.get",
        fake_get,
    )

    results = search_oakland(
        "The Overstory", "Richard Powers",
    )

    assert len(results) == 1
    assert results[0].library == "oakland"
    assert results[0].provider == "Catalog"