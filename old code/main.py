import requests
import csv


def find_isbn(title, author):
    # Step 1: Find the work
    search_url = "https://openlibrary.org/search.json"

    params = {
        "title": title,
        "author": author,
        "limit": 1
    }

    response = requests.get(search_url, params=params)
    data = response.json()

    if not data.get("docs"):
        return "Not found"

    work_key = data["docs"][0]["key"]

    # Step 2: Get editions for that work
    editions_url = f"https://openlibrary.org{work_key}/editions.json"

    editions_response = requests.get(editions_url)
    editions_data = editions_response.json()

    # Step 3: Find an ISBN
    for edition in editions_data.get("entries", []):
        isbns = edition.get("isbn_13") or edition.get("isbn_10")

        if isbns:
            return isbns[0]

    return "Not found"


print("Welcome to Library Assistant!")
print()

with open("books.csv", newline="", encoding="utf-8") as file:
    reader = csv.DictReader(file)

    print("Books with ISBN lookup:")
    print()

    for book in reader:
        isbn = find_isbn(book["Title"], book["Author"])

        print(f"{book['Title']} — {book['Author']}")
        print(f"ISBN: {isbn}")
        print()
    