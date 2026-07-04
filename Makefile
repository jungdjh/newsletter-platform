.PHONY: test update-goldens install-dev evals evals-llm review nightly-review nightly-approve archive notify-install notify-uninstall notify-test nightly-send-now demo mirror

# Build the side-by-side HITL review page and open it (Phase 3 v1).
# Demo from the sample bundle:
#   make review
# From a real payload (re-fetches the source articles):
#   python -m scripts.build_review --from-input <payload>.json --out review/review.html
review:
	python -m scripts.build_review --bundle review/sample-bundle.json --out review/review.html
	open review/review.html

# Live demo web app — "Newsletter Studio": type an audience, watch the n8n-style
# flow run the pipeline, see the responsive issue. Opens http://localhost:8770.
# Live generation needs ANTHROPIC_API_KEY + BRAVE_SEARCH_API_KEY exported;
# the 'Rehearse' mode in the UI needs neither. Or just double-click demo.command.
demo:
	python3 scripts/demo_server.py

# Sync a sanitized copy of the generic engine into the public repo working
# tree: allowlist copy + fail-closed leak scan, then stage (never commit).
# Destination defaults to ~/Documents/newsletter-platform (MIRROR_DEST to override).
mirror:
	bash mirror.sh

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
# Always inspect `git diff tests/fixtures` afterward before committing.
update-goldens:
	UPDATE_GOLDENS=1 python -m pytest tests/test_render_snapshot.py -v

# One-shot pip install for the test toolchain.
install-dev:
	pip install pytest

# --- Nightly review-and-approve loop (local steps) --------------------------
# Step 2: pull the bundle the cloud generated overnight and open the review
# console with an Approve button (click to schedule the 7 AM send). Run in
# your Terminal app:
#   make nightly-review NL=download
nightly-review:
	git pull --quiet
	python3 scripts/review_server.py $(NL)

# Fallback approve (if you'd rather not use the button) — run in Terminal:
#   make nightly-approve NL=download
nightly-approve:
	cp review/pending/$(NL).json review/approved/$(NL).json
	git add review/approved/$(NL).json
	git commit -q -m "[nightly] approve $(NL) to send"
	git push -q
	@echo "✓ Approved $(NL). The 7 AM cloud job will send this issue."

# Late same-day approval — push the approval and trigger the cloud send NOW,
# instead of waiting for the next 7 AM cron. Needs the `gh` CLI, authenticated.
#   make nightly-send-now NL=download
nightly-send-now:
	-git push -q
	gh workflow run nightly-send.yml -f newsletter=$(NL)
	@echo "✓ Triggered the send for $(NL). Watch it: gh run list --workflow=nightly-send.yml"

# Build + open the newsletter archive — every sent issue rendered, with a
# browsable index (filter by month/year). Run in your Terminal app:
#   make archive             (all newsletters)
#   make archive NL=download (just one)
archive:
	python3 scripts/build_archive.py $(NL)
	open review/archive/index.html

# --- Hybrid review notifiers (local launchd agents) -------------------------
# Ambient macOS nudges layered on the cloud loop: a "review me" notification +
# auto-opened console after the nightly generate (Mon/Wed 9 PM PT), and a
# "draft held" reminder on send mornings if you didn't approve (Tue/Thu 7:30 AM).
#   make notify-install      install + load both LaunchAgents
#   make notify-test         fire both notifications now (to see them on screen)
#   make notify-uninstall    remove them
notify-install:
	mkdir -p ~/Library/LaunchAgents
	cp launchd/com.davidjung.newsletter.review-ready.plist ~/Library/LaunchAgents/
	cp launchd/com.davidjung.newsletter.review-held.plist ~/Library/LaunchAgents/
	-launchctl unload ~/Library/LaunchAgents/com.davidjung.newsletter.review-ready.plist 2>/dev/null
	-launchctl unload ~/Library/LaunchAgents/com.davidjung.newsletter.review-held.plist 2>/dev/null
	launchctl load ~/Library/LaunchAgents/com.davidjung.newsletter.review-ready.plist
	launchctl load ~/Library/LaunchAgents/com.davidjung.newsletter.review-held.plist
	@echo "✓ Installed: review-ready (Mon/Wed 9 PM PT) + review-held (Tue/Thu 7:30 AM PT)."

notify-uninstall:
	-launchctl unload ~/Library/LaunchAgents/com.davidjung.newsletter.review-ready.plist 2>/dev/null
	-launchctl unload ~/Library/LaunchAgents/com.davidjung.newsletter.review-held.plist 2>/dev/null
	rm -f ~/Library/LaunchAgents/com.davidjung.newsletter.review-ready.plist ~/Library/LaunchAgents/com.davidjung.newsletter.review-held.plist
	@echo "✓ Removed review notifiers."

notify-test:
	python3 scripts/notify_review.py ready download --no-pull --no-open --force
	python3 scripts/notify_review.py held download --no-pull --force
