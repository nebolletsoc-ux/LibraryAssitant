import requests
from functools import lru_cache

SESSION = requests.Session()

TIMEOUT = (3, 8)

@lru_cache(maxsize=2000)
def find_isbn(title, author):
    search_url = "https://openlibrary.org/search.json"

    params = {
        "title": title,
        "author": author,
        "limit": 1,
    }

    try:
        response = SESSION.get(
            search_url,
            params=params,
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        docs = response.json().get("docs", [])

        if not docs:
            return None

        isbn_list = docs[0].get("isbn", [])

        if isbn_list:
            for isbn in isbn_list:
                if len(isbn) == 13:
                    return isbn

        work_key = docs[0].get("key")

        if not work_key:
            return None

        response = SESSION.get(
            f"https://openlibrary.org{work_key}/editions.json",
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        editions = response.json().get("entries", [])

        for edition in editions:
            isbn_13 = edition.get("isbn_13")

            if isbn_13:
                return isbn_13[0]

            isbn_10 = edition.get("isbn_10")

            if isbn_10:
                return isbn_10[0]

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        IndexError,
    ):
        return None

    return None