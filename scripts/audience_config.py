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


def _operator_config() -> dict:
    for pack in _packs():
        cfg = _read_json(pack / "operator.json")
        if cfg:
            return cfg
    return {}


def operator_email() -> str | None:
    """The operator (To:) address for real sends, or None if unconfigured."""
    return _operator_config().get("operator_email")


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
