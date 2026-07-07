.PHONY: test evals evals-llm review update-goldens install-dev

# Stock macOS ships only `python3` — a bare `python` makes every target die on
# line 1. Route interpreter calls through one overridable var:
#   make test PYTHON=python3.11
PYTHON ?= python3

# Run the full test suite. Zero API calls — purely local Python.
test:
	$(PYTHON) -m pytest tests/ -v

# Fabrication-detection eval — FREE baseline backend (a label-blind numeric
# heuristic; $0, no API key). The baseline is INTENTIONALLY weak — it's the
# comparison floor, so a low recall here (~33%) is expected, not a bug. The
# committed scorecard (tests/evals/scorecard.md, the real Sr. Editor, 100%) is
# the showcase; reproduce it with `make evals-llm`. Prints only — never
# overwrites the committed scorecard.
evals:
	$(PYTHON) -m tests.evals.run_evals --backend baseline

# Fabrication-detection eval, REAL Sr. Editor. ~1 API call per item per run;
# needs ANTHROPIC_API_KEY. --runs averages LLM variance. Writes the scorecard.
evals-llm:
	$(PYTHON) -m tests.evals.run_evals --backend llm --runs 3 --write

# Build the side-by-side HITL review console from the bundled sample issue and
# open it. Runs offline (falls back to source excerpts if an article won't
# fetch), no API key needed.
review:
	$(PYTHON) -m scripts.build_review --bundle review/sample-bundle.json --out review/review.html
	open review/review.html

# Re-bless the golden HTML + plaintext snapshots after an intentional renderer
# change. Always inspect `git diff tests/fixtures` afterward before committing.
update-goldens:
	UPDATE_GOLDENS=1 $(PYTHON) -m pytest tests/test_render_snapshot.py -v

# One-shot pip install for the test toolchain.
install-dev:
	pip install pytest

# The interactive "Newsletter Studio" walkthrough is hosted, not local — see it
# run at https://jungdjh.github.io/newsletter-platform (no clone or keys needed).
