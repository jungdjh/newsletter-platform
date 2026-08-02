"""Compact operational briefs for the agent loop.

The full SKILL.md files (in `prompts/`) are authoritative human-readable
references for the design. The compact brief here is what we actually send
the agent at runtime — ~2K tokens vs ~8K — so we stay under rate limits.

The hand-written briefs for the original newsletters live in an audience
config pack (config/audiences/<pack>/briefs/<name>.md) and are loaded
verbatim at runtime. ANY newsletter without a pack brief is a platform
audience: its brief is assembled from its generated spec (briefs/<name>.json).
"""

from __future__ import annotations

from scripts import audience_config


# Per-newsletter trusted-source allowlists (bare domains, no scheme/path) and
# RSS/Atom feed URLs, loaded from the audience config packs. These mirror the
# tier-1 + specialist outlets each original's brief names in its "Source
# quality" section — promoted from prose guidance the agent could ignore to an
# enforced web_search allowlist. The agent_loop unions the sources with
# TIER1_BASELINE_DOMAINS, so the lists can focus on the niche/specialist
# outlets without re-listing every general wire. Feeds are fetched client-side
# (see scripts/tools/feeds.py), so they surface fresh links from outlets the
# web_search crawler can't reach — best-effort: dead/wrong feeds are skipped
# at runtime, never fatal. Empty when no config pack is present.
_PACK_SOURCES = audience_config.load_sources_config()
NEWSLETTER_SOURCES: dict[str, list[str]] = {
    nl: list(entry.get("sources") or []) for nl, entry in _PACK_SOURCES.items()
}
NEWSLETTER_FEEDS: dict[str, list[str]] = {
    nl: list(entry.get("feeds") or []) for nl, entry in _PACK_SOURCES.items()
}


def get_sources(newsletter: str) -> list[str]:
    """Trusted-source allowlist (bare domains) for a newsletter.

    Pack newsletters use their hand-curated list (NEWSLETTER_SOURCES). ANY
    other name is a platform audience: read its generated spec's `sources`
    field. Returns [] if no sources are found — the caller (agent_loop) then
    falls back to the blocklist, so nothing regresses for a spec that predates
    this field.
    """
    if newsletter in NEWSLETTER_SOURCES:
        return list(NEWSLETTER_SOURCES[newsletter])

    import json as _json
    from pathlib import Path as _Path
    spec_path = _Path(__file__).resolve().parent.parent / "briefs" / f"{newsletter}.json"
    if spec_path.exists():
        spec = _json.loads(spec_path.read_text())
        return list(spec.get("sources") or [])
    return []


def get_feeds(newsletter: str) -> list[str]:
    """RSS/Atom feed URLs for a newsletter (multi-source ingestion).

    Pack newsletters use their hand-curated list (NEWSLETTER_FEEDS). ANY other
    name is a platform audience: read its generated spec's optional `feeds`
    field. Returns [] if none — the agent then sources via web_search only
    (no regression)."""
    if newsletter in NEWSLETTER_FEEDS:
        return list(NEWSLETTER_FEEDS[newsletter])

    import json as _json
    from pathlib import Path as _Path
    spec_path = _Path(__file__).resolve().parent.parent / "briefs" / f"{newsletter}.json"
    if spec_path.exists():
        spec = _json.loads(spec_path.read_text())
        return list(spec.get("feeds") or [])
    return []


def get_freshness_window(newsletter: str) -> tuple[int, int]:
    """Per-newsletter freshness window (min_days, max_days) for freshness_floor.

    Fast fields (AI, Big Tech) want a tight window; a slow field (nursing) wants
    a wide one so a week with no hot news still surfaces relevant recent updates.
    A platform audience can override via `min_window_days` / `max_window_days` in
    its generated spec. Absent → the default (10, 21). The sent-stories ledger
    separately blocks repeats, so a wide window never re-runs old items; it only
    widens what's *eligible*.

    The min was 7 until 2026-07-29. On a weekly cadence `last_run_at` is ~7 days
    back, so the clamp made 7 the *effective* window for every newsletter without
    an override — while the briefs told the agent to search "~10 days". The agent
    obeys the injected EARLIEST_ACCEPTABLE_DATE, not the prose, so stories in the
    8-10 day band were found and then rejected. That is what cost a 2026-07-29 issue
    the Oura launches (Ring 5, menopause insights, women's health AI, US Open), all of
    which landed just before the July 22 edge. Raising the min to 10 makes the
    computed floor match what the briefs have always documented. The four wide
    per-audience overrides (nursing 120, tk-parents 60, first-time-homebuyers 30,
    college-admissions 21) are unaffected — an explicit override still wins."""
    default = (10, 21)
    import json as _json
    from pathlib import Path as _Path
    spec_path = _Path(__file__).resolve().parent.parent / "briefs" / f"{newsletter}.json"
    if spec_path.exists():
        spec = _json.loads(spec_path.read_text())
        lo = spec.get("min_window_days")
        hi = spec.get("max_window_days")
        if lo is not None or hi is not None:
            return (int(lo) if lo is not None else default[0],
                    int(hi) if hi is not None else default[1])
    return default


def make_compact_prompt(newsletter: str) -> str:
    """Return the compact operational brief for the given newsletter.

    The original newsletters use hand-written briefs, loaded verbatim from
    their config pack. ANY other name is treated as a platform audience: if
    `briefs/<name>.json` exists (a spec produced by brief_generator), it's
    assembled into a brief on the fly. This is what makes the system a
    platform rather than a fixed set of hardcoded newsletters.
    """
    brief = audience_config.load_brief(newsletter)
    if brief is not None:
        return brief

    # Platform path: load a generated brief spec.
    import json as _json
    from pathlib import Path as _Path
    spec_path = _Path(__file__).resolve().parent.parent / "briefs" / f"{newsletter}.json"
    if spec_path.exists():
        from scripts.brief_generator import assemble_brief
        return assemble_brief(_json.loads(spec_path.read_text()))

    raise ValueError(
        f"Unknown newsletter '{newsletter}' and no briefs/{newsletter}.json found. "
        f"Generate one: python -m scripts.brief_generator --audience '...' --out briefs/{newsletter}.json"
    )
