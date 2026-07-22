"""Content-flow A (visibility): the Top-3 floor and other anomalies must be
LOUD where David actually reviews — a banner in the console — not just in CI
logs. Also verifies the bundle carries anomalies from the payload."""

import json

from scripts.build_review import render_review_html, build_bundle_from_payload


def _bundle(anomalies):
    return {
        "newsletter": "download", "issue_number": 1, "anomalies": anomalies,
        "stories": [{
            "headline": "H", "track": "T", "summary": "S", "implications": ["i"],
            "source_excerpt": "e", "source_url": "https://example.com",
            "article_title": "", "article_text": "body", "fetched": True,
        }],
    }


def test_anomaly_banner_renders():
    # Banner copy changed 2026-07-21 (anomaly_rank): it now counts only the
    # items needing a decision. The intent — a real problem is LOUD — is unchanged.
    html = render_review_html(_bundle(["Top-3 floor SHORT: only 1 top story — expected 3"]))
    assert "need" in html and "your call" in html
    assert 'class="anomalies"' in html
    assert "Top-3 floor SHORT" in html


def test_resolved_notes_render_quietly_not_as_alerts():
    # A confirmed-by-fallback date is provenance, not an alert: it must still be
    # present (nothing is hidden) but outside the ⚠ banner.
    note = ("CoinDesk article did not return published_at from web_fetch; date "
            "confirmed from URL path and search result '2 days ago'.")
    html = render_review_html(_bundle([note]))
    assert 'class="provenance"' in html
    assert 'class="anomalies"' not in html          # nothing needs a decision
    assert "date confirmed from URL path" in html   # but it is still shown


def test_no_banner_when_clean():
    html = render_review_html(_bundle([]))
    assert 'class="anomalies"' not in html
    assert 'class="provenance"' not in html


def test_bundle_carries_anomalies_from_payload(tmp_path):
    payload = {
        "newsletter": "download",
        "meta": {"issue_number": 5},
        "content": {
            "top_stories": [{
                "headline": "H", "track": "T", "summary": "S", "implications": ["i"],
                "source_excerpt": "e", "source_url": "https://example.com",
                "article_text": "body",  # non-empty -> no network fetch
            }],
            "other_news": [],
            "anomalies": ["Top-3 floor SHORT: only 1 top story"],
        },
    }
    p = tmp_path / "download.json"
    p.write_text(json.dumps(payload))
    bundle = build_bundle_from_payload(p)
    assert bundle["anomalies"] == ["Top-3 floor SHORT: only 1 top story"]
