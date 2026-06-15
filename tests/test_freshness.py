"""Tests for the Other News strict freshness gate (run_newsletter._drop_stale_other_news).

Offline, no API. Cutoff = today - 3 days, same as Top Stories.
"""

from datetime import date

from scripts.run_newsletter import _drop_stale_other_news

TODAY = date(2026, 6, 7)
EARLIEST = date(2026, 6, 4)  # today - 3


def _item(headline, pub):
    return {"track": "T", "headline": headline, "subtitle": "s",
            "source_url": "https://techcrunch.com/x", "published_at": pub}


def test_drops_items_before_cutoff():
    items = [_item("fresh", "2026-06-06"), _item("stale", "2026-06-02")]
    kept, dropped = _drop_stale_other_news(items, EARLIEST, TODAY)
    assert [k["headline"] for k in kept] == ["fresh"]
    assert len(dropped) == 1 and "stale" in dropped[0] and "2026-06-02" in dropped[0]


def test_keeps_item_on_cutoff_boundary():
    # cutoff itself is acceptable (gate is "before", strictly older)
    kept, dropped = _drop_stale_other_news([_item("edge", "2026-06-04")], EARLIEST, TODAY)
    assert len(kept) == 1 and not dropped


def test_keeps_items_with_missing_or_bad_date():
    # can't prove staleness → keep (never drop on a guess)
    items = [_item("nodate", None), {"headline": "absent", "source_url": "u"},
             _item("garbage", "not-a-date")]
    kept, dropped = _drop_stale_other_news(items, EARLIEST, TODAY)
    assert len(kept) == 3 and not dropped


def test_empty_list():
    assert _drop_stale_other_news([], EARLIEST, TODAY) == ([], [])
