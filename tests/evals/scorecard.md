# Fabrication-detection eval — backend=llm, runs=3

- Items: 19 fixtures × 3 runs = 57 judgements (11 fabricated, 8 faithful fixtures)
- **Recall (catch rate): 33/33 = 100%**   misses: none
- **False-positive rate: 1/24 = 4%**   false alarms: ['fab-09']

  confusion: TP=33 FN=0 TN=23 FP=1

_Scoring: fabricated item caught iff the editor NAMES the planted lie token (robust to phrasing); faithful item is a false alarm iff the editor flags a claim unsupported by the excerpt. The editor's broad advisory verdict (vendor-PR, placeholder URL, voice, over-certainty) is excluded — measuring it as fabrication detection is what produced the misleading 39% earlier._
