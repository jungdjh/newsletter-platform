"""Multi-source ingestion: pull fresh article candidates from curated RSS/Atom feeds.

Feeds are fetched CLIENT-SIDE (normal Chrome UA via requests), the same path as
web_fetch — NOT Anthropic's web_search crawler. So feeds surface fresh links from
quality outlets the web_search allowlist can't reach (NYT/WSJ/Verge/Guardian), and
web_fetch can then read them. The agent receives these as LEADS and must web_fetch
each to verify facts/date/excerpt before using — the candidate list is discovery,
not a citation.

Public API:
    fetch_feed_candidates(feed_urls, earliest_date, *, limit=25, timeout=15)
        -> (candidates, warnings)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from .web_fetch import DEFAULT_HEADERS

_SUMMARY_MAX = 200


def _clean_summary(raw: str | None) -> str:
    """Strip HTML tags from a feed summary and truncate."""
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return text[:_SUMMARY_MAX].strip()


def _entry_domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _normalize_url(url: str) -> str:
    """For dedupe: drop fragment + trailing slash, lowercase. Keep query (some
    feeds carry the article id there)."""
    u = (url or "").strip().split("#", 1)[0].rstrip("/")
    return u.lower()


def _entry_date(entry: Any) -> date | None:
    """feedparser exposes *_parsed (UTC time.struct_time) for published/updated."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc).date()
            except Exception:  # noqa: BLE001
                pass
    return None


def _entry_date_iso(entry: Any) -> str | None:
    for key in ("published", "updated"):
        v = entry.get(key)
        if v:
            return v
    return None


def fetch_feed_candidates(
    feed_urls: list[str],
    earliest_date: date,
    *,
    limit: int = 25,
    timeout: int = 15,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Pull recent entries from feeds → freshness-filtered, deduped candidate list.

    Each candidate: {title, url, published_at (str|None), published_date (date|None),
    summary, source_domain}. Entries older than earliest_date are dropped; no-date
    entries are kept but ranked last. Sorted most-recent-first, capped at `limit`.

    Robust by design: a feed that errors (network / HTTP / parse / empty) is skipped
    with a warning — one bad feed never aborts the run. Returns (candidates, warnings).
    """
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for url in feed_urls:
        try:
            resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
            if resp.status_code >= 400:
                warnings.append(f"feed {url}: HTTP {resp.status_code}")
                continue
            parsed = feedparser.parse(resp.content)
            entries = parsed.entries or []
            if not entries:
                warnings.append(f"feed {url}: no entries (bozo={getattr(parsed, 'bozo', 0)})")
                continue
        except requests.RequestException as e:
            warnings.append(f"feed {url}: {type(e).__name__}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 — never let one feed abort ingestion
            warnings.append(f"feed {url}: parse error: {type(e).__name__}: {e}")
            continue

        for entry in entries:
            link = (entry.get("link") or "").strip()
            title = (entry.get("title") or "").strip()
            if not link or not title:
                continue
            nurl, ntitle = _normalize_url(link), title.lower()
            if nurl in seen_urls or ntitle in seen_titles:
                continue
            d = _entry_date(entry)
            if d is not None and d < earliest_date:
                continue  # stale — pre-filter to the freshness window
            seen_urls.add(nurl)
            seen_titles.add(ntitle)
            candidates.append({
                "title": title,
                "url": link,
                "published_at": _entry_date_iso(entry),
                "published_date": d,
                "summary": _clean_summary(entry.get("summary")),
                "source_domain": _entry_domain(link),
            })

    # most-recent first; no-date entries (date.min sentinel) sink to the bottom
    candidates.sort(key=lambda c: c["published_date"] or date.min, reverse=True)
    return candidates[:limit], warnings
