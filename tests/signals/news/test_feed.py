import asyncio
from datetime import datetime, timezone

import httpx

from signals.news.feed import MockNewsFeed, NewsHeadline, RssNewsFeed, parse_feed

RSS2_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <item>
    <title>Headline One</title>
    <link>https://example.com/1</link>
    <guid>guid-1</guid>
    <pubDate>Sun, 26 Jul 2026 11:41:04 GMT</pubDate>
  </item>
  <item>
    <title>Headline Two</title>
    <link>https://example.com/2</link>
    <guid>guid-2</guid>
    <pubDate>Sun, 26 Jul 2026 11:29:07 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM_SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Atom Feed</title>
  <entry>
    <title>Atom Headline</title>
    <id>atom-1</id>
    <published>2026-07-26T11:41:04Z</published>
  </entry>
</feed>"""


def test_mock_news_feed_yields_all_headlines_in_order():
    headlines = [
        NewsHeadline(id="1", text="a", source="test", published_at=datetime.now(timezone.utc)),
        NewsHeadline(id="2", text="b", source="test", published_at=datetime.now(timezone.utc)),
    ]
    feed = MockNewsFeed(headlines)

    async def collect():
        return [h async for h in feed.stream()]

    assert asyncio.run(collect()) == headlines


def test_mock_news_feed_handles_empty_list():
    feed = MockNewsFeed([])

    async def collect():
        return [h async for h in feed.stream()]

    assert asyncio.run(collect()) == []


def test_parse_feed_parses_rss2_items():
    headlines = parse_feed(RSS2_SAMPLE, source="test-rss")

    assert [h.text for h in headlines] == ["Headline One", "Headline Two"]
    assert headlines[0].id == "guid-1"
    assert headlines[0].source == "test-rss"
    assert headlines[0].published_at == datetime(2026, 7, 26, 11, 41, 4, tzinfo=timezone.utc)


def test_parse_feed_parses_atom_entries():
    headlines = parse_feed(ATOM_SAMPLE, source="test-atom")

    assert len(headlines) == 1
    assert headlines[0].text == "Atom Headline"
    assert headlines[0].id == "atom-1"
    assert headlines[0].published_at == datetime(2026, 7, 26, 11, 41, 4, tzinfo=timezone.utc)


def test_parse_feed_skips_items_missing_title():
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><guid>only-guid</guid></item>
      <item><title>Has Title</title><guid>guid-x</guid></item>
    </channel></rss>"""

    headlines = parse_feed(xml, source="test")

    assert [h.text for h in headlines] == ["Has Title"]


def test_rss_news_feed_dedupes_across_polls():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # Second poll re-serves the same two items plus one new one.
        content = RSS2_SAMPLE if call_count == 1 else RSS2_SAMPLE.replace(
            "</channel>",
            "<item><title>Headline Three</title><guid>guid-3</guid></item></channel>",
        )
        return httpx.Response(200, text=content)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    async def collect():
        import signals.news.feed as feed_module

        original = feed_module.httpx.AsyncClient
        feed_module.httpx.AsyncClient = patched_client
        try:
            feed = RssNewsFeed(["https://example.com/rss"], poll_interval_seconds=0)
            collected = []
            async for headline in feed.stream():
                collected.append(headline)
                if len(collected) == 3:
                    break
            return collected
        finally:
            feed_module.httpx.AsyncClient = original

    collected = asyncio.run(collect())

    assert [h.id for h in collected] == ["guid-1", "guid-2", "guid-3"]
    assert call_count == 2
