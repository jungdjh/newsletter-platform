"""Cadence-aware freshness cutoff — shared by the agent loop and the orchestrator.

Dependency-free (stdlib only) so it stays importable in zero-API replay mode.
"""

from __future__ import annotations

from datetime import date, timedelta


def freshness_floor(today_iso: str, last_run_at: str | None = None,
                    min_window_days: int = 10, max_window_days: int = 21) -> date:
    """Earliest publish date a story may have.

    Weekly cadence: anchor the window to the last SENT date so each issue covers
    everything *since the previous one*. If a week was skipped, the window widens
    to cover the gap instead of orphaning stories that are newer than the last
    issue but older than a fixed 7-day window. The sent-stories ledger
    (sent_ledger.py) separately prevents repeats, so a slightly wide window that
    overlaps the prior issue is safe.

    The window is clamped to [min_window_days, max_window_days]:
      - never narrower than min_window_days — a same-day manual re-run, or a
        newsletter with no send history yet, still looks back ~10 days instead
        of starving; the ledger handles any overlap-driven repeats.
        NOTE: on a weekly cadence the gap is ~7 days, so this min is what
        actually sets the window on a normal run — it is not just a floor for
        edge cases. Keep it in sync with `get_freshness_window`'s default and
        with the "~10 days" the briefs document.
      - never wider than max_window_days — a long gap or a first issue won't drag
        in stale news.
    """
    today = date.fromisoformat(today_iso)
    window = min_window_days
    if last_run_at and last_run_at != "never":
        try:
            window = (today - date.fromisoformat(last_run_at[:10])).days
        except (ValueError, TypeError):
            window = min_window_days
    window = min(max(window, min_window_days), max_window_days)
    return today - timedelta(days=window)
