from app import build_book_rows


def test_notorious_rbg_returns_libby_result():
    book = {
        "title": "Notorious RBG: The Life and Times of Ruth Bader Ginsburg",
        "author": "Irin Carmon",
    }
    rows = build_book_rows([book], limit=1)
    assert rows[0]["availability"]
    assert any(item["service"] == "Libby" for item in rows[0]["availability"])
