#!/usr/bin/env python3
"""The generation stop — one switch that closes every path to the agent loop.

WHY THIS EXISTS
On 2026-08-04 David paused all SENDING (scripts/send_pause.py) and deliberately
left generation running, so there would be a fresh draft every weekday to look
at while the pipeline got fixed. On 2026-08-11 he stopped active work on both
remaining audiences (Nursing and the AI PM Brief). Generation that nobody
reviews is not a pipeline you can watch, it is an API bill: every weekday night
a full agent loop runs, fetches, writes, fact-checks and commits a draft that
no human will open.

So this is the second half of the same switch. The send pause answers "can this
reach a reader". This answers "should we spend money making it at all".

WHAT IT BLOCKS
A fresh agent run: scripts/run_newsletter.py without --from-payload, from any
caller — the nightly-generate cron, its manual dispatch, or a local run. That
is the only path that spends API credit.

WHAT IT DOES NOT BLOCK, ON PURPOSE
Replays. --from-payload re-renders an already-sourced payload for $0 and never
touches the agent loop, which is how preview-send.yml works and how every
render fix gets verified. Blocking it would stop the fixing that this stop
exists to make affordable. Also untouched: the review console, the bit queue,
the gallery refresh, and the PAUSED send gate, which stays exactly as it was.

WHY A FILE AND NOT THE GITHUB UI
Disabling the cron in GitHub's UI closes one caller and closes it invisibly:
nothing in the tree would say why the drafts stopped, and in six weeks nobody
remembers. A file in the repo says it in git history, shows up in `git status`
the moment someone deletes it, and every generate path reads that one file.
Same argument as send_pause.py, same shape, same resume story.

WHY NOT SHARED CODE WITH send_pause.py
The mechanics are near-identical and factoring them together was tempting. It
would mean editing the module that is the only thing currently holding sends,
with twelve tests bound to it, while a live pause depends on it. A refactor
whose worst-case failure is "sends silently resume" is not worth ~40 saved
lines. They can converge later, when nothing is riding on either one.

TO RESUME
    git rm GENERATION_STOPPED && git commit -m "resume generation" && git push

One step, and the next weekday 02:00 UTC run generates normally. Nothing else
has to be re-enabled: the cron was never disabled, it runs every night and
refuses out loud, which is how you can see the stop working.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Repo root, next to PAUSED. The two switches should be visible together — the
# state of this pipeline is "what markers are lying at the top of the repo".
MARKER_NAME = "GENERATION_STOPPED"

_DEFAULT = f"generation is stopped; no reason recorded in {MARKER_NAME}"


def marker_path(repo: Path | None = None) -> Path:
    """Resolved per call, against the CALLER's repo root.

    Same reason send_pause does it this way: binding the path once at import
    means a live marker in the developer's checkout leaks into every test that
    points its module at a tmp tree, and a gate that checks a different tree
    than the one it acts on is the bug class this repo keeps paying for.
    """
    return (repo or REPO) / MARKER_NAME


def is_stopped(repo: Path | None = None) -> bool:
    return marker_path(repo).exists()


def reason(repo: Path | None = None) -> str:
    """The first non-empty, non-comment line of GENERATION_STOPPED.

    Read defensively: a marker that exists but cannot be read still means
    stopped. Failing to parse the reason must never re-open the gate.
    """
    try:
        for raw in marker_path(repo).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return _DEFAULT


def banner(nl: str, caller: str, repo: Path | None = None) -> str:
    return (
        f"[{caller}] {nl}: GENERATION IS STOPPED — {reason(repo)}\n"
        f"[{caller}] {nl}: no agent ran, no API credit spent, no draft written. "
        f"Replays (--from-payload) and previews still work. To resume, delete "
        f"{MARKER_NAME}."
    )
