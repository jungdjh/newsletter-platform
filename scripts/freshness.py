"""Cadence-aware freshness cutoff — shared by the agent loop and the orchestrator.

Dependency-free (stdlib only) so it stays importable in zero-API replay mode.
"""

from __future__ import annotations

from datetime import date, timedelta


def freshness_floor(today_iso: str, last_run_at: str | None = None, max_window_days: int = 7) -> date:
    """Earliest publish date a story may have: a steady trailing window back from
    today (default 7 days).

    De-dup is handled separately by the sent-stories ledger (content-based — see
    sent_ledger.py), so the window no longer collapses to the last-run date.
    Anchoring to last_run starved low-volume verticals: when the previous issue
    ran only 2-3 days ago the window shrank to 2-3 days and orphaned bigger
    stories the (often thin) prior issue never actually covered. A trailing week
    + ledger de-dup surfaces the week's genuinely-new developments without
    repeats. The agent still gets a "new since last run" recency hint separately
    (see _build_dynamic_context), so it prioritises fresh news within the window.

    `last_run_at` is accepted for call-site compatibility; it no longer narrows
    the window.
    """
    today = date.fromisoformat(today_iso)
    return today - timedelta(days=max_window_days)
