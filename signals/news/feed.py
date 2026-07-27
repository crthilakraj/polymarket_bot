"""News sources: a NewsFeed streams NewsHeadline items. MockNewsFeed replays a fixed
list for testing/local dev; RssNewsFeed is the real integration point to swap in once
you have feed URLs to watch.
"""

import asyncio
import logging
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsHeadline:
    id: str
    text: str
    source: str
    published_at: datetime


class NewsFeed(ABC):
    """A source of headlines/text snippets, streamed as they arrive."""

    @abstractmethod
    async def stream(self) -> AsyncIterator[NewsHeadline]:
        """Yield headlines as they become available. Runs until cancelled."""
        raise NotImplementedError
        yield  # pragma: no cover - marks this as an async generator for subclasses


class MockNewsFeed(NewsFeed):
    """Replays a fixed list of headlines. Use in tests and for local development
    without a live news source."""

    def __init__(self, headlines: Iterable[NewsHeadline], delay_seconds: float = 0.0):
        self._headlines = list(headlines)
        self._delay_seconds = delay_seconds

    async def stream(self) -> AsyncIterator[NewsHeadline]:
        for headline in self._headlines:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            yield headline


def _parse_rss_entry_datetime(raw: str | None) -> datetime:
    """RSS 2.0 uses RFC 822 dates (<pubDate>), Atom uses ISO 8601
    (<updated>/<published>) - try both, since feeds mix formats and this is
    only used to timestamp a headline, not to gate anything."""
    if raw:
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def parse_feed(xml_text: str, source: str) -> list[NewsHeadline]:
    """Parses RSS 2.0 (<item>) or Atom (<entry>) into NewsHeadlines. Stdlib
    XML only - deliberately avoids adding a feedparser dependency for what's
    a handful of well-known, stable tag shapes."""
    root = ET.fromstring(xml_text)
    atom_ns = "{http://www.w3.org/2005/Atom}"

    headlines: list[NewsHeadline] = []
    items = root.findall(".//item")
    if items:
        for item in items:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link or title).strip()
            if not title or not guid:
                continue
            published = _parse_rss_entry_datetime(item.findtext("pubDate"))
            headlines.append(NewsHeadline(id=guid, text=title, source=source, published_at=published))
        return headlines

    for entry in root.findall(f".//{atom_ns}entry"):
        title = (entry.findtext(f"{atom_ns}title") or "").strip()
        entry_id = (entry.findtext(f"{atom_ns}id") or title).strip()
        if not title or not entry_id:
            continue
        published_raw = entry.findtext(f"{atom_ns}published") or entry.findtext(f"{atom_ns}updated")
        headlines.append(
            NewsHeadline(id=entry_id, text=title, source=source, published_at=_parse_rss_entry_datetime(published_raw))
        )
    return headlines


class RssNewsFeed(NewsFeed):
    """Polls one or more RSS/Atom feed URLs on an interval, deduping by entry id
    (guid/link for RSS, id for Atom) across polls so the same headline isn't
    re-emitted every cycle - only newly-seen entries are yielded.

    A fetch failure for one feed URL (network error, malformed XML) is logged
    and skipped for that poll rather than killing the whole stream - a live
    news source going down transiently shouldn't take the signal offline.
    """

    def __init__(self, feed_urls: list[str], poll_interval_seconds: float = 60.0):
        self._feed_urls = feed_urls
        self._poll_interval_seconds = poll_interval_seconds
        self._seen_ids: set[str] = set()

    async def stream(self) -> AsyncIterator[NewsHeadline]:
        async with httpx.AsyncClient() as client:
            while True:
                for url in self._feed_urls:
                    try:
                        resp = await client.get(url, timeout=15.0, follow_redirects=True)
                        resp.raise_for_status()
                        headlines = parse_feed(resp.text, source=url)
                    except (httpx.HTTPError, ET.ParseError) as exc:
                        logger.warning("news.feed: failed to fetch/parse %s: %s", url, exc)
                        continue
                    for headline in headlines:
                        if headline.id in self._seen_ids:
                            continue
                        self._seen_ids.add(headline.id)
                        yield headline
                await asyncio.sleep(self._poll_interval_seconds)
