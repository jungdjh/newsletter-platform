"""Loader for audience config packs (config/audiences/<pack>/).

The engine is audience-agnostic: every brand palette, hand-written audience
brief, source/feed allowlist, and operator setting that used to be hardcoded
for the original newsletters now lives in a config pack. The engine discovers
packs by scanning config/audiences/ — no pack name appears in engine code —
so a checkout without a pack (e.g. the public mirror) falls back cleanly to
the generated-spec path (briefs/<name>.json).

Pack layout:
  config/audiences/<pack>/
    palettes.json    {newsletter: branding entry with a `chassis` block}
    briefs/<nl>.md   verbatim operational brief sent to the writer agent
    sources.json     {newsletter: {"sources": [...], "feeds": [...]}}
    operator.json    operator_email / gh_repo / editor_team_name / newsletters

All lookups are best-effort: missing packs or files return None/{} and the
caller falls back to its generic behavior.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config" / "audiences"


def _packs() -> list[Path]:
    if not CONFIG_ROOT.is_dir():
        return []
    return sorted(p for p in CONFIG_ROOT.iterdir() if p.is_dir())


def _read_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def load_palettes() -> dict[str, dict]:
    """Merged branding entries from every pack's palettes.json (raw entries;
    render_html expands each onto the shared chassis)."""
    out: dict[str, dict] = {}
    for pack in _packs():
        out.update(_read_json(pack / "palettes.json"))
    return out


# Hero assets are self-hosted on the always-on Railway review app (public, no
# auth) so the "lead always has a visual" guarantee never depends on a
# third-party image host. The deploy host is injected at runtime via env
# (ASSET_BASE_URL, else REVIEW_URL — a repo Actions variable) and is
# deliberately NOT hardcoded here: this file is on the public-mirror allowlist,
# so a literal host would trip mirror.sh's leak scan and bake a private URL into
# the would-be-public engine. Empty base → a relative ref (caught loudly at
# generate time, see run_newsletter._backfill_lead_hero).
DEFAULT_FALLBACK_HERO = "hero/default.jpg"


def _asset_base() -> str:
    return (os.environ.get("ASSET_BASE_URL")
            or os.environ.get("REVIEW_URL")
            or "").rstrip("/")


def _resolve_asset(ref: str) -> str:
    """A full http(s) URL passes through (an audience may point at an external
    image); a bare path resolves against the self-hosted asset base."""
    ref = ref.strip()
    if ref.lower().startswith(("http://", "https://")):
        return ref
    return f"{_asset_base()}/assets/{ref.lstrip('/')}"


def load_fallback_hero(newsletter: str) -> str:
    """Fallback hero image URL for the lead / Big-Signal card. ALWAYS returns a
    URL (never None) so every audience can lead with a visual (David's locked
    rule, 2026-07-12).

    Uses the audience's `fallback_hero_url` (branding entry in
    config/audiences/<pack>/palettes.json) when set, else the shared self-hosted
    default. Platform audiences (nursing) source from sites with no usable
    og:image, so the agent leaves the lead imageless and run_newsletter fills
    this in. A bare ref (e.g. "hero/nursing.jpg") resolves to the self-hosted
    asset; a full http(s) URL is used as-is."""
    entry = load_palettes().get(newsletter) or {}
    raw = entry.get("fallback_hero_url")
    ref = raw if isinstance(raw, str) and raw.strip() else DEFAULT_FALLBACK_HERO
    return _resolve_asset(ref)


def load_brief(newsletter: str) -> str | None:
    """Verbatim hand-written brief for a pack newsletter, or None."""
    for pack in _packs():
        path = pack / "briefs" / f"{newsletter}.md"
        if path.exists():
            return path.read_text()
    return None


def load_sources_config() -> dict[str, dict]:
    """Merged sources.json from every pack:
    {newsletter: {"sources": [...], "feeds": [...]}}."""
    out: dict[str, dict] = {}
    for pack in _packs():
        out.update(_read_json(pack / "sources.json"))
    return out


def load_recipients(newsletter: str) -> list[dict]:
    """Recipient list [{"name": str, "email": str}] for a newsletter, merged from
    every pack's recipients.json and keyed by newsletter/audience slug. Returns []
    if unconfigured. Recipients live ONLY in the private config packs
    (config/audiences/<pack>/recipients.json) — mirror-excluded, so a public
    checkout has no recipients.json and this returns []."""
    out: dict[str, list] = {}
    for pack in _packs():
        out.update(_read_json(pack / "recipients.json"))
    return out.get(newsletter, [])


def _operator_config() -> dict:
    for pack in _packs():
        cfg = _read_json(pack / "operator.json")
        if cfg:
            return cfg
    return {}


def operator_email() -> str | None:
    """The operator (To:) address for real sends, or None if unconfigured."""
    return _operator_config().get("operator_email")


def preview_email() -> str | None:
    """Where the pre-release preview goes — David's PERSONAL inbox, not the
    audience list. Layer 0: he reviews the frozen render on his phone and
    releases from there, so the preview must land somewhere he actually reads
    on a phone. Falls back to the operator address if unconfigured."""
    return _operator_config().get("preview_email") or operator_email()


def gh_repo() -> str | None:
    """Default owner/repo for the hosted review console, or None."""
    return _operator_config().get("gh_repo")


def feedback_email() -> str | None:
    """Reply-with-feedback address rendered in newsletter footers, or None."""
    return _operator_config().get("feedback_email")


def fallback_palette() -> str | None:
    """Palette key to use for a spec without a `theme`, or None."""
    return _operator_config().get("fallback_palette")


def editor_team_name(newsletter: str) -> str | None:
    """Audience label the Sr. Editor prompt uses for a pack newsletter."""
    cfg = _operator_config()
    if newsletter in cfg.get("newsletters", []):
        return cfg.get("editor_team_name")
    return None


def notion_recipients_ds_id() -> str | None:
    """Notion data-source ID for the recipients DB, or None if unconfigured."""
    return _operator_config().get("notion_recipients_ds_id")


def notion_feedback_ds_id() -> str | None:
    """Notion data-source ID for the reader-feedback DB, or None if unconfigured."""
    return _operator_config().get("notion_feedback_ds_id")


def newsletter_display_names() -> dict:
    """Map of pack newsletter short-names to their Notion display names."""
    return _operator_config().get("newsletter_display_names", {})
