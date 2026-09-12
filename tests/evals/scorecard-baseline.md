# Fabrication-detection eval — backend=baseline, runs=1

- Date: 2026-09-11   model: n/a (offline heuristic)
- Command: `python -m tests.evals.run_evals --backend baseline --runs 1 --write`
- Items: 19 fixtures × 1 run = 19 judgements (11 fabricated, 8 faithful fixtures)
- **Recall (catch rate): 6/11 = 55%**   misses: ['fab-06', 'fab-12', 'nurse-01', 'nurse-02', 'nurse-03']
- **False-positive rate: 0/8 = 0%**   false alarms: none

  confusion: TP=6 FN=5 TN=8 FP=0

_Scoring: label-blind numeric heuristic — FAIL iff a significant number in the summary/implications is absent from the source_excerpt. Misses non-numeric fabrications (invented quotes, entities, false exclusivity) by design._

## Failure cases

- **fab-06** run 1 — MISS. Planted: invented CEO quote not in excerpt
  - baseline heuristic: no unmatched number, so nothing to flag
- **fab-12** run 1 — MISS. Planted: invented named partner 'launched with Chase exclusively' not in excerpt
  - baseline heuristic: no unmatched number, so nothing to flag
- **nurse-01** run 1 — MISS. Planted: unsupported superlative 'highest in the US' — excerpt only says 'higher than the $45 national average'
  - baseline heuristic: no unmatched number, so nothing to flag
- **nurse-02** run 1 — MISS. Planted: self-reported poll figure mislabeled as a 'national median'
  - baseline heuristic: no unmatched number, so nothing to flag
- **nurse-03** run 1 — MISS. Planted: invented geography 'Bay Area' — excerpt names New Jersey/California/Oklahoma, never the Bay Area
  - baseline heuristic: no unmatched number, so nothing to flag
