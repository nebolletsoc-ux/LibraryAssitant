"""Guards for the "checked but nothing found" availability state.

A book whose scan completed but produced zero library results has
availability_summary.checked = True (so the footer's "checked" count includes
it) and an empty availability array. The frontend must treat that as
*Unavailable* — grey "Not checked" styling and an "Availability has not been
checked yet." detail message are wrong for it. These tests pin down both the
API contract and the template wiring.
"""

from datetime import datetime, timezone


def _tbr_html(client):
    resp = client.get("/tbr")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_checked_empty_book_is_reported_checked_by_api(client, app_context):
    """A scanned book with zero results stays flagged checked with no rows."""
    resp = client.post("/api/books",
                       json={"title": "Nowhere to Be Found", "author": "Nobody"})
    assert resp.status_code == 201
    ub_id = resp.get_json()["id"]

    with app_context.app.app_context():
        from models import UserBook, db
        ub = UserBook.query.get(ub_id)
        ub.last_checked_at = datetime.now(timezone.utc)
        db.session.commit()

    rows = client.get("/api/books").get_json()
    row = next(b for b in rows if b["id"] == ub_id)
    assert row["availability"] == []
    assert row["availability_summary"]["checked"] is True


def test_status_of_checked_empty_is_unavailable_in_template(client):
    html = _tbr_html(client)
    assert ("availability_summary?.checked ? \"unavailable\" : \"pending\""
            in html), ("statusOf must classify a checked-but-empty book as "
                       "unavailable, not as pending/not-checked")


def test_detail_sheet_message_distinguishes_checked(client):
    html = _tbr_html(client)
    assert "No copies currently available at your libraries." in html
    assert "Availability has not been checked yet." in html
    # The empty-rows branch must be conditional on the checked flag.
    assert "ub.availability_summary?.checked" in html