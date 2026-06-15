"""Tests for the brief generator's assemble_brief (pure, offline — no API).

generate_brief_spec needs the LLM + a key, so it's not unit-tested here; the
assembly (spec -> engine-compatible brief) is the part that must stay correct.
"""

import json
from pathlib import Path

import pytest

from scripts.brief_generator import assemble_brief
from scripts.compact_prompts import make_compact_prompt

BRIEFS = Path(__file__).parent.parent / "briefs"


@pytest.fixture
def ai_pms_spec():
    return json.loads((BRIEFS / "ai-pms.json").read_text())


def test_assembles_engine_contract(ai_pms_spec):
    b = assemble_brief(ai_pms_spec)
    # Fixed engine sections must always be present
    for required in ["Output contract", "Top Story fields", "Fact discipline",
                     "Freshness", "Anti-padding", "relevance gate"]:
        assert required in b, f"missing engine section: {required}"


def test_includes_variable_parts(ai_pms_spec):
    b = assemble_brief(ai_pms_spec)
    assert ai_pms_spec["display_name"] in b
    assert ai_pms_spec["relevance_gate"] in b
    for t in ai_pms_spec["tracks"]:
        assert t["name"] in b




def test_self_coverage_block_optional(ai_pms_spec):
    assert "Self-coverage filter" not in assemble_brief(ai_pms_spec)  # null in fixture
    spec = {**ai_pms_spec, "self_coverage": "Acme AI"}
    out = assemble_brief(spec)
    assert "Self-coverage filter" in out and "Acme AI" in out


def test_sources_block_present_when_sources_set(ai_pms_spec):
    b = assemble_brief(ai_pms_spec)
    assert "Trusted sources" in b
    for d in ai_pms_spec["sources"]:
        assert d in b
    # freshness nudge is always present
    assert "Query freshness" in b


def test_no_sources_block_when_absent(ai_pms_spec):
    spec = {**ai_pms_spec, "sources": []}
    b = assemble_brief(spec)
    assert "Trusted sources" not in b
    assert "Query freshness" in b  # freshness nudge is independent of sources


def test_engine_path_loads_generated_brief():
    """make_compact_prompt assembles the brief from briefs/<name>.json."""
    b = make_compact_prompt("ai-pms")
    assert "The AI PM Brief" in b
    assert "Fact discipline" in b  # the fixed engine contract is present



def test_unknown_newsletter_raises_helpful_error():
    with pytest.raises(ValueError, match="brief_generator"):
        make_compact_prompt("does-not-exist-xyz")
