"""Claude tool-use loop that generates a newsletter's structured content.

The loop:
1. Sends the newsletter prompt + run context to Claude.
2. Claude calls tools (web_search [server-side], web_fetch, og_image_check,
   notion_feedback) until it has enough to assemble the content.
3. Returns a structured payload Claude emits as its final assistant turn.

Web search is Anthropic's server-side `web_search_20250305` tool — runs on
Anthropic's infrastructure, no client-side execution needed. Source quality is
enforced at the search layer: if the newsletter has a curated source list
(get_sources), the tool runs with an `allowed_domains` ALLOWLIST (only vetted
outlets can be returned); otherwise it falls back to the `blocked_domains`
blocklist. See build_tool_schemas. Replaced the custom Brave wrapper on
2026-05-26 to drop the BRAVE_SEARCH_API_KEY dependency.

The expected final-output shape (Claude is instructed to produce this JSON):
    {
      "top3": [{
          "headline": "...",
          "summary": "...",
          "track": "...",
          "filed_time": "07:42 CT",
          "dateline": "Cupertino · Brussels",
          "hero_image_url": "https://..." | null,
          "source_url": "https://...",
          "korean_takeaway": "..." | null,   # only when the audience enables Korean
          "implications": ["...", "...", "..."]  # 2 EN bullets (+ 1 KO if enabled)
      }, ...],
      "watchlist": [{"tag": "Filing", "headline": "...", "source": "..."}, ...],
      "also_noted": [{"headline": "...", "source": "..."}, ...],
      "anomalies": ["any operational notes worth surfacing", ...]
    }
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import anthropic

from .tools import web_fetch, og_image, notion_client, feeds as feed_ingest
from .compact_prompts import make_compact_prompt, get_sources, get_feeds, get_freshness_window

# Source-quality blocklist for the server-side web_search tool. Anthropic's
# web_search accepts these as `blocked_domains` and never returns results from
# them. Note: path-based blocks (e.g. marketwatch.com/press-release) aren't
# supported — domain-only entries here.
#
# 2026-07-29: the press-release WIRES were removed from this list. They were
# blocked on the theory that "Vendor X announces Y" without external signal is
# fabrication-tempting. In practice the block did something worse: it made the
# briefs structurally unable to see product launches, because on launch day the
# wire or the company newsroom is frequently the ONLY source. What survived the
# filter was policy, litigation and funding rounds, so two of the briefs drifted
# into reading like trade-policy digests and the reader stopped sending them. See the launch rules in the briefs.
#
# The honesty control moved rather than disappeared: vendor sources are now
# accepted for WHAT shipped (product, price, date, named features) and still
# refused for HOW MUCH IT MATTERS (adoption, market share, accuracy, "first
# ever"). That distinction is enforced in the briefs and by the Sr. Editor,
# which is the right place for it — a judgement about a claim, not a domain.
#
# Low-signal AGGREGATORS stay blocked. That block is about syndication noise
# and losing the original outlet, not about PR.
WEB_SEARCH_BLOCKED_DOMAINS = [
    "yahoo.com",        # generic syndication — re-search the original outlet
    "msn.com",
    "headtopics.com",
    "newsbreak.com",
    "smartcompany.com.au",
]

# Model choice:
# - Agent uses Sonnet 4.6. Tried Haiku first — it hallucinated product names
#   ("Antigravity 2.0", "Gemini 3.5 Flash", unverified subscriber counts).
#   Newsletters need zero fabrication, so the cost/speed savings aren't worth
#   the editorial risk. Sonnet's fact discipline is much better.
# - Upgraded 4.5 → 4.6 on 2026-05-27 per Anthropic cookbook reference. Unlocks
#   web_search_20260209 (dynamic source filtering) at same per-token price.
# - Sr. Editor (sr_editor.py) also Sonnet — single call per run, low TPM cost.
AGENT_MODEL = "claude-sonnet-4-6"
MAX_AGENT_TURNS = 25  # was 40; tightened to fit workflow timeout

# Pacing for Tier 1 TPM (30K input/min on Sonnet). With our compact prompt +
# accumulating message history, each turn is ~5-12K input tokens.
# 12-sec sleep keeps cumulative input under 30K/min in steady state.
SLEEP_BETWEEN_TURNS_SEC = 12
SDK_MAX_RETRIES = 6
# Cap per-turn output. Sonnet rarely uses >2K tokens for tool-use turns;
# 4096 is plenty and cuts ~5-15 sec of generation time per turn vs 8192.
MAX_RESPONSE_TOKENS = 8192  # bumped 2026-05-25 from 4096 — payloads with source_excerpt + korean_takeaway across 3 stories were occasionally truncating mid-serialization

# Tier-1 baseline allowlist — broad-coverage outlets unioned into EVERY audience's
# allowlist (in build_tool_schemas) so a genuinely consequential story isn't missed
# just because it broke on a general wire outside the audience's niche source list.
#
# CRITICAL crawler constraint (learned live, 2026-06-07): Anthropic's web_search
# user agent CANNOT read many paywalled papers of record — NYT, WSJ, FT, Reuters,
# AP, The Economist, Wired, Ars Technica, The Verge all block it. With an
# ALLOWLIST, naming an inaccessible domain 400s the WHOLE request
# (invalid_request_error: "not accessible to our user agent"). So this baseline is
# intentionally small (only domains confirmed crawler-accessible), and run_agent
# self-heals by pruning any audience source the crawler can't reach (see
# _parse_inaccessible_domains). Don't re-add majors here without confirming they
# return a 200 on a live allowlist run.
# Confirmed crawler-accessible on a live allowlist run (2026-06-07). Kept small
# on purpose — most papers of record block the crawler. NOTE: theguardian.com is
# NOT here despite being a paper of record — it blocks the Anthropic crawler too
# (confirmed pruned live). Its free content is reachable only via the Guardian
# Open Platform API, which would be a separate client-side source tool (parked).
TIER1_BASELINE_DOMAINS = [
    "bloomberg.com",
    "axios.com",
    "cnbc.com",
]


_INACCESSIBLE_DOMAINS_RE = re.compile(r"not accessible to our user agent:\s*\[([^\]]*)\]")


def _parse_inaccessible_domains(message: str) -> list[str]:
    """Extract the domains named in a web_search allowlist 400, normalized.

    The API rejects an allowlist that contains any domain its crawler can't read,
    listing them all: "...not accessible to our user agent: ['ft.com','wsj.com']".
    Returns [] if the message isn't that error."""
    m = _INACCESSIBLE_DOMAINS_RE.search(message or "")
    if not m:
        return []
    return [_normalize_domain(p.strip().strip("'\"")) for p in m.group(1).split(",") if p.strip()]


def _normalize_domain(raw: str) -> str:
    """Coerce a source entry to the bare form Anthropic web_search expects:
    no scheme, no leading 'www.', no trailing slash. Subpaths are left intact
    (the tool supports them). Defensive against spec entries like
    'https://www.example.com/' that would otherwise 400 (invalid_tool_input)."""
    s = (raw or "").strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.rstrip("/")
    if s.startswith("www."):
        s = s[4:]
    return s


def _normalize_domains(domains: list[str]) -> list[str]:
    """Normalize, dedupe, and sort a domain list. Sorting keeps the rendered tool
    schema byte-stable within a run so the cached tools+system prefix holds."""
    out: set[str] = set()
    for d in domains:
        nd = _normalize_domain(d)
        if nd:
            out.add(nd)
    return sorted(out)


# Static (non-web_search) tools. web_search is constructed per-run by
# build_tool_schemas() so its domain filter (allowlist vs blocklist) can vary by
# newsletter. cache_control still lands on the last tool (submit_newsletter),
# anchoring a cached [tools + static brief] prefix across the agent loop.
_STATIC_TOOL_SCHEMAS = [
    {
        "name": "web_fetch",
        "description": (
            "Fetch an article URL and extract its readable body and og:image. "
            "Use this to verify story facts before writing the summary, and to "
            "get the candidate hero image URL. "
            "The response includes `published_at` — ALWAYS check this against "
            "EARLIEST_ACCEPTABLE_DATE from your run context. If the article is "
            "older than that, REJECT it and find a fresher source. Stale news "
            "framed as current is the #1 failure mode."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The article URL to fetch."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "og_image_check",
        "description": (
            "Vision-check whether an image is relevant to a story headline. "
            "Returns 'approve', 'reject', or 'skip' (image too large or "
            "unfetchable). Use this on the og_image_url from web_fetch BEFORE "
            "including the image in the final payload."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string"},
                "headline": {"type": "string"},
                "track": {"type": "string"},
                "image_size_bytes": {"type": "integer", "description": "From web_fetch.og_image_size_bytes if known."},
            },
            "required": ["image_url", "headline"],
        },
    },
    {
        "name": "notion_get_feedback",
        "description": (
            "Fetch recent reader feedback for this newsletter since a cutoff "
            "timestamp. Use this to inform tone or topic choices."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "newsletter": {"type": "string", "description": "The audience short-name."},
                "since_iso": {"type": "string", "description": "ISO 8601 timestamp."},
            },
            "required": ["newsletter", "since_iso"],
        },
    },
    {
        "name": "submit_newsletter",
        "description": (
            "Submit the final structured newsletter payload. Call this ONCE "
            "when you've assembled the Top Stories + Other News items. "
            "This ends the agent loop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "top_stories": {
                    "type": "array",
                    "description": (
                        "1 to 3 Top stories. First is Big Signal (most consequential). "
                        "Each gets full treatment. NEVER pad — ship 1 or 2 if news is thin. "
                        "Cap is 3, floor is 1."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "headline": {"type": "string"},
                            "summary": {"type": "string", "description": "1-2 sentences, ≤ 35 words. Every claim must trace to source."},
                            "track": {"type": "string"},
                            "published_at": {
                                "type": ["string", "null"],
                                "description": (
                                    "Pass through the `published_at` value web_fetch returned for the source article, "
                                    "verbatim. DO NOT reformat — the renderer parses it and converts to CT for display. "
                                    "If web_fetch didn't return a published_at, set null (and see freshness_confirmed). "
                                    "Never invent or guess a publish time."
                                ),
                            },
                            "freshness_confirmed": {
                                "type": ["boolean", "null"],
                                "description": (
                                    "Set true ONLY when published_at is null (the source gave no machine-readable "
                                    "date) but your web_search results independently confirm the story is within the "
                                    "freshness window — e.g. a search result showing 'N hours ago' or an explicit recent "
                                    "date. This lets a genuinely-fresh story from a date-less source still qualify as the "
                                    "Big Signal (lead) instead of being demoted to a lower slot. If you cannot confirm "
                                    "recency, leave it false/null — never guess. Ignored when published_at is present."
                                ),
                            },
                            "dateline": {"type": "string", "description": "Where the news happened, e.g. 'Cupertino · Brussels'"},
                            "hero_image_url": {"type": ["string", "null"]},
                            "source_url": {"type": "string"},
                            "source_excerpt": {
                                "type": "string",
                                "description": (
                                    "2-4 verbatim sentences from the source article that contain the key facts your summary and implications "
                                    "are based on. Quote directly — do not paraphrase. The Sr. Editor verifies every claim in summary + "
                                    "implications against this excerpt, so include the passage(s) that support your strongest claims (numbers, "
                                    "named entities, legal framing, quotes). If a claim isn't supported by anything you can quote, drop it before "
                                    "submitting. This field is what lets the editor stop flagging valid claims as fabricated."
                                ),
                            },
                            "implications": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Exactly 2 English bullets. 8-14 words each. Concrete but GENTLE moves for the reader/audience — frame as exploratory recommendations (lead with a soft verb: Explore, Consider, Watch, Monitor, Weigh). NEVER hard directives: no 'must', 'need to', 'require', 'should', or bare commands."
                            },
                            "korean_takeaway": {
                                "type": ["array", "null"],
                                "items": {"type": "string"},
                                "description": (
                                    "Include only when the audience brief calls for a Korean takeaway; otherwise null. "
                                    "A Korean mirror of the implications: exactly one short Korean line per English implication "
                                    "bullet (so 2 lines), 명사형 종결 (~됨, ~함, ~임), 개조식, ≤ 40 chars each. For Korean-native readers."
                                ),
                            },
                        },
                        "required": ["headline", "summary", "track", "published_at", "dateline", "source_url", "source_excerpt", "implications", "korean_takeaway"],
                    },
                    "minItems": 1,
                    "maxItems": 3,
                },
                "other_news": {
                    "type": "array",
                    "description": "0 to 5 scan-only items. Each: track tag + headline + subtitle + source URL. NO summary, NO implications, NO hero. These are skim items — headline+subtitle must do all the work.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "track": {"type": "string", "description": "One of the newsletter's tracks (e.g. App Stores, Developers, Apps)"},
                            "headline": {"type": "string", "description": "Sentence case, 6-10 words, no colons"},
                            "subtitle": {"type": "string", "description": "One-line dek that adds context. ≤ 14 words. This is the only post-headline context."},
                            "published_at": {
                                "type": ["string", "null"],
                                "description": (
                                    "Pass through web_fetch's published_at for this item, verbatim; null if unknown. "
                                    "Other News obeys the SAME freshness gate as Top Stories — items published before "
                                    "EARLIEST_ACCEPTABLE_DATE are dropped. Never invent or guess a date."
                                ),
                            },
                            "source_url": {"type": "string", "description": "The article URL (for hyperlinking the headline)"},
                            "number_source": {
                                "type": ["string", "null"],
                                "description": (
                                    "REQUIRED IF the headline or subtitle states a statistic (a $ amount, a "
                                    "percentage, or an N-of-M ratio): paste the ONE verbatim sentence from the "
                                    "source that contains that exact figure. Null when the item states no statistic. "
                                    "Do not paraphrase — if you can't quote a sentence that contains the number, "
                                    "remove the number and keep the item qualitative."
                                ),
                            },
                        },
                        "required": ["track", "headline", "subtitle", "source_url"],
                    },
                    "maxItems": 5,
                },
                "anomalies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Operational notes — image omissions, blocked URLs, thin news day, etc.",
                },
            },
            "required": ["top_stories"],
        },
        # Cache_control on the last tool cascades back through all preceding
        # tools, capturing the ~1.5K tokens of static tool schemas + the
        # web_search domain filter. Combined with the system-block cache
        # (static_brief), this caches everything up to the dynamic context.
        "cache_control": {"type": "ephemeral"},
    },
]


# Bench: reserve stories the agent drafts alongside the Top 3, so the reviewer
# can backfill a dropped top story without a regenerate. Same full-story shape
# as top_stories (reuse the exact item object — DRY, and it stays in sync).
for _t in _STATIC_TOOL_SCHEMAS:
    if _t.get("name") == "submit_newsletter":
        _submit_props = _t["input_schema"]["properties"]
        _submit_props["bench"] = {
            "type": "array",
            "description": (
                "Up to 3 RESERVE stories, ranked best-first, same full shape as "
                "top_stories. Held back so the reviewer can backfill a dropped top "
                "story without regenerating. Draft real, verified reserves from your "
                "next-strongest candidates — never filler. Omit or leave empty on a "
                "thin news day; never invent a reserve to hit the count."
            ),
            "items": _submit_props["top_stories"]["items"],
            "maxItems": 3,
        }
        break


def build_tool_schemas(allowed_domains: list[str] | None = None) -> list[dict[str, Any]]:
    """Assemble the agent's tool list for one run.

    web_search uses an ALLOWLIST when `allowed_domains` is non-empty — only those
    domains can ever be returned (the strong source-quality lever). Otherwise it
    falls back to the legacy `blocked_domains` filter. The two are mutually
    exclusive per the API, so the allowlist subsumes the blocklist: press-release
    wires and aggregators simply aren't on it. Domains are normalized + sorted for
    a byte-stable cached prefix.
    """
    web_search: dict[str, Any] = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 10,
    }
    norm = _normalize_domains(allowed_domains) if allowed_domains else []
    if norm:
        web_search["allowed_domains"] = norm
    else:
        web_search["blocked_domains"] = WEB_SEARCH_BLOCKED_DOMAINS
    return [web_search] + _STATIC_TOOL_SCHEMAS


from scripts.freshness import freshness_floor  # noqa: E402 — cadence-aware cutoff
from scripts.sent_ledger import covered_block as _covered_block  # noqa: E402 — no-repeat ledger


def run_agent(
    *,
    newsletter: str,
    prompt_text: str,
    state: dict[str, Any],
    today_date_iso: str,
    issue_number: int,
    editor_must_fix: list[str] | None = None,
) -> dict[str, Any]:
    """Run the newsletter-generation agent loop. Returns the payload submitted
    via the submit_newsletter tool.

    Raises RuntimeError if the agent fails to submit within MAX_AGENT_TURNS.
    """
    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        max_retries=SDK_MAX_RETRIES,
    )

    # Split system into two blocks so the static brief (~2K tokens) caches
    # across all 25 turns of the agent loop. Render order is tools → system,
    # so cache_control on the first system block anchors a cached prefix of
    # [tools + static brief]. The dynamic context (~150 tokens, with today's
    # date / state / editor concerns) comes after — varies per request but
    # doesn't invalidate the cache.
    #
    # Cost math at Sonnet 4.5 ($3/M input): old = ~$0.09/run on the brief
    # (2K tokens × 15 turns × $3/M). New = ~$0.016/run (1 write @ 1.25x +
    # ~14 reads @ 0.1x). Savings ~$0.07/run = ~$1.80/mo at 6 runs/week.
    static_brief = make_compact_prompt(newsletter)
    win_min, win_max = get_freshness_window(newsletter)
    dynamic_context = _build_dynamic_context(state, today_date_iso, issue_number, editor_must_fix,
                                             win_min, win_max)

    # Resolve the per-audience source allowlist (sources + tier-1 baseline). If a
    # newsletter has no curated sources, allowed_domains is None and build_tool_schemas
    # falls back to the legacy blocklist — nothing regresses.
    audience_sources = get_sources(newsletter)
    allowed_domains = (audience_sources + TIER1_BASELINE_DOMAINS) if audience_sources else None
    tool_schemas = build_tool_schemas(allowed_domains)
    if allowed_domains:
        n = len(tool_schemas[0].get("allowed_domains", []))
        print(f"[agent_loop] web_search ALLOWLIST: {n} domains ({len(audience_sources)} audience + tier-1 baseline)")
    else:
        print(f"[agent_loop] web_search BLOCKLIST fallback ({len(WEB_SEARCH_BLOCKED_DOMAINS)} blocked) — no sources for '{newsletter}'")

    # Multi-source ingestion: pre-fetch fresh candidates from the audience's curated
    # feeds (client-side — reaches outlets web_search can't crawl; web_fetch reads them).
    # Injected as LEADS into the opening message; the agent must web_fetch to verify.
    # Robust: dead feeds are skipped, never abort the run.
    _feed_earliest = freshness_floor(today_date_iso, state.get("last_run_at"), win_min, win_max)
    feed_block = ""
    feed_urls = get_feeds(newsletter)
    if feed_urls:
        try:
            candidates, feed_warnings = feed_ingest.fetch_feed_candidates(
                feed_urls, _feed_earliest, limit=25)
            print(f"[agent_loop] feed candidates: {len(candidates)} from {len(feed_urls)} feeds "
                  f"({len(feed_warnings)} skipped)")
            for _w in feed_warnings:
                print(f"[agent_loop]   feed skip: {_w}")
            # AI-rank the pool by relevance (cheap Haiku call); keep the best.
            # Recency alone surfaces off-topic-but-fresh items; ranking fixes that.
            if candidates:
                from .candidate_ranker import rank_candidates
                candidates = rank_candidates(candidates, static_brief, top_k=12, client=client)
                _scores = [c["rank_score"] for c in candidates if isinstance(c.get("rank_score"), int)]
                if _scores:
                    print(f"[agent_loop] ranked feed candidates → top {len(candidates)} "
                          f"(scores {min(_scores)}..{max(_scores)})")
            feed_block = _build_feed_block(candidates)
        except Exception as _e:  # noqa: BLE001 — ingestion must never block a run
            print(f"[agent_loop] feed ingestion failed (continuing web_search-only): {type(_e).__name__}: {_e}")

    # The human editor's review-console edits from the last issue, wired into the
    # prompt so corrections (dropped stories, reworded implications) actually reach
    # the next issue instead of being stored to review/feedback/ and never read.
    edit_feedback_block = _build_edit_feedback_block(newsletter)

    system_blocks = [
        {
            "type": "text",
            "text": static_brief,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": dynamic_context,
        },
    ]

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Generate today's newsletter issue. The full SKILL.md prompt is in the system message above. "
                f"Today: {today_date_iso}. Issue number to assign: {issue_number}. "
                f"Last run: {state.get('last_run_at', 'never')}. "
                f"Last feedback check: {state.get('last_feedback_check_at', 'never')}. "
                f"\n\nBegin by checking recent feedback (notion_get_feedback), then research news across the use-case tracks, "
                f"then assemble the Top 3 + watchlist + also-noted, then call submit_newsletter ONCE with the complete payload."
                + feed_block
                + edit_feedback_block
                + _covered_block(newsletter)
            ),
        }
    ]

    final_payload: dict[str, Any] | None = None
    # Stash article bodies the agent fetches, keyed by URL, so the review
    # console's right pane shows what the agent actually read instead of
    # re-fetching at review time (which breaks on paywalls / link-rot).
    fetched_bodies: dict[str, dict[str, Any]] = {}

    def _create_response():
        """messages.create with self-healing allowlist pruning. If web_search
        rejects the request because some allowed_domains aren't crawler-accessible,
        drop exactly those (the error names them), rebuild tool_schemas, and retry.
        An empty allowlist after pruning falls back to the blocklist."""
        nonlocal tool_schemas, allowed_domains
        for _ in range(4):
            try:
                # Low temperature for factual zero-fabrication content. Matches the
                # Anthropic cookbook reference (patterns/agents/util.py) and reduces
                # variance run-to-run, which makes A/B comparison of fixes possible.
                _resp = client.messages.create(
                    model=AGENT_MODEL,
                    max_tokens=MAX_RESPONSE_TOKENS,
                    system=system_blocks,
                    tools=tool_schemas,
                    messages=messages,
                    temperature=0.1,
                )
                from scripts import usage_meter
                usage_meter.record(getattr(_resp, "usage", None))
                return _resp
            except anthropic.BadRequestError as e:
                bad = set(_parse_inaccessible_domains(getattr(e, "message", "") or str(e)))
                if not bad or not allowed_domains:
                    raise
                kept = [d for d in allowed_domains if _normalize_domain(d) not in bad]
                dropped = sorted(set(_normalize_domains(allowed_domains)) - set(_normalize_domains(kept)))
                print(f"[agent_loop] web_search: dropped {len(dropped)} crawler-inaccessible domain(s) "
                      f"from allowlist: {dropped}")
                allowed_domains = kept or None
                tool_schemas = build_tool_schemas(allowed_domains)
                if not allowed_domains:
                    print("[agent_loop] web_search: allowlist empty after pruning — falling back to blocklist")
        raise RuntimeError("web_search allowlist still rejected after pruning inaccessible domains")

    for turn in range(MAX_AGENT_TURNS):
        if turn > 0:
            # Pace requests to stay under Tier 1 TPM
            time.sleep(SLEEP_BETWEEN_TURNS_SEC)
        response = _create_response()

        # Build assistant message from response.content (multiple blocks possible)
        assistant_content = []
        for block in response.content:
            assistant_content.append(block.model_dump())
        messages.append({"role": "assistant", "content": assistant_content})

        # Find tool uses
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            # No tool calls and no submit. Likely model gave up — break with error.
            break

        # Execute tools, build tool_result blocks
        tool_results = []
        for tu in tool_uses:
            name = tu.name
            tu_id = tu.id
            tinput = tu.input
            try:
                result = _execute_tool(name, tinput, newsletter)
                if name == "web_fetch" and isinstance(result, dict):
                    # Key on both requested and post-redirect URL so the later
                    # source_url lookup hits regardless of redirects.
                    for k in (result.get("url"), result.get("final_url")):
                        if k:
                            fetched_bodies[k] = result
                if name == "submit_newsletter":
                    # Emit-time grounding gate: reject a payload that cites an
                    # unfetched top-story source or an ungrounded Other News stat,
                    # so the agent fixes it and resubmits (a generation-time catch,
                    # not a post-hoc banner). Off for the config-pack dailies until a
                    # dry-run validates it against their real payloads.
                    violations = (_grounding_violations(result, fetched_bodies)
                                  if strict_grounding_enabled(newsletter) else [])
                    if violations:
                        print(f"[agent_loop] submit rejected — {len(violations)} grounding "
                              f"violation(s); asking the agent to fix + resubmit")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu_id,
                            "is_error": True,
                            "content": ("submit_newsletter REJECTED — fix every item below, then "
                                        "call submit_newsletter again:\n- " + "\n- ".join(violations)),
                        })
                    else:
                        final_payload = result
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu_id,
                            "content": "Submitted.",
                        })
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu_id,
                        "content": json.dumps(result, ensure_ascii=False)[:8000],  # cap per-tool output (Tier 1 TPM safety)
                    })
            except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "is_error": True,
                    "content": f"Tool '{name}' raised: {type(e).__name__}: {e}",
                })

        messages.append({"role": "user", "content": tool_results})

        if final_payload is not None:
            _attach_article_bodies(final_payload, fetched_bodies)
            return final_payload

    raise RuntimeError(f"Agent did not submit a payload within {MAX_AGENT_TURNS} turns.")


def _norm_url(u: str) -> str:
    """Canonicalize a URL for matching: drop query/fragment + trailing slash,
    lowercase scheme/host. The agent often fetches a tracking URL
    (…/slug/?utm_campaign=…) but submits the cleaned source_url (…/slug/), so
    exact-key lookups miss without this."""
    p = urlparse(u or "")
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]  # www vs non-www is the same article — don't let it miss
    return f"{p.scheme.lower()}://{host}{p.path.rstrip('/')}"


def strict_grounding_enabled(newsletter: str) -> bool:
    """Whether the emit-time grounding gates (top-story-URL-fetched + Other-News
    number-sourcing) fire for this newsletter. ON for every audience EXCEPT the
    config-pack dailies — those are the live
    product and stay on the existing post-hoc guards until a dry-run validates
    the gates against their real payloads. Set STRICT_GROUNDING_ALL=1 to enable
    them everywhere once that validation passes."""
    if os.environ.get("STRICT_GROUNDING_ALL") == "1":
        return True
    from scripts import audience_config
    return newsletter not in audience_config.newsletter_display_names()


def _grounding_violations(payload: dict[str, Any],
                          fetched_bodies: dict[str, dict[str, Any]]) -> list[str]:
    """Offline emit-time grounding checks. Returns actionable violation strings
    (empty = pass); a non-empty result rejects the submit so the agent retries.

    Three rules, targeting the ways the model invented numbers:
      1. Every Top Story must cite a source_url actually fetched THIS run — kills
         the 'cite a page you never opened' path (a story with no real fetched
         evidence behind its numbers).
      2. Every number in a Top Story must appear verbatim in that story's
         `source_excerpt` — the deterministic numeric guard, run at emit time so a
         figure pulled from memory (Zelle's "$1.2 trillion" not in the fetched
         excerpt) forces a retry instead of only tripping the post-hoc banner.
      3. Any Other News item that states a statistic must carry a `number_source`
         sentence that actually contains the figure — Other News has no excerpt,
         so this is the only generation-time grounding it can have. (The live
         verifier still re-checks the number against the real source post-hoc.)"""
    from scripts.numeric_guard import _material_claims, unsupported_numbers, check_story
    problems: list[str] = []
    fetched = {_norm_url(k) for k in fetched_bodies}

    for i, s in enumerate(payload.get("top_stories") or []):
        url = s.get("source_url") or ""
        if url and _norm_url(url) not in fetched:
            problems.append(
                f"Top story {i+1} cites {url}, but you never web_fetched that page this "
                f"run — its facts have no real source. Fetch it and quote from it, or drop the story.")
        for fig in check_story(s):
            problems.append(
                f"Top story {i+1}: {fig}. Every number in a top story must appear verbatim in its "
                f"source_excerpt — quote the source sentence that states it, or make the point qualitatively.")

    for i, item in enumerate(payload.get("other_news") or []):
        claim = " ".join([item.get("headline") or "",
                          item.get("subtitle") or item.get("summary") or ""])
        mats = _material_claims(claim)
        if not mats:
            continue
        figs = ", ".join(sorted({d for d, _ in mats}))
        src = (item.get("number_source") or "").strip()
        if not src:
            problems.append(
                f"Other News {i+1} states {figs} but has no number_source. Paste the verbatim "
                f"source sentence that contains the figure, or remove the number.")
        elif unsupported_numbers(claim, src):
            problems.append(
                f"Other News {i+1}: {', '.join(unsupported_numbers(claim, src))} is not in your "
                f"number_source sentence. Quote the sentence that actually states it, or drop the number.")
    return problems


def _attach_article_bodies(
    payload: dict[str, Any], fetched_bodies: dict[str, dict[str, Any]]
) -> None:
    """Embed the fetched article body + title into each top story (by source_url)
    so the review console can render the original article without re-fetching.
    Exact-URL match first, then a query/slash-normalized fallback.
    setdefault: never clobber a body the agent already supplied."""
    norm_index: dict[str, dict[str, Any]] = {}
    for key, body in fetched_bodies.items():
        norm_index.setdefault(_norm_url(key), body)
    # Top stories AND bench reserves — the console renders the original article
    # for whichever a reviewer promotes, so both need their bodies attached.
    for story in (payload.get("top_stories") or []) + (payload.get("bench") or []):
        url = story.get("source_url", "")
        hit = fetched_bodies.get(url) or norm_index.get(_norm_url(url))
        if hit:
            story.setdefault("article_text", hit.get("body_text", "") or "")
            story.setdefault("article_title", hit.get("title", "") or "")


def _build_dynamic_context(
    state: dict[str, Any],
    today_date_iso: str,
    issue_number: int,
    editor_must_fix: list[str] | None,
    win_min: int = 7,
    win_max: int = 21,
) -> str:
    """Per-request system context that comes AFTER the cached static brief.

    Contains today's date, freshness cutoff, issue number, last-run state, and
    any editor must-fix items from a prior failed draft. This block varies per
    request — it's intentionally placed after the cache_control breakpoint so
    the static brief (~2K tokens) stays cached across the full agent loop.
    """
    earliest_acceptable_date = freshness_floor(
        today_date_iso, state.get("last_run_at"), win_min, win_max).strftime("%Y-%m-%d")
    parts = [
        "## Today's run context",
        f"- TODAY: {today_date_iso}",
        f"- EARLIEST_ACCEPTABLE_DATE: {earliest_acceptable_date}",
        f"  (Reject any source article published before this date. Stale news",
        f"  is the most common failure mode — verify published_at on every",
        f"  web_fetch result before including the story.)",
        f"- Issue number to assign: {issue_number}",
        f"- Time window for fresh news: from {state.get('last_run_at','never')} through now.",
        f"- Last feedback check: {state.get('last_feedback_check_at','never')}",
    ]
    if editor_must_fix:
        parts.extend([
            "",
            "## ⚠ Previous draft REJECTED by Sr. Editor — fix these:",
        ])
        for fix in editor_must_fix:
            parts.append(f"- {fix}")
        parts.append("")
        parts.append("Regenerate the draft addressing every fix above, then call submit_newsletter.")
    return "\n".join(parts)


def _build_feed_block(candidates: list[dict[str, Any]]) -> str:
    """Render pre-fetched feed candidates as an opening-message block (empty if none).

    The candidates are LEADS, not citations — the prompt is explicit that the agent
    must web_fetch each before using it, so this never short-circuits fact discipline."""
    if not candidates:
        return ""
    ranked = any(isinstance(c.get("rank_score"), int) for c in candidates)
    intro = (
        "LEADS pulled from your curated RSS feeds, then scored 0-100 for relevance to THIS "
        "audience (highest first) — prioritise the top-scored. "
        if ranked else
        "LEADS pulled from your curated RSS feeds (freshness-filtered). "
    )
    lines = [
        "\n\n## Fresh candidates from your trusted feeds",
        intro
        + "They are NOT verified — web_fetch each one you intend to use to confirm the facts, "
        "the published_at date, and to pull the source_excerpt. Do NOT cite from the feed line "
        "alone. web_search is still available to supplement (not replace) these.",
        "",
    ]
    for c in candidates:
        d = c.get("published_date")
        date_str = d.isoformat() if d else "date?"
        score = c.get("rank_score")
        prefix = f"[{score:>3}] " if isinstance(score, int) else ""
        reason = f"  — {c['rank_reason']}" if c.get("rank_reason") else ""
        lines.append(f"- {prefix}{c['title']} — {c['source_domain']} · {date_str} · {c['url']}{reason}")
    return "\n".join(lines)


def _build_edit_feedback_block(newsletter: str, max_issues: int = 1, max_rows: int = 8) -> str:
    """Render the human editor's most-recent review-console edits as an
    opening-message block (empty string if none).

    Wires issue_memory.load_editorial into generation so the corrections made in the
    review console — a dropped story, a reworded implication — actually reach the
    next issue's prompt. Before this, those markups were written to
    review/feedback/<nl>-<issue>.json and never read back, so they influenced
    nothing (verified 2026-07-10, ticket newsletter-hitl-feedback-verify).

    The records capture WHAT changed (dropped/edited components), not WHY, so this is
    selection/style guidance, not explicit intent. Scoped to the most-recent issue and
    capped so it never anchors generation on stale specifics. Never raises — a
    feedback-file problem must not block a run."""
    try:
        from scripts import issue_memory
        rows = issue_memory.load_editorial(newsletter)
    except Exception as _e:  # noqa: BLE001 — feedback must never block a run
        print(f"[agent_loop] edit-feedback load failed (continuing): {type(_e).__name__}: {_e}")
        return ""
    if not rows:
        return ""
    # Keep only the most-recent issue(s): a stale drop shouldn't suppress a genuinely
    # new story weeks later, and old edits drift out of relevance.
    numbered = [r for r in rows if isinstance(r.get("issue"), int)]
    if numbered:
        keep = set(sorted({r["issue"] for r in numbered}, reverse=True)[:max_issues])
        recent = [r for r in numbered if r["issue"] in keep]
    else:
        recent = rows  # unnumbered file (rare) — take as-is
    if not recent:
        return ""
    lines = [
        "\n\n## Prior editorial corrections (your last review)",
        "PRIOR EDITORIAL CORRECTIONS — match this style and avoid re-introducing dropped "
        "story types. These are the human editor's own edits to the previous issue; they "
        "record WHAT changed, not why, so treat them as selection/style guidance, not "
        "hard rules:",
        "",
    ]
    for r in recent[:max_rows]:
        tag = (r.get("tag") or "note").strip()
        comp = (r.get("component") or "").strip()
        instr = (r.get("instruction") or "").strip()
        # instruction already reads "edited: … → …" / "dropped: …"; strip the leading
        # "<tag>:" so it isn't doubled with the [tag] prefix we prepend.
        if instr.lower().startswith(tag.lower() + ":"):
            instr = instr[len(tag) + 1:].strip()
        lines.append(f"- [{tag}] {comp}: {instr}" if comp else f"- [{tag}] {instr}")
    n_more = len(recent) - max_rows
    if n_more > 0:
        lines.append(f"- …and {n_more} more correction(s).")
    return "\n".join(lines)


# The submit_newsletter top-story schema fields (see build_tool_schemas above).
# Any key on a story dict outside this set is a json-repair fragmentation
# artifact: when the model double-serializes top_stories as a JSON string with
# unescaped quotes inside source_excerpt, the tolerant parser tears the excerpt
# at the stray quote and reinterprets the trailing verbatim text as bogus
# sibling keys (e.g. "low-quality,", "low-effort,"). The Sr. Editor then flags
# the story as "excerpt split across multiple keys."
_TOP_STORY_KEYS = frozenset({
    "headline", "summary", "track", "published_at", "dateline",
    "hero_image_url", "source_url", "source_excerpt", "implications",
    "korean_takeaway",
})


def _sanitize_top_stories(stories: Any) -> Any:
    """Repair json-repair source_excerpt fragmentation in a top_stories list.

    Pulled out as a pure function so it can be unit-tested without the API
    (mirrors sr_editor.parse_editor_response). For each story dict: re-join any
    fragmentation-artifact keys back into source_excerpt (best-effort — recovers
    the verbatim tail json-repair tore off at an unescaped quote) and coerce
    source_excerpt to a single clean string. The stray keys are dropped so the
    payload is clean before it reaches the Sr. Editor and the renderer. Non-dict
    items and non-list input pass through untouched.
    """
    import sys as _sys
    from scripts.render_html import coerce_excerpt
    if not isinstance(stories, list):
        return stories
    for story in stories:
        if not isinstance(story, dict):
            continue
        bogus = [k for k in story if k not in _TOP_STORY_KEYS]
        excerpt = coerce_excerpt(story.get("source_excerpt"))
        if bogus:
            salvage: list[str] = []
            for k in bogus:
                salvage.append(str(k))
                salvage.append(coerce_excerpt(story.pop(k)))
            tail = " ".join(p for p in salvage if p)
            if tail:
                excerpt = f"{excerpt} {tail}".strip()
            print(
                f"[agent_loop] WARN: story '{str(story.get('headline', ''))[:50]}' had "
                f"{len(bogus)} fragmented excerpt key(s) {bogus} from json-repair — "
                f"re-joined into source_excerpt.",
                file=_sys.stderr,
            )
        story["source_excerpt"] = excerpt
        # Normalize implications to a list at ingestion: the model can emit
        # `null` (schema is advisory) or json-repair can null it out. Renderers
        # coerce too, but keeping the saved payload clean protects every
        # downstream consumer (replay, review console, feedback).
        if not isinstance(story.get("implications"), list):
            story["implications"] = []
    return stories


def _execute_tool(name: str, tinput: dict[str, Any], newsletter: str) -> Any:
    # web_search is server-side now — Anthropic handles invocation and we never
    # see a tool_use block for it. Only client-side tools below.
    if name == "web_fetch":
        url = tinput["url"]
        # Log every fetch attempt with its domain. Lets us see which outlets the
        # agent actually reads — and spot feed-only / web_search-blocked domains
        # (e.g. theverge.com) being recovered via the RSS-ingestion path.
        host = urlparse(url).netloc.lower()
        host = host[4:] if host.startswith("www.") else host
        print(f"[agent_loop]   web_fetch → {host}  {url[:90]}")
        return web_fetch.fetch(url)
    if name == "og_image_check":
        verdict = og_image.check_image_relevance(
            tinput["image_url"],
            tinput["headline"],
            track=tinput.get("track", ""),
            image_size_bytes=tinput.get("image_size_bytes"),
        )
        if verdict is True:
            return {"verdict": "approve"}
        if verdict is False:
            return {"verdict": "reject"}
        return {"verdict": "skip", "reason": "image too large, unfetchable, or vision check failed"}
    if name == "notion_get_feedback":
        return notion_client.get_recent_feedback(
            tinput.get("newsletter", newsletter),
            tinput["since_iso"],
        )
    if name == "submit_newsletter":
        # Defensive: under complex schemas the model occasionally serializes
        # array fields as JSON strings instead of native arrays. Worse, the
        # serialized content can contain unescaped quotes from source_excerpt
        # (verbatim article text that includes "quoted phrases"), making the
        # string itself malformed JSON. Two-stage repair: standard json.loads,
        # then fall back to json-repair (handles unescaped quotes, trailing
        # commas, mixed quote styles, and other LLM serialization quirks).
        import sys as _sys
        from json_repair import repair_json as _repair_json
        payload = dict(tinput)  # shallow copy
        for k in ("top_stories", "other_news", "anomalies", "bench"):
            v = payload.get(k)
            if isinstance(v, str):
                try:
                    payload[k] = json.loads(v)
                    print(f"[agent_loop] WARN: '{k}' arrived as JSON string from model — auto-parsed.", file=_sys.stderr)
                    continue
                except (json.JSONDecodeError, TypeError):
                    pass
                # Strict parse failed — try json-repair (tolerant parser)
                try:
                    repaired = _repair_json(v, return_objects=True)
                    if isinstance(repaired, list):
                        payload[k] = repaired
                        print(f"[agent_loop] WARN: '{k}' was malformed JSON string ({len(v)} chars) — repaired via json-repair.", file=_sys.stderr)
                        continue
                except Exception as e:  # noqa: BLE001
                    print(f"[agent_loop] WARN: json-repair also failed for '{k}': {e}", file=_sys.stderr)
                # Both parsers failed — log diagnostic and leave as-is so
                # downstream surfaces a clear error rather than silently passing.
                head = v[:400].replace("\n", "\\n")
                tail = v[-200:].replace("\n", "\\n") if len(v) > 400 else ""
                print(f"[agent_loop] WARN: '{k}' is a string ({len(v)} chars), strict + repair both failed; leaving as-is.", file=_sys.stderr)
                print(f"[agent_loop]   head: {head}", file=_sys.stderr)
                if tail:
                    print(f"[agent_loop]   tail: ...{tail}", file=_sys.stderr)
        # json-repair (or the model) can leave source_excerpt fragmented across
        # stray sibling keys when verbatim article quotes weren't escaped — e.g.
        # "low-quality," "low-effort," become bogus keys. Re-join them into one
        # clean source_excerpt string BEFORE the payload reaches the Sr. Editor
        # (which flags the fragmentation) and the renderer.
        if "top_stories" in payload:
            payload["top_stories"] = _sanitize_top_stories(payload["top_stories"])
        # Bench reserves are full-story shape too — same excerpt-fragmentation repair.
        if "bench" in payload:
            payload["bench"] = _sanitize_top_stories(payload["bench"])
        return payload
    raise ValueError(f"Unknown tool: {name}")
