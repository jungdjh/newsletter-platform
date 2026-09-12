# Fabrication-detection eval — run history

One row per `--write` run. The scorecard holds the latest result; this
holds the trend. A drift with no code change means the model moved.

The first two rows are backfilled by hand from the record, not from a live
run: 2026-07-28 from the scorecard committed in `234b449`, 2026-07-29 from the
result quoted in `87f4b1d`'s commit message. Every row from 2026-07-30 onward
is written by the harness.

**What the trend already shows.** Recall has not moved: 100% on every pass.
The false-positive count has, scoring 0, then 1, then 0 out of 24 across three
consecutive passes. So a single run does not measure the false-positive rate to
better than about one item, and any public claim should be stated as a range
rather than as whichever run finished last.

| date | backend | model | runs | recall | false-pos | misses | false alarms |
|---|---|---|---|---|---|---|---|
| 2026-07-28 | llm | claude-sonnet-4-6 | 3 | 33/33 = 100% | 0/24 = 0% | none | none |
| 2026-07-29 | llm | claude-sonnet-4-6 | 3 | 33/33 = 100% | 1/24 = 4% | none | (not recorded) |
| 2026-07-30 | baseline | n/a (offline heuristic) | 1 | 6/11 = 55% | 0/8 = 0% | fab-06, fab-12, nurse-01, nurse-02, nurse-03 | none |
| 2026-07-30 | llm | claude-sonnet-4-6 | 3 | 33/33 = 100% | 0/24 = 0% | none | none |
| 2026-08-01 | llm | claude-sonnet-4-6 | 3 | 33/33 = 100% | 1/24 = 4% | none | fab-09 |
| 2026-09-11 | baseline | n/a (offline heuristic) | 1 | 6/11 = 55% | 0/8 = 0% | fab-06, fab-12, nurse-01, nurse-02, nurse-03 | none |
