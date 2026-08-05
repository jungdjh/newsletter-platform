"""Deterministic numeric-claim guard — catches fabricated statistics.

The agent must cite every number it prints. Each story carries a
`source_excerpt` (verbatim text the agent pulled from the source it fetched).
This guard extracts the *material* numbers from a story's human-visible copy —
currency amounts, percentages, and "N of M" ratios — and checks each appears in
that story's own excerpt. A figure in the copy but absent from its cited excerpt
is a likely fabrication: exactly the failure the LLM fact-check missed (an
invented "$21,640/yr BSN-vs-ADN gap" or "32 of 200 applicants" bolted onto a
source page that never states it).

Design notes:
  - Deterministic + offline. No network, no LLM. The excerpt is treated as the
    evidence the agent committed to; if a figure is not there, a human must see
    it before it ships.
  - Scoped to currency / percent / ratio — the high-signal statistic types. Bare
    counts (e.g. "4 sections") are skipped: they are usually legitimate and only
    truncated out of a snippet, so flagging them would be noise.
  - Ranges ("$32–$47") and ratios ("32 of 200") are checked at every endpoint.
  - A story that prints material numbers but saved NO excerpt is itself flagged —
    otherwise omitting the excerpt would silently dodge the check.
"""

from __future__ import annotations

import re

# A number literal: 21,640 / 21640 / 4.9 / 200. Optional K/M/B magnitude suffix
# is captured separately so "$21K" and "$21,000" compare equal.
_NUM = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"
_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

# The K/M/B magnitude suffix counts only when it isn't the first letter of a
# following word — otherwise "$21,640 more" parses the "m" of "more" as Mega
# and reads as $21.6B (a spurious mismatch + a double-count with the plain form).
_CURRENCY_RE = re.compile(rf"\$\s?({_NUM})\s?([KMBkmb])?(?![A-Za-z])")
_PERCENT_RE = re.compile(rf"({_NUM})\s?%")
_RATIO_RE = re.compile(rf"\b({_NUM})\s+of\s+({_NUM})\b|\b({_NUM})\s*/\s*({_NUM})\b")
# Any number literal (with optional magnitude suffix) — used to build the set of
# values the excerpt actually contains. Same suffix-not-a-word-letter guard as
# _CURRENCY_RE so the excerpt parses "$21,640 more" identically to the claim.
_ANY_RE = re.compile(rf"({_NUM})\s?([KMBkmb])?(?![A-Za-z])")

# Magnitude spelled as a WORD. Publishers write "$55 billion"; our copy writes
# "$55B". Without this the two normalize to 55 and 55e9 and the guard reports a
# fabrication against a correct figure, which is what a live issue did on copy
# that actually shipped. These findings BLOCK the send now, so a false
# positive is a stopped issue at 6 AM, not a stray log line.
_WORD_MULT = {"thousand": 1_000, "million": 1_000_000,
              "billion": 1_000_000_000, "trillion": 1_000_000_000_000}
_WORD_MAG_RE = re.compile(
    rf"(?P<cur>\$\s?)?(?P<num>{_NUM})\s*(?P<mag>thousand|million|billion|trillion)\b",
    re.IGNORECASE)


def _val(num: str, suffix: str | None) -> float:
    v = float(num.replace(",", ""))
    if suffix:
        v *= _MULT[suffix.lower()]
    # Round the scaled result. 8.3 is not representable in binary, so 8.3 * 1e9
    # == 8300000000.000001 while an excerpt's literal "$8,300,000,000" parses to
    # 8300000000.0 — set membership then misses and a correct figure reads as a
    # fabrication. 2dp, not 6: the drift lands ON the 6th place. Nothing in this
    # copy (currency, percent, ratio) turns on a fraction of a cent.
    return round(v, 2)


def _excerpt_values(text: str) -> set[float]:
    """Every numeric value present in the excerpt, normalized.

    Both readings of a spelled-out magnitude are kept: "$55 billion" yields 55
    (the bare literal) AND 55e9, so it matches copy written either way."""
    t = text or ""
    vals = {_val(m.group(1), m.group(2)) for m in _ANY_RE.finditer(t)}
    vals |= {round(float(m.group("num").replace(",", "")) * _WORD_MULT[m.group("mag").lower()], 2)
             for m in _WORD_MAG_RE.finditer(t)}
    return vals


def _material_claims(text: str) -> list[tuple[str, list[float]]]:
    """Material numbers in the copy, as (display_string, [values_to_verify])."""
    out: list[tuple[str, list[float]]] = []
    _word_spans: set[int] = set()
    for m in _WORD_MAG_RE.finditer(text):
        # "$100 million" is a claim about 1e8, not about 100. The suffix group
        # cannot swallow a whole word, so without this the guard verifies the
        # wrong number and reports "$100" unsupported.
        val = round(float(m.group("num").replace(",", "")) * _WORD_MULT[m.group("mag").lower()], 2)
        if val:
            _word_spans.add(m.start("num"))
            out.append((m.group(0).strip(), [val]))
    for m in _CURRENCY_RE.finditer(text):
        if m.start(1) in _word_spans:
            continue  # already captured just above, at its true magnitude
        val = _val(m.group(1), m.group(2))
        if val == 0:
            continue  # "$0"/"free" is rhetorical framing, not a verifiable statistic
        out.append((m.group(0).strip(), [val]))
    for m in _PERCENT_RE.finditer(text):
        out.append((m.group(0).strip(), [_val(m.group(1), None)]))
    for m in _RATIO_RE.finditer(text):
        pairs = [(m.group(1), m.group(2)), (m.group(3), m.group(4))]
        for a, b in pairs:
            if a and b:
                out.append((m.group(0).strip(), [_val(a, None), _val(b, None)]))
    return out


def unsupported_numbers(claim_text: str, source_text: str) -> list[str]:
    """Material numbers in claim_text whose value(s) are absent from source_text.

    A value is 'supported' when it appears in the excerpt exactly.

    De-duplicated by normalized VALUE, not by display string. One fact written
    two ways ("$100M" in the summary, "$100 million" in an implication) is one
    problem and must cost one acknowledgement — a reviewer asked to tick eight
    boxes for three facts learns to tick without reading."""
    excerpt_vals = _excerpt_values(source_text)
    bad: list[str] = []
    seen: set[tuple[float, ...]] = set()
    for display, vals in _material_claims(claim_text):
        if any(v not in excerpt_vals for v in vals):
            key = tuple(sorted(vals))
            if key not in seen:
                seen.add(key)
                bad.append(display)
    return bad


def _story_text(story: dict) -> tuple[str, str]:
    """(claim_copy, source_excerpt) for a story, tolerant of field aliases."""
    summary = story.get("summary") or story.get("body") or ""
    impls = story.get("implications") or story.get("takeaways") or []
    headline = story.get("headline") or ""
    claim = " ".join([headline, summary, *impls])
    excerpt = story.get("source_excerpt") or ""
    return claim, excerpt


def check_story(story: dict) -> list[str]:
    """Findings for one story. Empty list = clean.

    A story that prints material numbers but saved no excerpt to check them
    against is flagged as unverifiable (closes the omit-the-excerpt loophole)."""
    claim, excerpt = _story_text(story)
    materials = _material_claims(claim)
    if not materials:
        return []
    if not excerpt.strip():
        figs = ", ".join(sorted({d for d, _ in materials}))
        return [f"no source excerpt saved to verify: {figs}"]
    return unsupported_numbers(claim, excerpt)


def check_payload(top_stories: list[dict]) -> list[str]:
    """Flatten findings across top stories into human-readable anomaly lines."""
    findings: list[str] = []
    for i, s in enumerate(top_stories or []):
        for fig in check_story(s):
            findings.append(
                f"Story {i+1:02d} '{(s.get('headline') or '')[:50]}' — "
                f"unsupported number not in its source excerpt: {fig}")
    return findings
