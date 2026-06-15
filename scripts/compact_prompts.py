"""Brief routing for the agent loop.

Each audience is a generated spec at ``briefs/<name>.json`` (produced by
``brief_generator``). ``make_compact_prompt`` assembles that spec into the compact
operational brief the agent runs at ~2K tokens; ``get_sources`` / ``get_feeds``
expose the spec's web_search allowlist and RSS feed URLs for sourcing.
"""

from __future__ import annotations

import json
from pathlib import Path

_BRIEFS_DIR = Path(__file__).resolve().parent.parent / "briefs"


def _spec_path(newsletter: str) -> Path:
    return _BRIEFS_DIR / f"{newsletter}.json"


def make_compact_prompt(newsletter: str) -> str:
    """Assemble the compact operational brief for an audience from its spec."""
    path = _spec_path(newsletter)
    if not path.exists():
        raise ValueError(
            f"No brief found for '{newsletter}'. Generate one:\n"
            f"  python -m scripts.brief_generator --audience '...' --out briefs/{newsletter}.json"
        )
    from scripts.brief_generator import assemble_brief
    return assemble_brief(json.loads(path.read_text()))


def _spec_field(newsletter: str, field: str) -> list[str]:
    path = _spec_path(newsletter)
    if path.exists():
        return list(json.loads(path.read_text()).get(field) or [])
    return []


def get_sources(newsletter: str) -> list[str]:
    """Trusted-source allowlist (bare domains) from the audience's spec `sources`.
    Empty if absent — the agent then falls back to the blocklist (no regression)."""
    return _spec_field(newsletter, "sources")


def get_feeds(newsletter: str) -> list[str]:
    """RSS/Atom feed URLs from the audience's spec `feeds` (multi-source ingestion)."""
    return _spec_field(newsletter, "feeds")
