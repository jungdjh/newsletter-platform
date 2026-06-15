#!/usr/bin/env python3
"""Newsletter quality eval harness — fabrication detection.

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
  python -m tests.evals.run_evals --backend llm --runs 3 --newsletter ai-pms
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "fabrication.jsonl"
SCORECARD = Path(__file__).parent / "scorecard.md"

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


def llm_verdict(story: dict, newsletter: str) -> str:
    """Run the real Sr. Editor over a one-story payload."""
    from scripts.sr_editor import review  # lazy — needs anthropic + API key
    payload = {"top_stories": [story], "other_news": []}
    result = review(
        newsletter=newsletter,
        story_payload=payload,
        today_date_iso="2026-05-27",
        current_time_ct="07:00 CT",
    )
    return str(result.get("verdict", "FAIL")).upper()


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["baseline", "llm"], default="baseline")
    ap.add_argument("--runs", type=int, default=1, help="repeats per item (LLM variance)")
    ap.add_argument("--newsletter", default="ai-pms")
    ap.add_argument("--write", action="store_true", help="write scorecard.md")
    args = ap.parse_args()

    items = [json.loads(l) for l in FIXTURES.read_text().splitlines() if l.strip()]
    cells = {"TP": 0, "FN": 0, "TN": 0, "FP": 0}
    misses, false_alarms = [], []

    for it in items:
        for _ in range(args.runs):
            if args.backend == "baseline":
                verdict = baseline_verdict(it["story"])
            else:
                verdict = llm_verdict(it["story"], args.newsletter)
            cell = classify(it["label"], verdict)
            cells[cell] += 1
            if cell == "FN":
                misses.append(it["id"])
            if cell == "FP":
                false_alarms.append(it["id"])

    tp, fn, tn, fp = cells["TP"], cells["FN"], cells["TN"], cells["FP"]
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0

    lines = [
        f"# Fabrication-detection eval — backend={args.backend}, runs={args.runs}",
        "",
        f"- Items: {len(items)}  (fabricated {tp+fn}, faithful {tn+fp})",
        f"- **Recall (catch rate): {tp}/{tp+fn} = {recall:.0%}**   misses: {sorted(set(misses)) or 'none'}",
        f"- **False-positive rate: {fp}/{fp+tn} = {fpr:.0%}**   false alarms: {sorted(set(false_alarms)) or 'none'}",
        "",
        f"  confusion: TP={tp} FN={fn} TN={tn} FP={fp}",
    ]
    out = "\n".join(lines)
    print(out)
    if args.write:
        SCORECARD.write_text(out + "\n")
        print(f"\n→ wrote {SCORECARD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
