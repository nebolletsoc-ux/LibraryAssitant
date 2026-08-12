import requests
from bs4 import BeautifulSoup


def search_oakland(title, author):

    query = f"{title} {author}"

    url = "https://oaklandlibrary.bibliocommons.com/v2/search"

    params = {
        "query": query,
        "searchType": "smart"
    }

    response = requests.get(url, params=params)

    soup = BeautifulSoup(response.text, "html.parser")

    results = []

    # Find Hoopla links
    for link in soup.find_all("a", href=True):

        href = link["href"]
        text = link.get_text(" ", strip=True)

        if "hoopladigital.com" in href:
            results.append({
               "service": "Hoopla",
                "status": "Available now",
               "link": href
            })
        

    return results


books = search_oakland("The Overstory", "Richard Powers")

for book in books:
    print(book)