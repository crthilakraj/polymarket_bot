from scripts.find_market import _extract_slug


def test_extracts_slug_from_full_event_url():
    assert _extract_slug("https://polymarket.com/event/fed-decision-in-october") == "fed-decision-in-october"


def test_extracts_slug_from_url_with_query_string():
    url = "https://polymarket.com/event/fed-decision-in-october?tid=123"
    assert _extract_slug(url) == "fed-decision-in-october"


def test_treats_bare_slug_looking_string_as_a_slug():
    assert _extract_slug("fed-decision-in-october") == "fed-decision-in-october"


def test_returns_none_for_a_free_text_search_query():
    assert _extract_slug("fed interest rate") is None


def test_returns_none_for_unrelated_url():
    assert _extract_slug("https://example.com/foo") is None
