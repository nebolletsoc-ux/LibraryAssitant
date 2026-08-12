from library.oakland import search


def test_full_house_returns_hoopla_result():
    results = search("Full House: The Spread of Excellence from Plato to Darwin", "Stephen Jay Gould", timeout=15)
    assert results
    assert any(item["service"] == "Hoopla" for item in results)
