"""Unit tests for sr_editor.parse_editor_response.

Tests the response-parsing layer in isolation — no API calls. These cover the
failure modes that have actually happened during live retests:

  1. Model wraps JSON in markdown ```json ... ``` fences
  2. Model returns unparseable JSON
  3. Model returns lowercase verdict
  4. Model returns must_fix as a string instead of a list
  5. Model omits notes / must_fix entirely
  6. Model returns an empty response

A regression here is what produced the 2026-05-25 false-positive editor flags.
With these tests, editor-prompt iteration can happen for $0.
"""

import pytest

from scripts.sr_editor import parse_editor_response


def test_clean_pass_verdict():
    out = parse_editor_response('{"verdict": "PASS", "must_fix": [], "notes": "All good."}')
    assert out == {"verdict": "PASS", "must_fix": [], "notes": "All good."}


def test_clean_fail_verdict():
    out = parse_editor_response(
        '{"verdict": "FAIL", "must_fix": ["Story 02 is stale"], "notes": "Reject draft."}'
    )
    assert out["verdict"] == "FAIL"
    assert out["must_fix"] == ["Story 02 is stale"]
    assert out["notes"] == "Reject draft."


def test_markdown_code_fence_stripped():
    """Models sometimes wrap JSON in ```json ... ``` despite system instructions."""
    raw = '```json\n{"verdict": "PASS", "must_fix": [], "notes": ""}\n```'
    out = parse_editor_response(raw)
    assert out["verdict"] == "PASS"


def test_bare_code_fence_stripped():
    """Same as above but with no language tag on the fence."""
    raw = '```\n{"verdict": "FAIL", "must_fix": ["claim X unsupported"], "notes": ""}\n```'
    out = parse_editor_response(raw)
    assert out["verdict"] == "FAIL"
    assert out["must_fix"] == ["claim X unsupported"]


def test_unparseable_json_returns_conservative_fail():
    """Malformed JSON must NOT crash — must return FAIL with diagnostic note."""
    raw = '{"verdict": "PASS", "must_fix": [unclosed'
    out = parse_editor_response(raw)
    assert out["verdict"] == "FAIL"
    assert "unparseable" in out["must_fix"][0].lower()
    assert raw[:50] in out["notes"]


def test_lowercase_verdict_normalized_to_uppercase():
    out = parse_editor_response('{"verdict": "pass", "must_fix": [], "notes": ""}')
    assert out["verdict"] == "PASS"


def test_mixed_case_verdict_normalized():
    out = parse_editor_response('{"verdict": "Fail", "must_fix": [], "notes": ""}')
    assert out["verdict"] == "FAIL"


def test_must_fix_as_string_coerced_to_list():
    """Model occasionally returns must_fix as a string instead of a list."""
    out = parse_editor_response('{"verdict": "FAIL", "must_fix": "Story 02 is stale", "notes": ""}')
    assert out["must_fix"] == ["Story 02 is stale"]


def test_missing_must_fix_defaults_to_empty_list():
    out = parse_editor_response('{"verdict": "PASS", "notes": "fine"}')
    assert out["must_fix"] == []


def test_missing_notes_defaults_to_empty_string():
    out = parse_editor_response('{"verdict": "PASS", "must_fix": []}')
    assert out["notes"] == ""


def test_missing_verdict_defaults_to_fail():
    """Defensive: a verdict missing the verdict key should fail conservatively."""
    out = parse_editor_response('{"must_fix": [], "notes": ""}')
    assert out["verdict"] == "FAIL"


def test_empty_response_returns_fail():
    out = parse_editor_response("")
    assert out["verdict"] == "FAIL"
    assert "unparseable" in out["must_fix"][0].lower()


def test_whitespace_only_response_returns_fail():
    out = parse_editor_response("   \n\n   ")
    assert out["verdict"] == "FAIL"


def test_must_fix_items_coerced_to_strings():
    """If model returns non-string entries in must_fix, coerce defensively."""
    out = parse_editor_response('{"verdict": "FAIL", "must_fix": [{"complex": "obj"}, 42], "notes": ""}')
    assert len(out["must_fix"]) == 2
    assert all(isinstance(x, str) for x in out["must_fix"])


def test_realistic_failing_verdict():
    """Mirrors the actual verdict shape from a 2026-05-26 retest."""
    raw = (
        '{"verdict": "FAIL", '
        '"must_fix": ['
        '"Story 02 published 2026-05-20 is 5d old, beyond the 2026-05-22 cutoff", '
        '"Oura \\"$2B in 2026 sales\\" claim not in source_excerpt"'
        '], '
        '"notes": "Stale-news belt+suspenders fired correctly. Editor caught a fabricated revenue claim."}'
    )
    out = parse_editor_response(raw)
    assert out["verdict"] == "FAIL"
    assert len(out["must_fix"]) == 2
    assert "cutoff" in out["must_fix"][0].lower()
    assert "Oura" in out["must_fix"][1]
    # Verify the escaped inner quotes survived JSON parsing intact
    assert '"$2B in 2026 sales"' in out["must_fix"][1]
