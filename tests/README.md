# Tests

Run everything (offline, no API key needed):

```bash
make test          # or: python -m pytest tests/ -q
```

## What's covered

| File | What it checks |
|---|---|
| `test_brief_generator.py` | `assemble_brief` templates a spec into the fixed engine contract; the spec-driven routing in `compact_prompts`. |
| `test_sourcing.py` | The web_search allowlist builder (`build_tool_schemas`), domain normalization, the crawler-inaccessible-domain prune, and `get_sources`. |
| `test_feeds.py` | RSS ingestion: freshness filter, dedupe, dead-feed handling, and `get_feeds`. |
| `test_candidate_ranker.py` | Haiku relevance ranking: sort/cap, unscored defaults, graceful fallback (uses a fake client — no API). |
| `test_freshness.py` | The strict Other-News freshness gate (`_drop_stale_other_news`). |
| `test_editor_response.py` | Parsing/repair of the Sr. Editor's JSON verdict. |
| `test_render_snapshot.py` | Frozen golden snapshot of the rendered HTML + plaintext for the `ai-pms` example. |
| `test_render_invariants.py` | Hard render guarantees (no broken style attrs, issue number present, every source URL appears). |
| `evals/` | The fact-checking eval harness — see below. |

## Snapshot goldens

`test_render_snapshot.py` renders `tests/fixtures/ai-pms_input.json` and diffs the
output against `ai-pms_golden.{html,txt}`. After an intentional render change:

```bash
make update-goldens        # re-blesses the goldens
git diff                   # review, then commit
```

## Evals

The fabrication-detection harness lives in `tests/evals/`. It feeds labeled
payloads (`fixtures/fabrication.jsonl`, faithful/fabricated pairs with planted
defects) through the Sr. Editor and scores recall + false-positive rate.

```bash
make evals          # free baseline backend
make evals-llm      # real Sr. Editor (needs ANTHROPIC_API_KEY)
```
