"""Offline tests for the fabrication eval harness itself.

The harness is what the public numbers rest on, so it gets tests of its own:
the scoring rules, the baseline heuristic, and the fixture set's shape. None
of this calls the API — the one live path (llm_judgement) is exercised with a
stubbed editor so the marker-matching rule is tested without spending a token.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.evals import run_evals as R


# --- fixtures: shape and hygiene ------------------------------------------------

def test_fixtures_load_and_have_the_expected_shape():
    items = R.load_fixtures()
    assert len(items) == 19
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)), "duplicate fixture id"
    for it in items:
        assert it["label"] in ("fabricated", "faithful"), it["id"]
        s = it["story"]
        for key in ("headline", "summary", "source_excerpt", "source_url", "implications", "track"):
            assert key in s, f"{it['id']} missing {key}"
        if it["label"] == "fabricated":
            assert it.get("fabrication"), f"{it['id']} fabricated but has no markers"
            assert it.get("planted"), f"{it['id']} fabricated but does not say what was planted"
        else:
            assert "fabrication" not in it, f"{it['id']} faithful but carries markers"


def test_fixture_counts_match_the_published_arithmetic():
    items = R.load_fixtures()
    fabricated = [i for i in items if i["label"] == "fabricated"]
    faithful = [i for i in items if i["label"] == "faithful"]
    # 11 × 3 runs = 33 recall judgements; 8 × 3 = 24 false-alarm judgements.
    assert (len(fabricated), len(faithful)) == (11, 8)


def test_every_planted_fabrication_differs_from_its_faithful_twin():
    """A fabricated fixture and its faithful twin share an excerpt; the lie has
    to be in the copy, not the source. If the two stories were identical the
    label would be arbitrary and recall would be measuring noise."""
    items = {i["id"]: i for i in R.load_fixtures()}
    twins = [("fab-02", "fab-01"), ("fab-04", "fab-03"), ("fab-06", "fab-05"),
             ("fab-08", "fab-07"), ("fab-10", "fab-09"), ("fab-12", "fab-11"),
             ("nurse-01", "nurse-faithful-01"), ("nurse-02", "nurse-faithful-02")]
    for fab, clean in twins:
        a, b = items[fab]["story"], items[clean]["story"]
        assert a["source_excerpt"] == b["source_excerpt"], (fab, clean)
        assert (a["summary"], a["implications"]) != (b["summary"], b["implications"]), (fab, clean)


# --- scoring rules --------------------------------------------------------------

@pytest.mark.parametrize("label,verdict,cell", [
    ("fabricated", "FAIL", "TP"), ("fabricated", "PASS", "FN"),
    ("faithful", "PASS", "TN"), ("faithful", "FAIL", "FP"),
])
def test_confusion_cells(label, verdict, cell):
    assert R.classify(label, verdict) == cell


def test_fabricated_item_is_caught_only_when_the_editor_names_the_lie():
    markers = ["21"]
    assert R.score_llm(["The $21B valuation is not in the excerpt"], markers) == "FAIL"
    # A generic complaint that never names the planted value is not a catch,
    # even though it uses fabrication language.
    assert R.score_llm(["Summary contains a figure not present in the excerpt"], markers) == "PASS"
    assert R.score_llm([], markers) == "PASS"


def test_faithful_item_is_a_false_alarm_only_for_fabrication_language():
    # Advisory concerns (voice, vendor PR, staleness) are not fabrication flags.
    assert R.score_llm(["Headline is hype-y: 'groundbreaking'"], None) == "PASS"
    assert R.score_llm(["Sourced only to the vendor newsroom"], None) == "PASS"
    assert R.score_llm(["'first for strap-only wearables' is not supported by the excerpt"], None) == "FAIL"
    assert R.score_llm(["The $1.1B figure does not appear in the excerpt"], None) == "FAIL"


def test_marker_match_is_case_insensitive_and_substring():
    assert R.score_llm(["Sundar PICHAI never said this; the quote is invented"], ["pichai", "quote"]) == "FAIL"
    assert R.score_llm(["claims exclusivity with Chase — unsupported"], ["chase", "exclusiv"]) == "FAIL"


# --- baseline heuristic ---------------------------------------------------------

def test_baseline_catches_a_changed_number_and_misses_an_invented_quote():
    items = {i["id"]: i for i in R.load_fixtures()}
    assert R.baseline_verdict(items["fab-02"]["story"]) == "FAIL"   # $11B -> $21B
    assert R.baseline_verdict(items["fab-01"]["story"]) == "PASS"   # faithful twin
    assert R.baseline_verdict(items["fab-06"]["story"]) == "PASS"   # invented quote: blind by design


def test_baseline_recall_on_the_shipped_fixtures_is_the_documented_floor():
    items = R.load_fixtures()
    tp = sum(1 for i in items if i["label"] == "fabricated" and R.baseline_verdict(i["story"]) == "FAIL")
    fp = sum(1 for i in items if i["label"] == "faithful" and R.baseline_verdict(i["story"]) == "FAIL")
    assert (tp, fp) == (6, 0)


# --- the live path, with the editor stubbed -------------------------------------

def test_llm_judgement_returns_verdict_and_the_editor_transcript(monkeypatch):
    import scripts.sr_editor as ed
    def fake_review(**kw):
        assert kw["newsletter"] == R.DEFAULT_NEWSLETTER
        assert kw["story_payload"]["top_stories"][0]["headline"]
        return {"verdict": "FAIL", "must_fix": ["The 40% merchant figure is not present in the excerpt"], "notes": "n"}
    monkeypatch.setattr(ed, "review", fake_review)
    items = {i["id"]: i for i in R.load_fixtures()}
    verdict, raw = R.llm_judgement(items["fab-04"]["story"], R.DEFAULT_NEWSLETTER, items["fab-04"]["fabrication"])
    assert verdict == "FAIL"
    assert raw["must_fix"][0].startswith("The 40%")


def test_transcript_records_are_json_serialisable_and_name_the_cell(tmp_path, monkeypatch):
    """--write must leave a readable record of every judgement, including what
    the editor said. Run the baseline end-to-end into a temp dir."""
    monkeypatch.setattr(R, "HERE", tmp_path)
    monkeypatch.setattr(R, "_SCORECARDS", {"baseline": tmp_path / "scorecard-baseline.md", "llm": tmp_path / "scorecard.md"})
    monkeypatch.setattr(R, "HISTORY", tmp_path / "history.md")
    monkeypatch.setattr("sys.argv", ["run_evals", "--backend", "baseline", "--write"])
    assert R.main() == 0
    rows = [json.loads(l) for l in (tmp_path / "last-run-baseline.jsonl").read_text().splitlines()]
    assert len(rows) == 19
    assert {r["cell"] for r in rows} <= {"TP", "FN", "TN", "FP"}
    card = (tmp_path / "scorecard-baseline.md").read_text()
    assert "## Failure cases" in card and "fab-06" in card
    assert (tmp_path / "history.md").read_text().count("| baseline |") == 1


def test_min_recall_gate_fails_the_run(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(R, "HERE", tmp_path)
    monkeypatch.setattr("sys.argv", ["run_evals", "--backend", "baseline", "--min-recall", "1.0"])
    assert R.main() == 1
    assert "below the required 100%" in capsys.readouterr().out


def test_llm_backend_refuses_to_start_without_a_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["run_evals", "--backend", "llm"])
    assert R.main() == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


# --- the SDK boundary --------------------------------------------------------------

def test_editor_passes_temperature_through_extra_body_not_as_a_keyword(monkeypatch):
    """anthropic 1.x removed the `temperature` keyword from messages.create()
    (TypeError). That is how the 2026-09-01 scheduled eval run died before its
    first fixture. The setting is kept — the recorded passes were measured at
    0.1 — but it must travel in extra_body, which both 0.x and 1.x accept."""
    import anthropic
    import scripts.sr_editor as ed
    seen = {}

    class _Resp:
        content = [type("B", (), {"text": '{"verdict": "PASS", "must_fix": [], "notes": ""}'})()]
        usage = None

    class _Messages:
        def create(self, **kw):
            assert "temperature" not in kw, "keyword removed in anthropic 1.x"
            assert "top_p" not in kw and "top_k" not in kw
            seen.update(kw)
            return _Resp()

    class _Client:
        def __init__(self, **kw):
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    out = ed.review(newsletter=R.DEFAULT_NEWSLETTER, story_payload={"top_stories": [], "other_news": []},
                    today_date_iso="2026-05-27", current_time_ct="07:00 CT")
    assert out["verdict"] == "PASS"
    assert seen["extra_body"] == {"temperature": 0.1}
    assert seen["model"] == ed.EDITOR_MODEL
