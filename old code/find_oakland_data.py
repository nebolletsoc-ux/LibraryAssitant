import requests

url = "https://oaklandlibrary.bibliocommons.com/v2/search?query=The%20Overstory%20Richard%20Powers&searchType=smart"

response = requests.get(url)

html = response.text

position = html.find("Hoopla")

print(html[position-1000:position+1000])