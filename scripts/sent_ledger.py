"""Sent-stories ledger — what each newsletter has already covered.

Reads the archived payloads the nightly send writes to review/sent/, so the
agent can be told "you already ran these — don't repeat them." First brick of
the Issue Memory & Archive feature; stdlib-only so it's safe to import anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SENT_DIR = REPO / "review" / "sent"


def recently_covered(newsletter: str, limit: int = 6) -> list[dict[str, str]]:
    """Return [{headline, url}] of stories featured in the most recent `limit`
    sent issues of this newsletter (newest first, de-duplicated by URL). Empty
    list if nothing has shipped yet. Never raises — a missing/garbled archive
    file is skipped, not fatal."""
    if not SENT_DIR.exists():
        return []
    from scripts import send_receipt as sr

    # Two things in this directory are NOT issues that reached readers:
    #   - <stem>.artifact.json / <stem>.receipt.json share the "<nl>-*.json"
    #     shape, so they used to consume slots in the `limit` window — asking
    #     for 6 recent issues quietly looked at ~2.
    #   - an issue whose send THREW. The payload is moved to review/sent/
    #     before the wire, so a failure leaves a file that looks shipped. Its
    #     stories reached nobody and must stay eligible; counting them covered
    #     is how content disappears without anyone deciding to drop it.
    candidates = [f for f in SENT_DIR.glob(f"{newsletter}-*.json")
                  if not f.name.endswith(sr.NON_PAYLOAD_SUFFIXES)]
    files = [f for f in sorted(candidates, reverse=True) if sr.shipped(f.stem, SENT_DIR)][:limit]
    covered: list[dict[str, str]] = []
    seen: set[str] = set()
    for f in files:
        try:
            payload = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        content = payload.get("content", payload)
        stories = (content.get("top_stories") or []) + (content.get("other_news") or [])
        for s in stories:
            url = (s.get("source_url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                covered.append({"headline": (s.get("headline") or "").strip(), "url": url})
    return covered


def covered_block(newsletter: str, limit: int = 6) -> str:
    """The prompt fragment listing already-covered stories, or '' if none."""
    covered = recently_covered(newsletter, limit)
    if not covered:
        return ""
    lines = "\n".join(f"- {c['headline']} ({c['url']})" for c in covered)
    return (
        "\n\nALREADY COVERED in recent issues — do NOT feature any of these stories "
        f"again; find genuinely new developments instead:\n{lines}"
    )
