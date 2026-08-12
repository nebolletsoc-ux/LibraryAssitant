import requests

url = "https://gateway.bibliocommons.com/v2/libraries/oaklandlibrary/bibs/search/probes"

params = {
    "query": "The Overstory Richard Powers",
    "searchType": "keyword",
    "locale": "en-US"
}

response = requests.get(url, params=params)

print("Status:", response.status_code)
print()
print("Content type:")
print(response.headers.get("content-type"))
print()

print("First 1000 characters:")
print(response.text[:1000])