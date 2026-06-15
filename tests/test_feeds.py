"""Tests for multi-source ingestion (scripts/tools/feeds.py).

Offline — requests.get is monkeypatched to return a fixture feed (no network).
"""

from datetime import date
from pathlib import Path

import pytest
import requests

from scripts.tools import feeds
from scripts.compact_prompts import get_feeds

FIXTURE = (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_bytes()
EARLIEST = date(2026, 6, 4)


class _FakeResp:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status
        self.url = "https://feed.example.com/rss"


@pytest.fixture
def fixture_feed(monkeypatch):
    monkeypatch.setattr(feeds.requests, "get", lambda *a, **k: _FakeResp(FIXTURE))


def test_freshness_dedupe_and_nodate(fixture_feed):
    cands, warnings = feeds.fetch_feed_candidates(["https://feed/x"], EARLIEST)
    assert not warnings
    titles = [c["title"] for c in cands]
    # stale "Beta" dropped; the two Alpha duplicates collapse to one; Gamma (no date) kept
    assert titles == ["Alpha launches model", "Gamma no date"]
    # no-date entry ranked last
    assert cands[0]["source_domain"] == "theverge.com"
    assert cands[-1]["title"] == "Gamma no date"
    # summary HTML stripped
    assert cands[0]["summary"] == "Alpha ships a new model today."


def test_cap_respected(fixture_feed):
    cands, _ = feeds.fetch_feed_candidates(["https://feed/x"], EARLIEST, limit=1)
    assert len(cands) == 1 and cands[0]["title"] == "Alpha launches model"


def test_dead_feed_skipped_with_warning(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("dns fail")
    monkeypatch.setattr(feeds.requests, "get", boom)
    cands, warnings = feeds.fetch_feed_candidates(["https://dead/x"], EARLIEST)
    assert cands == [] and len(warnings) == 1 and "dead/x" in warnings[0]


def test_http_error_skipped(monkeypatch):
    monkeypatch.setattr(feeds.requests, "get", lambda *a, **k: _FakeResp(b"", status=503))
    cands, warnings = feeds.fetch_feed_candidates(["https://feed/x"], EARLIEST)
    assert cands == [] and "503" in warnings[0]


def test_clean_summary_strips_html():
    assert feeds._clean_summary("<p>Hello <b>world</b></p>") == "Hello world"
    assert feeds._clean_summary(None) == ""


# ---- get_feeds --------------------------------------------------------------


def test_get_feeds_platform_reads_spec():
    assert any("theverge.com" in u for u in get_feeds("ai-pms"))


def test_get_feeds_unknown_empty():
    assert get_feeds("does-not-exist-xyz") == []
