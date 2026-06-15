"""Tests for Phase 1.5 sourcing — the web_search allowlist plumbing.

All offline (no API): build_tool_schemas, domain normalization, and get_sources.
"""

from scripts.agent_loop import (
    build_tool_schemas,
    _normalize_domain,
    _normalize_domains,
    _parse_inaccessible_domains,
    TIER1_BASELINE_DOMAINS,
    WEB_SEARCH_BLOCKED_DOMAINS,
)
from scripts.compact_prompts import get_sources


# ---- domain normalization ---------------------------------------------------

def test_normalize_strips_scheme_www_and_trailing_slash():
    assert _normalize_domain("https://www.Example.com/") == "example.com"
    assert _normalize_domain("HTTP://Reuters.COM") == "reuters.com"
    assert _normalize_domain("  techcrunch.com ") == "techcrunch.com"


def test_normalize_keeps_subpath():
    # web_search allows subpaths; only the trailing slash is stripped
    assert _normalize_domain("example.com/blog/") == "example.com/blog"


def test_normalize_domains_dedupes_and_sorts():
    out = _normalize_domains(["b.com", "https://www.b.com", "a.com", ""])
    assert out == ["a.com", "b.com"]  # deduped, sorted, empties dropped


# ---- build_tool_schemas -----------------------------------------------------

def test_allowlist_mode_uses_allowed_not_blocked():
    ws = build_tool_schemas(["example.com", "example.com"])[0]
    assert ws["name"] == "web_search"
    assert ws["allowed_domains"] == ["example.com"]
    assert "blocked_domains" not in ws  # mutually exclusive per the API


def test_blocklist_fallback_when_no_allowlist():
    for empty in (None, []):
        ws = build_tool_schemas(empty)[0]
        assert ws["blocked_domains"] == WEB_SEARCH_BLOCKED_DOMAINS
        assert "allowed_domains" not in ws


def test_allowlist_is_sorted_for_stable_cache():
    ws = build_tool_schemas(["zeta.com", "alpha.com"])[0]
    assert ws["allowed_domains"] == sorted(ws["allowed_domains"])


def test_static_tools_still_present_and_cache_anchored():
    schemas = build_tool_schemas(["example.com"])
    names = [t.get("name") for t in schemas]
    assert names[0] == "web_search"
    for required in ("web_fetch", "og_image_check", "submit_newsletter"):
        assert required in names
    # cache_control still anchors the last tool
    assert schemas[-1].get("cache_control") == {"type": "ephemeral"}


# ---- crawler-inaccessible domain pruning ------------------------------------

def test_parse_inaccessible_domains_extracts_and_normalizes():
    msg = ("The following domains are not accessible to our user agent: "
           "['ft.com', 'nytimes.com', 'WWW.WSJ.com']. Read more...")
    assert _parse_inaccessible_domains(msg) == ["ft.com", "nytimes.com", "wsj.com"]


def test_parse_inaccessible_domains_non_match_returns_empty():
    assert _parse_inaccessible_domains("some unrelated 400 error") == []
    assert _parse_inaccessible_domains("") == []


def test_tier1_baseline_excludes_known_inaccessible_majors():
    # These block Anthropic's crawler — must NOT seed the allowlist or every run 400s.
    inaccessible = {"nytimes.com", "wsj.com", "ft.com", "reuters.com", "apnews.com",
                    "economist.com", "wired.com", "arstechnica.com", "theverge.com",
                    "theguardian.com"}
    assert not (set(TIER1_BASELINE_DOMAINS) & inaccessible)


# ---- get_sources ------------------------------------------------------------



def test_get_sources_platform_reads_spec():
    s = get_sources("ai-pms")  # briefs/ai-pms.json
    assert "techcrunch.com" in s


def test_get_sources_unknown_returns_empty():
    assert get_sources("does-not-exist-xyz") == []


def test_tier1_baseline_nonempty_bare_domains():
    assert TIER1_BASELINE_DOMAINS
    for d in TIER1_BASELINE_DOMAINS:
        assert "://" not in d and not d.startswith("www.")
