from io import BytesIO

from app import app


def test_load_more_returns_another_batch_for_large_upload():
    client = app.test_client()
    rows = [f"Title {i},Author {i}" for i in range(25)]
    content = "Title,Author\n" + "\n".join(rows)

    response = client.post(
        "/",
        data={"books_file": (BytesIO(content.encode("utf-8")), "books.csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="load-more"' in html

    load_response = client.get("/load-more?offset=20&limit=20")
    payload = load_response.get_json()
    assert load_response.status_code == 200
    assert payload["has_more"] is False
    assert payload["next_offset"] == 25
    assert payload["html"]
