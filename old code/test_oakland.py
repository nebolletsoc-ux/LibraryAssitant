import requests

url = "https://oaklandlibrary.bibliocommons.com/v2/search?query=The%20Overstory%20Richard%20Powers&searchType=smart"

response = requests.get(url)

print("Status code:")
print(response.status_code)

print()

print("First 500 characters:")
print(response.text[:500])