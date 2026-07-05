#!/usr/bin/env python3
"""Brief generator — the platform core.

Turns an audience description ("AI PMs", "nursing students") into a compact
operational brief the existing agent loop can run — same shape as a
hand-written audience brief.

A brief = VARIABLE parts (audience, relevance gate, tracks, geography, tone,
Korean?) wrapped around a FIXED engine contract (output buckets, field
schemas, fact discipline). The LLM generates the variable spec; assemble_brief
templates it into the fixed contract. This is what makes the system a
*platform* instead of three hardcoded newsletters.

Two functions:
  generate_brief_spec(audience) -> dict   # 1 LLM call; needs ANTHROPIC_API_KEY
  assemble_brief(spec) -> str             # pure, offline-testable

CLI:
  python -m scripts.brief_generator --audience "AI PMs" --out briefs/ai-pms.json
  python -m scripts.brief_generator --from-spec briefs/ai-pms.json   # assemble only
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

MODEL = "claude-sonnet-4-6"

# ---- spec the generator must produce (also the offline-test contract) -------
SPEC_SCHEMA = {
    "type": "object",
    "required": ["slug", "display_name", "category_main", "subtitle",
                 "implications_label", "footer_tagline", "theme", "audience", "in_scope",
                 "out_scope", "sources", "relevance_gate", "tracks", "geography", "wants_korean", "tone"],
    "properties": {
        "slug": {"type": "string", "description": "lowercase-hyphen id, e.g. 'ai-pms'"},
        "display_name": {"type": "string", "description": "masthead wordmark, e.g. 'The AI PM Brief'"},
        "category_main": {"type": "string", "description": "short category label, e.g. 'AI Product'. NEVER an employer name."},
        "category_sub": {"type": "string", "description": "category suffix, default 'Intel'"},
        "subtitle": {"type": "string", "description": "one-line tagline under the wordmark, e.g. 'The intelligence brief on models, agents & shipping AI products'"},
        "implications_label": {"type": "string", "description": "label for the implications block, e.g. 'For AI PMs'"},
        "footer_tagline": {"type": "string", "description": "footer line, e.g. 'Curated for AI product managers.'"},
        "audience": {"type": "string", "description": "2-4 sentences: who they are, what they already know, what they need from a brief"},
        "in_scope": {"type": "array", "items": {"type": "string"}, "description": "5-8 topic areas that ARE relevant"},
        "out_scope": {"type": "array", "items": {"type": "string"}, "description": "4-6 adjacent things that are WRONG AUDIENCE (the no-go list)"},
        "sources": {"type": "array", "items": {"type": "string"},
                    "description": "8-15 trusted BARE domains (no scheme/path, e.g. 'techcrunch.com', "
                                   "'reuters.com') this audience relies on — authoritative outlets, trade "
                                   "press, and primary sources/regulators for the field. These become the "
                                   "web_search ALLOWLIST: only these (plus a general tier-1 baseline) can be "
                                   "cited. EXCLUDE SEO/affiliate/'best-of'/single-author blog-farm sites."},
        "feeds": {"type": "array", "items": {"type": "string"},
                  "description": "4-8 RSS/Atom FEED URLs (full https URLs, e.g. 'https://techcrunch.com/feed/') "
                                 "for the `sources` above — the outlets' news feeds. Used to pre-fetch fresh "
                                 "article candidates client-side (recovers outlets web_search can't crawl). "
                                 "Best-effort: give your best-guess canonical feed URL per major source; wrong "
                                 "or dead feeds are skipped at runtime. Optional but strongly preferred."},
        "relevance_gate": {"type": "string", "description": "one yes/no question applied to every story, e.g. 'Would an AI PM change their roadmap after reading this?'"},
        "tracks": {"type": "array", "minItems": 5, "maxItems": 6,
                   "items": {"type": "object", "required": ["name", "desc"],
                             "properties": {"name": {"type": "string"}, "desc": {"type": "string"}}}},
        "geography": {"type": "string", "description": "e.g. 'US-first', 'Global', 'UK-first'"},
        "wants_korean": {"type": "boolean", "description": "true only if the audience is Korean-native or bilingual"},
        "self_coverage": {"type": ["string", "null"], "description": "the audience's own product/org to exclude as self-coverage, or null"},
        "tone": {"type": "string", "description": "1 sentence on voice, e.g. 'direct, practitioner-to-practitioner, no hype'"},
        "theme": {
            "type": "object",
            "description": "Brand colors fitting THIS audience — a distinct visual identity of its own, not borrowed from another brand. Pick colors that feel right for the field (e.g. indigo/violet for AI/tech, warm clinical green for nursing).",
            "required": ["bg_dark", "primary", "accent"],
            "properties": {
                "bg_dark": {"type": "string", "description": "deep DARK hex for the masthead/big-signal/footer backdrop (white text sits on it), e.g. '#13122B'"},
                "primary": {"type": "string", "description": "saturated brand hex for links, dividers, track tags on white, e.g. '#4F46E5'"},
                "accent": {"type": "string", "description": "bright accent hex for numerals/CTA on the dark cards, e.g. '#A78BFA'"},
                "ink": {"type": "string", "description": "near-black body text hex, default '#16181D'"},
            },
        },
    },
}

# ---- FIXED engine contract (identical across every audience) ----------------
_OUTPUT_CONTRACT = """\
## Output contract — two buckets

1. **Top Stories** — 1, 2, OR 3 entries. Cap 3, floor 1. First is the Big
   Signal. Each gets full treatment: track, dateline, hero image
   (vision-checked), headline, summary, source_excerpt, 2 implications, {kr}source URL.
2. **Other news** — 0 to 5 scan-only items. Each: track, headline, subtitle
   (one-line dek), source_url. NO summary, NO implications, NO hero."""

_FACT_DISCIPLINE = """\
## Fact discipline — non-negotiable, audit before submitting

After drafting, audit EVERY claim against your `source_excerpt`. If a claim
isn't supported by what you've quoted, expand the excerpt or cut the claim.
Do NOT add specific dates, figures, named entities, or quotes unless they
appear verbatim in the source. The Sr. Editor verifies every claim against
the excerpt.

## Freshness — non-negotiable
Run context provides TODAY and EARLIEST_ACCEPTABLE_DATE. Every story's
`published_at` MUST be on or after EARLIEST_ACCEPTABLE_DATE — check it on
every web_fetch, reject stale. Never frame stale news as current.

## Anti-padding
Better to ship 1 strong Top Story + 0 Other News than to pad. If a story
fails fact-discipline, the relevance gate, or freshness, DROP it."""

_FIELDS_BASE = """\
## Top Story fields
- **track**: one of the tracks above
- **headline**: sentence case, 6-10 words. Compact dollar notation ("$11B"),
  numerals for counts/percentages, no colons, no hype.
- **summary**: 1-2 SHORT sentences, each <= 18 words so each fits ~2 lines.
  Break a dense fact into two short sentences rather than one long run-on. Fact-led.
- **published_at**: pass through web_fetch's value verbatim; null if missing.
- **dateline**: where the news happened, e.g. "Cupertino · Brussels"
- **hero_image_url**: og:image, ONLY after passing og_image_check; else null.
- **source_url**: direct link to original (no aggregators)
- **source_excerpt**: 2-4 verbatim sentences from the source containing the
  facts your summary + implications rest on. Copy-paste, do not paraphrase.
- **implications**: exactly 2 bullets, 8-14 words each. Concrete moves for
  the audience — no periods at end.
{korean_field}
## Other News item fields (scan-only — MUST each fit ONE line)
- **track**, **headline** (6-9 words, fits one line), **subtitle** (<= 11 words,
  fits one line — no second clause that spills over), **source_url**
- **published_at**: pass through web_fetch's value verbatim; null if unknown.
  Other News obeys the SAME freshness gate as Top Stories — if published_at is
  before EARLIEST_ACCEPTABLE_DATE, DROP the item. Do not include-and-flag stale
  items; just leave them out."""

_KOREAN_FIELD = """\
- **korean_takeaway**: REQUIRED. 1-2 short Korean lines, 명사형 종결, 개조식,
  executive-briefing tone, <= 80 chars. Every Top Story has this.
"""

_QUERY_FRESHNESS = """\
## Query freshness — the search tool has NO date filter
Put the current month + year in your queries (e.g. "<topic> February 2026") to
bias toward fresh results. The tool can't constrain by date for you, so verify
`published_at` on every web_fetch and reject anything older than
EARLIEST_ACCEPTABLE_DATE."""


def assemble_brief(spec: dict) -> str:
    """Pure: spec -> full compact brief string (engine-compatible). No API."""
    kr = spec.get("wants_korean", False)
    tracks_line = " · ".join(t["name"] for t in spec["tracks"])
    tracks_block = "\n".join(f"- **{t['name']}**: {t['desc']}" for t in spec["tracks"])
    in_scope = "\n".join(f"- {x}" for x in spec["in_scope"])
    out_scope = "\n".join(f"- {x}" for x in spec["out_scope"])
    self_cov = spec.get("self_coverage")
    self_cov_block = (
        f"\n## Self-coverage filter\nExclude stories whose primary subject is "
        f"**{self_cov}** unless they carry external signal (peer-reviewed study, "
        f"analyst data, regulator action, independent adoption data).\n"
        if self_cov else ""
    )
    korean_field = _KOREAN_FIELD if kr else ""
    kr_treatment = "Korean takeaway, " if kr else ""

    sources = spec.get("sources") or []
    if sources:
        sources_lines = "\n".join(f"- {d}" for d in sources)
        sources_block = (
            "## Trusted sources — web_search is allowlisted to these (+ general tier-1 wires)\n"
            "Only these outlets can be returned by search. Bias queries toward them by\n"
            "name. If a story surfaces only outside this list, it didn't clear the source\n"
            f"bar — drop it.\n{sources_lines}\n\n"
        )
    else:
        sources_block = ""

    return f"""\
You are the writer agent for **{spec['display_name']}**, a market-intelligence
brief for: {spec['audience']}
Generate this issue end-to-end and submit via the submit_newsletter tool.
Tone: {spec['tone']}

## CRITICAL — who this is for
This brief serves the audience above. Stay in their world.

**In scope (relevant):**
{in_scope}

**Out of scope (interesting but WRONG AUDIENCE — drop these):**
{out_scope}

**The relevance gate (apply to every story):**
> "{spec['relevance_gate']}"
If the honest answer is no, DROP the story.
{self_cov_block}
## Tracks (use for both Top Stories and Other News)
{tracks_line}

{tracks_block}

{_OUTPUT_CONTRACT.format(kr=kr_treatment)}

{_FIELDS_BASE.format(korean_field=korean_field)}

{sources_block}## Geography priority
Primary market: **{spec['geography']}**. A Top Story (especially the Big
Signal) should be {spec['geography']}-relevant; off-geography items go to
Other News at best, often dropped.

{_QUERY_FRESHNESS}

{_FACT_DISCIPLINE}
"""


def generate_brief_spec(audience: str) -> dict[str, Any]:
    """1 LLM call: audience description -> structured spec. Needs ANTHROPIC_API_KEY."""
    import os
    import anthropic  # lazy
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=4)
    sys_prompt = (
        "You design newsletter briefs. Given an audience, produce a JSON spec that "
        "defines a sharp, well-scoped market-intelligence brief for them. The spec "
        "must match this JSON Schema exactly:\n" + json.dumps(SPEC_SCHEMA) +
        "\n\nBe specific and opinionated: tracks should be the 5-6 categories that "
        "audience actually cares about; out_scope should name the adjacent-but-wrong "
        "topics they'd be annoyed to see; the relevance_gate is one concrete yes/no "
        "question. For `sources`, list the 8-15 bare domains a discerning reader in "
        "this field actually trusts — the outlets, trade press, and primary sources "
        "they'd cite — and deliberately leave OUT SEO/affiliate/blog-farm sites. "
        "For `feeds`, give best-guess canonical RSS/Atom feed URLs (full https) for the "
        "most important of those sources — these pre-fetch fresh article candidates. "
        "Set wants_korean true ONLY for Korean-native/bilingual audiences. "
        "Return ONLY the JSON object."
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=2048, temperature=0.2,
        system=sys_prompt,
        messages=[{"role": "user", "content": f"Audience: {audience}"}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--audience", help="audience description → generate spec (LLM) + assemble")
    g.add_argument("--from-spec", help="assemble a brief from an existing spec JSON (no API)")
    ap.add_argument("--out", help="write the spec JSON here (with --audience)")
    ap.add_argument("--print-brief", action="store_true", help="print the assembled brief")
    args = ap.parse_args()

    if args.from_spec:
        spec = json.loads(Path(args.from_spec).read_text())
    else:
        spec = generate_brief_spec(args.audience)
        if args.out:
            Path(args.out).write_text(json.dumps(spec, indent=2, ensure_ascii=False))
            print(f"[brief] wrote spec → {args.out}")

    brief = assemble_brief(spec)
    print(f"[brief] {spec['display_name']} · {len(spec['tracks'])} tracks · "
          f"korean={spec.get('wants_korean')} · {len(brief)} chars")
    if args.print_brief or args.from_spec:
        print("\n" + "=" * 70 + "\n" + brief)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
