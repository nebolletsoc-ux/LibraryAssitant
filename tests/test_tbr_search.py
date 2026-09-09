"""TBR search-box regression guards.

The /tbr page's top search box is an *online* add-book search (OpenLibrary via
POST /api/books/search). It must never filter (or otherwise affect) the user's
own reading list, and its "Add from online search" results must render above
the book list so they are immediately visible.

These are DOM/string-level checks against the served template: the pytest suite
does not execute the page's JavaScript, so a UI-only regression like the one
this guards (search box silently re-filtering the TBR list and burying the
online panel below the cards) used to sail through a fully-green suite.
"""

# Fragments that implemented the WRONG behaviour (search text filtering the
# local list inside filteredBooks()). Their presence in the template means the
# regression is back.
_LOCAL_FILTER_FRAGMENTS = (
    "title.includes(q)",
    "author.includes(q)",
)


def _tbr_html(client):
    resp = client.get("/tbr")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _listener_body(html, element_id, event):
    marker = f'document.getElementById("{element_id}").addEventListener("{event}"'
    start = html.find(marker)
    assert start != -1, f"listener for #{element_id} ({event}) not found"
    return html[start:].split("});", 1)[0]


def test_search_box_does_not_filter_tbr_list(client):
    html = _tbr_html(client)
    for fragment in _LOCAL_FILTER_FRAGMENTS:
        assert fragment not in html, (
            f"search text filtering crept back into the TBR list filter "
            f"(found {fragment!r})"
        )


def test_search_input_listener_triggers_online_search_only(client):
    html = _tbr_html(client)
    body = _listener_body(html, "search-input", "input")
    assert "renderOnlineResults(" in body, "typing must trigger the online search"
    assert "renderBooks(" not in body, "typing must NOT re-render/filter the TBR list"


def test_clear_button_does_not_filter_tbr_list(client):
    html = _tbr_html(client)
    body = _listener_body(html, "clear-btn", "click")
    assert "renderBooks(" not in body, "clearing the box must NOT re-render/filter the list"


def test_online_results_render_above_the_book_list(client):
    html = _tbr_html(client)
    online = html.find('id="online-results"')
    books = html.find('id="books-container"')
    assert online != -1, "no online-results panel in the template"
    assert books != -1, "no books-container in the template"
    assert online < books, "online add-results must render ABOVE the TBR list"