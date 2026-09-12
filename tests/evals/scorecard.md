# Fabrication-detection eval — backend=llm, runs=3

- Date: 2026-09-12   model: claude-sonnet-4-6
- Command: `python -m tests.evals.run_evals --backend llm --runs 3 --write`
- Items: 19 fixtures × 3 runs = 57 judgements (11 fabricated, 8 faithful fixtures)
- **Recall (catch rate): 33/33 = 100%**   misses: none
- **False-positive rate: 0/24 = 0%**   false alarms: none

  confusion: TP=33 FN=0 TN=24 FP=0

_Scoring: fabricated item caught iff the editor NAMES the planted lie token (robust to phrasing); faithful item is a false alarm iff the editor flags a claim unsupported by the excerpt. The editor's broad advisory verdict (vendor-PR, placeholder URL, voice, over-certainty) is excluded — measuring it as fabrication detection is what produced the misleading 39% earlier._

## Failure cases

None in this run. (That is a claim about this fixture set, not about the editor in general — see the false-positive discussion in history.md.)
