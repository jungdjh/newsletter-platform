#!/usr/bin/env python3
"""Fabrication-detection eval harness for the Sr. Editor.

Measures the Sr. Editor's ability to catch claims NOT supported by the source
text. Because every fixture is labeled (we planted the defect), we get ground
truth and can report real precision/recall, not vibes.

Two backends:
  --backend baseline   A label-blind numeric/entity heuristic. $0, no API key.
                       Doubles as a comparison floor for the LLM editor.
  --backend llm        The real Sr. Editor (scripts.sr_editor.review). Costs
                       ~1 API call per item per run. Needs ANTHROPIC_API_KEY.

Confusion matrix (positive class = "fabricated"):
  fabricated + FAIL  -> TP (caught)
  fabricated + PASS  -> FN (miss            <- trust failure)
  faithful   + PASS  -> TN
  faithful   + FAIL  -> FP (cry wolf        <- the false-positive problem)

Recall          = TP / (TP + FN)   -- of fabrications, how many caught
False-pos rate  = FP / (FP + TN)   -- of clean items, how many wrongly flagged

Usage:
  python -m tests.evals.run_evals --backend baseline
  python -m tests.evals.run_evals --backend llm --runs 3 --write
  python -m tests.evals.run_evals --preflight      # CI: is the key + model reachable?

What --write leaves behind (all committed, all public):
  scorecard.md / scorecard-baseline.md   the latest result, with its failure cases
  history.md                             one row per run — the trend
  last-run-<backend>.jsonl               every judgement: id, label, verdict,
                                         and the editor's own words. A miss or a
                                         false alarm is only useful if you can
                                         read what the editor actually said.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "fabrication.jsonl"
# One scorecard per backend. These used to share `scorecard.md`, so `make evals`
# (baseline) silently overwrote the LLM numbers the public README quotes — and
# `scorecard-baseline.md` existed but nothing ever wrote it.
_SCORECARDS = {
    "llm": HERE / "scorecard.md",
    "baseline": HERE / "scorecard-baseline.md",
}
# The scorecard only ever holds the LATEST run, so a drift in the number is
# invisible the moment it is overwritten. History is the point of a scheduled
# run: one number proves nothing, the trend across model versions does.
HISTORY = HERE / "history.md"
_HISTORY_HEADER = (
    "# Fabrication-detection eval — run history\n\n"
    "One row per `--write` run. The scorecard holds the latest result; this\n"
    "holds the trend. A drift with no code change means the model moved.\n\n"
    "| date | backend | model | runs | recall | false-pos | misses | false alarms |\n"
    "|---|---|---|---|---|---|---|---|\n"
)

# The audience name the editor is told it edits for. Any name with no config
# pack and no briefs/<name>.json resolves to generic defaults (display name
# "Briefing", default freshness window), which is exactly what a public,
# audience-agnostic measurement should run under.
DEFAULT_NEWSLETTER = "briefing"

# Significant numbers worth checking: $ figures, percentages, multi-digit ints,
# and 4-digit years. Single low digits (e.g. "2 implications") are too noisy.
_NUM = re.compile(r"\$\d[\d.,]*\s?[BMK]?|\d[\d.,]*%|\b\d{3,}\b|\b20\d{2}\b", re.I)


def _norm(tok: str) -> str:
    return tok.lower().replace(",", "").replace(" ", "").rstrip(".")


def baseline_verdict(story: dict) -> str:
    """Label-blind heuristic: FAIL if a significant number in the summary or
    implications does not appear in the source_excerpt. Catches numeric
    fabrications; misses invented quotes/entities (by design — that gap is
    exactly what motivates the LLM editor)."""
    excerpt = story.get("source_excerpt", "")
    excerpt_nums = {_norm(m) for m in _NUM.findall(excerpt)}
    claim_text = story.get("summary", "") + " " + " ".join(story.get("implications", []))
    for m in _NUM.findall(claim_text):
        if _norm(m) not in excerpt_nums:
            return "FAIL"
    return "PASS"


# Fabrication-specific flag detection. The editor's verdict is ADVISORY and
# fires FAIL on ANY concern — vendor-PR-with-no-external-signal, placeholder/bad
# source URLs, voice, over-certainty — not just fabrication. Scoring on the raw
# verdict therefore conflates "raised a concern" with "cried wolf on fabrication"
# and massively over-counts false positives on faithful items. A fabrication eval
# must score the thing it names: did the editor flag a claim as UNSUPPORTED BY
# THE SOURCE EXCERPT? Match that specific language in the must_fix items.
_FAB_FLAG = re.compile(
    r"not present in|not in the (?:source[_ ])?excerpt|does ?n.?t appear|not supported by|"
    r"no support in|fabricat|invented|made up|not found in|is ?n.?t in the excerpt|"
    r"unsupported by|no basis in|not backed by|contradict|does ?n.?t match the excerpt|"
    r"absent from the excerpt|no mention of|contains no mention|factual error|"
    r"discrepanc|mismatch|not (?:mentioned|stated|reflected) in the (?:source[_ ])?excerpt|"
    # 2026-09-12: the editor said "no reference to" on one of three identical
    # flags and that run scored as clean. Same catch, different verb.
    r"no reference to|nothing in the excerpt|excerpt (?:does ?n.?t|never) (?:mention|state|say)",
    re.I,
)


def flagged_fabrication(must_fix: list) -> bool:
    """True iff any concern is specifically about a claim unsupported by the excerpt."""
    return any(_FAB_FLAG.search(str(m)) for m in (must_fix or []))


def score_llm(must_fix: list, markers: list | None) -> str:
    """Turn the editor's concerns into a fabrication verdict. Pure — no API.

    Fabricated item (markers given = the planted lie's token(s)): caught iff the
    editor NAMES that token. This is robust to phrasing — the editor can word the
    catch however it likes ("not present", "no mention of", "appears nowhere",
    "factual error"...), but if it genuinely caught the lie it references the
    false value ($21B, 40%, the Pichai quote, October 2026, $200M, Chase). That
    ends the brittle keyword whack-a-mole.

    Faithful item (no markers): false-alarm iff the editor flags a claim as
    unsupported by the excerpt (the _FAB_FLAG language). Either way the editor's
    broad ADVISORY verdict — vendor-PR, placeholder URL, voice, over-certainty —
    does NOT count as fabrication detection. That conflation was the original 39%."""
    if markers:
        blob = " || ".join(str(m) for m in must_fix).lower()
        return "FAIL" if any(str(tok).lower() in blob for tok in markers) else "PASS"
    return "FAIL" if flagged_fabrication(must_fix) else "PASS"


def llm_judgement(story: dict, newsletter: str, markers: list | None = None) -> tuple[str, dict]:
    """Run the real Sr. Editor on a one-story payload. Returns (verdict, raw editor result)."""
    from scripts.sr_editor import review  # lazy — needs anthropic + API key
    payload = {"top_stories": [story], "other_news": []}
    result = review(
        newsletter=newsletter,
        story_payload=payload,
        today_date_iso="2026-05-27",
        current_time_ct="07:00 CT",
    )
    must_fix = result.get("must_fix") or []
    return score_llm(must_fix, markers), result


def llm_verdict(story: dict, newsletter: str, markers: list | None = None) -> str:
    """Verdict-only wrapper kept for callers that predate the transcript."""
    return llm_judgement(story, newsletter, markers)[0]


def classify(label: str, verdict: str) -> str:
    fabricated = label == "fabricated"
    failed = verdict == "FAIL"
    if fabricated and failed:
        return "TP"
    if fabricated and not failed:
        return "FN"
    if not fabricated and not failed:
        return "TN"
    return "FP"


def load_fixtures(path: Path = FIXTURES) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def preflight() -> int:
    """Answer the only two questions a red CI run needs answered first: is the
    key present, and does the model the editor is pinned to still exist? Each
    failure prints one line naming the cause, as a GitHub annotation, so the
    next person reading a failed run does not have to reverse-engineer a
    traceback. Costs no tokens — the Models endpoint is free."""
    from scripts.sr_editor import EDITOR_MODEL
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("::error::ANTHROPIC_API_KEY is not set. In GitHub Actions this means the "
              "repository secret is missing or was rotated; locally, export it first.")
        return 1
    try:
        import anthropic
    except ImportError:
        print("::error::the anthropic SDK is not installed (pip install -r requirements-dev.txt)")
        return 1
    client = anthropic.Anthropic(max_retries=2)
    try:
        m = client.models.retrieve(EDITOR_MODEL)
    except anthropic.NotFoundError:
        print(f"::error::model {EDITOR_MODEL!r} is not served any more — update EDITOR_MODEL in "
              "scripts/sr_editor.py. The number will change; that is the point of the history.")
        return 1
    except anthropic.AuthenticationError:
        print("::error::ANTHROPIC_API_KEY was rejected (401). Rotate the secret.")
        return 1
    except anthropic.APIStatusError as e:
        print(f"::error::Anthropic API returned {e.status_code} on models.retrieve: {e.message}")
        return 1
    print(f"preflight ok: key present, model {m.id} ({getattr(m, 'display_name', '')}) is served")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["baseline", "llm"], default="baseline")
    ap.add_argument("--runs", type=int, default=1, help="repeats per item (LLM variance)")
    ap.add_argument("--newsletter", default=DEFAULT_NEWSLETTER)
    ap.add_argument("--write", action="store_true",
                    help="write the scorecard, append history, dump last-run transcript")
    # The CI gate. Recall is the trust metric and is the one that has held flat
    # at 100% across every recorded pass, so it is safe to gate on. The
    # false-positive count is NOT gated: it has scored 0, 1, 0 out of 24 on
    # three consecutive passes of unchanged code, and a gate on a number with
    # that much run-to-run spread is a flaky gate, which is worse than none.
    ap.add_argument("--min-recall", type=float, default=0.0,
                    help="exit non-zero if recall falls below this (0-1). CI uses 1.0")
    ap.add_argument("--preflight", action="store_true",
                    help="check the key and the model, run nothing, exit 0/1")
    args = ap.parse_args()

    if args.preflight:
        return preflight()

    if args.backend == "llm" and not os.environ.get("ANTHROPIC_API_KEY"):
        # Fail before the first fixture, with the cause, instead of a KeyError
        # from inside the SDK client on item one.
        print("ANTHROPIC_API_KEY is not set; the llm backend needs it. "
              "Use --backend baseline for the free run.", file=sys.stderr)
        return 2

    items = load_fixtures()
    cells = {"TP": 0, "FN": 0, "TN": 0, "FP": 0}
    misses, false_alarms = [], []
    transcript: list[dict] = []   # one record per judgement

    for it in items:
        markers = it.get("fabrication")
        for run in range(1, args.runs + 1):
            if args.backend == "baseline":
                verdict, raw = baseline_verdict(it["story"]), {}
            else:
                verdict, raw = llm_judgement(it["story"], args.newsletter, markers)
            cell = classify(it["label"], verdict)
            cells[cell] += 1
            if cell == "FN":
                misses.append(it["id"])
            if cell == "FP":
                false_alarms.append(it["id"])
            transcript.append({
                "id": it["id"], "run": run, "label": it["label"], "planted": it.get("planted"),
                "markers": markers, "verdict": verdict, "cell": cell,
                "editor_verdict": raw.get("verdict"), "must_fix": raw.get("must_fix"),
                "notes": raw.get("notes"),
            })
            print(f"  {it['id']:<20} run {run}: {cell}", flush=True)

    tp, fn, tn, fp = cells["TP"], cells["FN"], cells["TN"], cells["FP"]
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    n_fabricated = sum(1 for it in items if it["label"] == "fabricated")

    method = (
        "Scoring: fabricated item caught iff the editor NAMES the planted lie token "
        "(robust to phrasing); faithful item is a false alarm iff the editor flags a "
        "claim unsupported by the excerpt. The editor's broad advisory verdict "
        "(vendor-PR, placeholder URL, voice, over-certainty) is excluded — measuring "
        "it as fabrication detection is what produced the misleading 39% earlier."
        if args.backend == "llm"
        else "Scoring: label-blind numeric heuristic — FAIL iff a significant number in "
        "the summary/implications is absent from the source_excerpt. Misses non-numeric "
        "fabrications (invented quotes, entities, false exclusivity) by design."
    )
    model = _model_name(args.backend)
    from datetime import date
    lines = [
        f"# Fabrication-detection eval — backend={args.backend}, runs={args.runs}",
        "",
        f"- Date: {date.today().isoformat()}   model: {model}",
        f"- Command: `python -m tests.evals.run_evals --backend {args.backend} --runs {args.runs} --write`",
        # Spell the arithmetic out. "Items: 19 (fabricated 33, faithful 24)"
        # read as 57 items to every reviewer who saw it: 19 is the fixture
        # count, 33 and 24 are per-JUDGEMENT counts across --runs repeats.
        f"- Items: {len(items)} fixtures × {args.runs} run"
        f"{'' if args.runs == 1 else 's'} = {tp+fn+tn+fp} judgements "
        f"({n_fabricated} fabricated, {len(items)-n_fabricated} faithful fixtures)",
        f"- **Recall (catch rate): {tp}/{tp+fn} = {recall:.0%}**   misses: {sorted(set(misses)) or 'none'}",
        f"- **False-positive rate: {fp}/{fp+tn} = {fpr:.0%}**   false alarms: {sorted(set(false_alarms)) or 'none'}",
        "",
        f"  confusion: TP={tp} FN={fn} TN={tn} FP={fp}",
        "",
        f"_{method}_",
    ]
    lines += _failure_section(transcript, items)
    out = "\n".join(lines)
    print("\n" + out)
    if args.write:
        scorecard = _SCORECARDS[args.backend]
        scorecard.write_text(out + "\n")
        print(f"\n→ wrote {scorecard}")
        _append_history(args, model, tp, fn, tn, fp, recall, fpr, misses, false_alarms)
        print(f"→ appended {HISTORY}")
        dump = HERE / f"last-run-{args.backend}.jsonl"
        dump.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in transcript))
        print(f"→ wrote {dump} ({len(transcript)} judgements)")
    if recall < args.min_recall:
        print(f"\nFAIL: recall {recall:.0%} is below the required "
              f"{args.min_recall:.0%}. Missed: {sorted(set(misses))}")
        return 1
    return 0


def _model_name(backend: str) -> str:
    if backend == "llm":
        from scripts.sr_editor import EDITOR_MODEL
        return EDITOR_MODEL
    return "n/a (offline heuristic)"


def _failure_section(transcript: list[dict], items: list[dict]) -> list[str]:
    """The part of a scorecard worth reading: every miss and every false alarm,
    with what was planted and what the editor actually said. A scorecard that
    only prints the headline number hides exactly the cases a reader needs to
    judge whether the number means anything."""
    bad = [r for r in transcript if r["cell"] in ("FN", "FP")]
    lines = ["", "## Failure cases", ""]
    if not bad:
        lines.append("None in this run. (That is a claim about this fixture set, not about the "
                     "editor in general — see the false-positive discussion in history.md.)")
        return lines
    for r in bad:
        kind = "MISS" if r["cell"] == "FN" else "FALSE ALARM"
        lines.append(f"- **{r['id']}** run {r['run']} — {kind}. Planted: {r['planted'] or 'nothing (faithful item)'}")
        said = r.get("must_fix")
        if said:
            for m in said[:4]:
                lines.append(f"  - editor said: {str(m)[:300]}")
        elif r.get("editor_verdict") is not None:
            lines.append(f"  - editor verdict {r['editor_verdict']} with no concerns listed")
        else:
            lines.append("  - baseline heuristic: no unmatched number, so nothing to flag")
    return lines


def _append_history(args, model, tp, fn, tn, fp, recall, fpr, misses, false_alarms) -> None:
    """Append one row to the trend log. Created on first use."""
    from datetime import date
    row = (f"| {date.today().isoformat()} | {args.backend} | {model} | {args.runs} "
           f"| {tp}/{tp+fn} = {recall:.0%} | {fp}/{fp+tn} = {fpr:.0%} "
           f"| {', '.join(sorted(set(misses))) or 'none'} "
           f"| {', '.join(sorted(set(false_alarms))) or 'none'} |\n")
    if not HISTORY.exists():
        HISTORY.write_text(_HISTORY_HEADER)
    with HISTORY.open("a") as fh:
        fh.write(row)


if __name__ == "__main__":
    raise SystemExit(main())
