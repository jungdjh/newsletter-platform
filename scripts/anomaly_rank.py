"""Rank anomaly notes: what needs David's decision vs what is just provenance.

The review console used to show every operational note under one ⚠ banner. On
the 2026-07-21 issue that was 7 notes, of which 6 needed no action — four were "the page
had no machine-readable date, so I confirmed it from the RSS feed / URL / search
snippet, and it passed." A gate the reviewer learns to skim is a gate that stops
working, so notes are split into two tiers:

  DECIDE — you have to make a call (freshness boundary, a repeat, a Top-3 short).
  NOTE   — provenance / FYI. Kept and shown, just not shouted.

Nothing is ever dropped: `split()` returns both lists and the caller renders
both. Never hiding how a date was established is a honesty requirement.

Severity comes from one of two places:
  1. An explicit {"severity": ..., "message": ...} dict — how code-generated
     anomalies declare themselves. Always wins.
  2. Heuristics over free-text prose (what the agent writes).

Unrecognized prose defaults to DECIDE on purpose: a note nobody taught us to
read is exactly the one worth surfacing. Better a spurious decision than a
silently buried problem.
"""
from __future__ import annotations

from typing import Any

DECIDE = "decide"
NOTE = "note"

# A freshness-boundary note always needs a human call, even though the pipeline
# also confirmed the date — "confirmed AND borderline" must not read as routine.
_BOUNDARY = ("cutoff", "boundary", "earliest_acceptable")

# Explicitly escalated by the producer.
_ESCALATE = ("review before sending", "short:", "floor short", " short —")

# The system worked: it hit a snag, resolved it, and said so.
#
# Safe to widen: _ESCALATE is checked BEFORE this, so a note that also says
# "review before sending" still lands in DECIDE. A phrase here only demotes a
# note that nothing else has already escalated.
_RESOLVED = (
    "confirmed",
    "approved",
    "no reader feedback",
    "falling back",
    "using audience default",
    "promoted",
    "excluded:",          # the place guard doing its job
    "rendered text-only",
    # Editorial notation the editor already harmonised, and explained. The
    # 2026-07-21 issue's 7th note ("full dollar notation to match verbatim source language … dropped
    # to qualitative to avoid compact-vs-full mismatch") is the system reporting
    # a style call it made correctly — it read as a DECIDE only because nothing
    # taught the classifier this shape, which is the fail-loud default doing its
    # job rather than a judgement about the note.
    "notation",
    "verbatim source",
)


def text(a: Any) -> str:
    """The human-readable message for an anomaly in either shape."""
    if isinstance(a, dict):
        return str(a.get("message") or a.get("text") or "").strip()
    return str(a).strip()


def severity(a: Any) -> str:
    """DECIDE or NOTE. Explicit dict severity wins; else heuristics; else DECIDE.

    A `block` severity (see scripts/guard_findings.py) ranks as DECIDE for
    display: a finding that halts the send must never render quieter than an
    ordinary decision."""
    if isinstance(a, dict):
        s = str(a.get("severity") or "").strip().lower()
        if s == "block":
            return DECIDE
        if s in (DECIDE, NOTE):
            return s

    low = text(a).lower()
    if not low:
        return NOTE                                   # empty note helps nobody
    if any(k in low for k in _BOUNDARY):
        return DECIDE
    if any(k in low for k in _ESCALATE):
        return DECIDE
    if any(k in low for k in _RESOLVED):
        return NOTE
    return DECIDE


def split(anomalies: list[Any] | None) -> tuple[list[str], list[str]]:
    """(decides, notes) as display strings, original order preserved."""
    decides: list[str] = []
    notes: list[str] = []
    for a in anomalies or []:
        t = text(a)
        if not t:
            continue
        (decides if severity(a) == DECIDE else notes).append(t)
    return decides, notes


def banner(decides: list[str], notes: list[str]) -> str:
    """Headline copy for the review console."""
    if decides and notes:
        return (f"{len(decides)} item{'' if len(decides) == 1 else 's'} need"
                f"{'s' if len(decides) == 1 else ''} your call · {len(notes)} note"
                f"{'' if len(notes) == 1 else 's'}")
    if decides:
        return (f"{len(decides)} item{'' if len(decides) == 1 else 's'} need"
                f"{'s' if len(decides) == 1 else ''} your call")
    if notes:
        return f"{len(notes)} note{'' if len(notes) == 1 else 's'} — nothing needs a decision"
    return ""
