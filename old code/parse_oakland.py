import requests
from bs4 import BeautifulSoup

url = "https://oaklandlibrary.bibliocommons.com/v2/search?query=The%20Overstory%20Richard%20Powers&searchType=smart"

response = requests.get(url)

soup = BeautifulSoup(response.text, "html.parser")

# Look at page title
print("Page title:")
print(soup.title.text)

print()

# Find all visible text
text = soup.get_text("\n")

print(text[:2000])