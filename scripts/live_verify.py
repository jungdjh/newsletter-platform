"""Live source verification — is the agent's cited evidence real?

The numeric guard and the Sr. Editor both trust each story's `source_excerpt`
as ground truth. This closes the remaining hole: an agent that fabricates BOTH
a claim AND a matching excerpt to support it. We re-fetch the live source URL
and check that the saved excerpt actually appears in the real article. A saved
excerpt that isn't in the live source is fabricated evidence — the draft fails.

Graceful by design — a network problem must never fail a real draft:
  - fetch fails / empty body  -> UNVERIFIED (advisory note, never a FAIL)
  - excerpt clearly present    -> OK
  - excerpt clearly ABSENT     -> FABRICATED_EXCERPT (the hard FAIL)
  - somewhere in between        -> PARTIAL (advisory; could be extractor drift)

Matching is fuzzy (word-shingle overlap), so it tolerates the difference between
the agent's extractor and ours; only a near-total absence trips the FAIL.
"""

from __future__ import annotations

import re

# Overlap at/above this = the excerpt is really in the source.
_OK_OVERLAP = 0.50
# Overlap at/below this = the excerpt is not in the source (fabricated evidence).
_FAIL_OVERLAP = 0.15
_SHINGLE_N = 6


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation to spaces, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def _shingles(text: str, n: int = _SHINGLE_N) -> set:
    words = _normalize(text).split()
    if len(words) < n:
        # Short excerpt: the whole normalized string is the single shingle.
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def excerpt_overlap(excerpt: str, source_text: str) -> float:
    """Fraction of the excerpt's word-shingles that appear in the source.

    1.0 = every shingle present (verbatim), 0.0 = none (absent/fabricated)."""
    ex = _shingles(excerpt)
    if not ex:
        return 1.0  # nothing to verify → not a finding
    # For a short excerpt (single shingle), fall back to substring containment.
    if len(ex) == 1:
        only = next(iter(ex))
        return 1.0 if only and only in _normalize(source_text) else 0.0
    src = _shingles(source_text)
    return len(ex & src) / len(ex)


def verify_story(story: dict, fetch_fn) -> dict:
    """Verify one story's excerpt against its live source.

    fetch_fn(url) returns the source body text, or None if it couldn't be
    fetched. Returns {status, overlap, detail}."""
    url = story.get("source_url") or story.get("url")
    excerpt = (story.get("source_excerpt") or "").strip()
    headline = (story.get("headline") or "")[:50]
    if not url or not excerpt:
        return {"status": "OK", "overlap": None, "detail": ""}
    try:
        body = fetch_fn(url)
    except Exception as e:  # noqa: BLE001 — a fetch error is never a fabrication verdict
        return {"status": "UNVERIFIED", "overlap": None,
                "detail": f"'{headline}' — could not fetch source to verify ({type(e).__name__})"}
    if not body:
        return {"status": "UNVERIFIED", "overlap": None,
                "detail": f"'{headline}' — source returned no readable text; excerpt unverified"}
    ov = excerpt_overlap(excerpt, body)
    if ov >= _OK_OVERLAP:
        return {"status": "OK", "overlap": ov, "detail": ""}
    if ov <= _FAIL_OVERLAP:
        return {"status": "FABRICATED_EXCERPT", "overlap": ov,
                "detail": (f"'{headline}' — source excerpt is NOT in the live article "
                           f"({ov:.0%} match) — possible fabricated evidence; verify before sending")}
    return {"status": "PARTIAL", "overlap": ov,
            "detail": (f"'{headline}' — source excerpt only partially matches the live article "
                       f"({ov:.0%}) — verify the quoted lines")}


def verify_other_news(other_news: list, fetch_fn=None) -> tuple[list, bool]:
    """Other News items carry no saved excerpt, so the offline numeric guard is
    blind to them — a fabricated stat can hide in an Other News headline
    ("32 of 200 applicants", "$21,640 more"). Verify their material numbers
    against the LIVE source instead: fetch the URL and check each currency /
    percent / ratio in the item actually appears there.

    Graceful, same as verify_payload: a fetch failure is advisory (never a FAIL);
    only a number clearly absent from a successfully-fetched source hard-fails."""
    from scripts.numeric_guard import unsupported_numbers, _material_claims
    fetch_fn = fetch_fn or _default_fetch
    findings: list = []
    fail = False
    for i, item in enumerate(other_news or []):
        claim = " ".join([item.get("headline") or "",
                          item.get("summary") or item.get("dek") or item.get("subtitle") or ""])
        if not _material_claims(claim):
            continue  # no currency/percent/ratio to verify
        url = item.get("source_url") or item.get("url")
        if not url:
            continue
        h = (item.get("headline") or "")[:50]
        try:
            body = fetch_fn(url)
        except Exception:  # noqa: BLE001 — fetch error is never a fabrication verdict
            body = None
        if not body:
            findings.append(f"Other News {i+1:02d} unverified: could not fetch source ('{h}')")
            continue
        bad = unsupported_numbers(claim, body)
        if bad:
            findings.append(f"Other News {i+1:02d} FABRICATED NUMBER not in live source: "
                            f"{', '.join(bad)} — '{h}'")
            fail = True
    return findings, fail


def _default_fetch(url: str) -> str | None:
    """Fetch full readable body via the agent's own fetch tool (uncapped)."""
    from scripts.tools.web_fetch import fetch as _fetch
    return (_fetch(url, body_cap=0) or {}).get("body_text") or None


def verify_payload(top_stories: list, fetch_fn=None) -> tuple[list, bool]:
    """Verify every top story. Returns (anomaly_lines, fail).

    fail is True only when at least one excerpt is clearly absent from its live
    source (FABRICATED_EXCERPT). Unverified/partial add advisory lines but never
    fail the draft."""
    fetch_fn = fetch_fn or _default_fetch
    findings: list = []
    fail = False
    for i, s in enumerate(top_stories or []):
        r = verify_story(s, fetch_fn)
        if r["status"] == "OK":
            continue
        prefix = {"FABRICATED_EXCERPT": "Story {:02d} FABRICATED EXCERPT",
                  "PARTIAL": "Story {:02d} excerpt partial-match",
                  "UNVERIFIED": "Story {:02d} unverified"}[r["status"]].format(i + 1)
        findings.append(f"{prefix}: {r['detail']}")
        if r["status"] == "FABRICATED_EXCERPT":
            fail = True
    return findings, fail
