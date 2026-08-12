from library.oakland import parse_search_results


def test_parse_search_results_finds_hoopla_and_libby_links():
    html = """
    <html><body>
      <a href="https://www.hoopladigital.com/title/12345">Hoopla</a>
      <a href="https://libbyapp.com/library/abc">Libby</a>
    </body></html>
    """

    results = parse_search_results(html, "oakland")

    services = {item.service for item in results}
    assert "Hoopla" in services
    assert "Libby" in services
