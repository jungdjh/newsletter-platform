"""Candidate scoring — AI-rank the feed candidate pool by audience relevance.

Multi-source ingestion (scripts/tools/feeds.py) hands the agent ~25 fresh feed
candidates in recency order. Recency alone surfaces off-topic-but-fresh items
(e.g. The Verge's gaming feed in an AI-PM brief). This module adds the "pull
many, AI-rank, keep the best" step: one cheap Haiku call scores every candidate
on relevance to the audience brief, and we inject the ranked top-K instead.

Graceful by design: any failure falls back to the recency order — ranking is an
improvement, never a dependency. Returns candidates annotated with rank_score
(int 0-100, or None on fallback) and rank_reason.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import anthropic

RANK_MODEL = "claude-haiku-4-5"  # cheap + fast; this is a scoring pass, not authoring

_RANK_TOOL = {
    "name": "rank",
    "description": "Return a relevance score for every candidate lead.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rankings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "The candidate's number from the list."},
                        "score": {"type": "integer",
                                  "description": "0-100 relevance to THIS audience. 0 = off-topic / wrong "
                                                 "audience, 100 = must-cover. Fresh-but-off-topic scores LOW."},
                        "reason": {"type": "string", "description": "<= 12 words."},
                    },
                    "required": ["index", "score", "reason"],
                },
            },
        },
        "required": ["rankings"],
    },
}


def _fallback(candidates: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    return [{**c, "rank_score": None, "rank_reason": ""} for c in candidates[:top_k]]


def rank_candidates(
    candidates: list[dict[str, Any]],
    brief_text: str,
    *,
    top_k: int = 12,
    model: str = RANK_MODEL,
    client: anthropic.Anthropic | None = None,
) -> list[dict[str, Any]]:
    """Score feed candidates on relevance to the audience brief; return the top_k,
    each annotated with `rank_score` (0-100) and `rank_reason`. Falls back to
    recency order (rank_score=None) on any failure — never raises."""
    if not candidates:
        return []
    if client is None:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3)

    listing = "\n".join(
        f"{i}. {c.get('title', '')} — {c.get('source_domain', '')}"
        + (f" · {c['published_date']}" if c.get("published_date") else "")
        + (f" — {c['summary']}" if c.get("summary") else "")
        for i, c in enumerate(candidates)
    )

    try:
        resp = client.messages.create(
            model=model,
            # ~40 tokens per ranking object × up to 25 candidates needs headroom;
            # 1024 truncated the forced tool_use mid-array and dropped the rankings.
            max_tokens=2048,
            temperature=0,
            system=(
                "You score news leads by RELEVANCE to a newsletter's specific audience. "
                "Use the brief below to judge who the audience is and what is in/out of scope. "
                "A fresh story that's off-topic for this audience must score LOW. Score EVERY "
                "candidate by its index.\n\n" + brief_text
            ),
            tools=[_RANK_TOOL],
            tool_choice={"type": "tool", "name": "rank"},
            messages=[{
                "role": "user",
                "content": f"Score these {len(candidates)} candidate leads (0-100) for relevance:\n{listing}",
            }],
        )
        rankings = None
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "rank":
                rankings = (block.input or {}).get("rankings")
                break
        if not rankings:
            raise ValueError("no rankings in tool output")

        score_by_i: dict[int, tuple[int, str]] = {}
        for r in rankings:
            i = r.get("index")
            if isinstance(i, int) and 0 <= i < len(candidates):
                try:
                    score_by_i[i] = (int(r.get("score", 0)), str(r.get("reason", "")))
                except (TypeError, ValueError):
                    continue

        ranked = [
            {**c, "rank_score": score_by_i.get(i, (0, ""))[0], "rank_reason": score_by_i.get(i, (0, ""))[1]}
            for i, c in enumerate(candidates)
        ]
        ranked.sort(key=lambda c: c["rank_score"], reverse=True)
        return ranked[:top_k]
    except Exception as e:  # noqa: BLE001 — ranking is best-effort, never blocks a run
        print(f"[candidate_ranker] ranking failed ({type(e).__name__}: {e}) — using recency order",
              file=sys.stderr)
        return _fallback(candidates, top_k)
