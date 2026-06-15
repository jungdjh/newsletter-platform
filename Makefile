.PHONY: test update-goldens install-dev evals evals-llm review

# Build the side-by-side review console (our draft vs. the original article).
review:
	python -m scripts.build_review --from-input review/ai-pms-ranked.json --out review/review.html
	open review/review.html

# Run the full test suite. Zero API calls — purely local Python.
test:
	python -m pytest tests/ -v

# Fabrication-detection eval, FREE baseline backend (numeric heuristic).
# No API key needed — validates the harness + gives a comparison floor.
evals:
	python -m tests.evals.run_evals --backend baseline --write

# Fabrication-detection eval, REAL Sr. Editor. Costs ~1 API call per item
# per run; needs ANTHROPIC_API_KEY. Use --runs for LLM-variance averaging.
evals-llm:
	python -m tests.evals.run_evals --backend llm --runs 3 --write

# Re-bless the golden HTML + plaintext snapshots. Run this when you've
# intentionally changed the renderer and the new output is correct.
# Always inspect `git diff tests/fixtures/*.html` afterward before committing.
update-goldens:
	UPDATE_GOLDENS=1 python -m pytest tests/test_render_snapshot.py -v

# One-shot pip install for the test toolchain.
install-dev:
	pip install pytest
