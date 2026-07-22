"""Per-run token + cost meter.

Every Anthropic API call site (agent_loop, sr_editor) calls `record(resp.usage)`
after a successful response; run_newsletter prints `format_line()` once per run so
each generate reports its real token spend and estimated cost. Process-global —
one accumulator per run (each run_newsletter invocation is a fresh process).

Prices are USD per 1M tokens. Defaults are the Sonnet tier the pipeline uses
(AGENT_MODEL / EDITOR_MODEL); update `_PRICES` if the model changes.
"""

from __future__ import annotations

_PRICES = {
    "input": 3.00,
    "output": 15.00,
    "cache_write": 3.75,   # 5-minute cache write (1.25x input)
    "cache_read": 0.30,    # cache hit (0.1x input)
}

_totals = {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0}


def _get(usage, key: str) -> int:
    if usage is None:
        return 0
    val = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
    return int(val or 0)


def record(usage) -> None:
    """Add one API response's usage (an Anthropic usage object or dict). No-op on None."""
    if usage is None:
        return
    _totals["calls"] += 1
    _totals["input"] += _get(usage, "input_tokens")
    _totals["output"] += _get(usage, "output_tokens")
    _totals["cache_write"] += _get(usage, "cache_creation_input_tokens")
    _totals["cache_read"] += _get(usage, "cache_read_input_tokens")


def cost_usd() -> float:
    return (
        _totals["input"] / 1e6 * _PRICES["input"]
        + _totals["output"] / 1e6 * _PRICES["output"]
        + _totals["cache_write"] / 1e6 * _PRICES["cache_write"]
        + _totals["cache_read"] / 1e6 * _PRICES["cache_read"]
    )


def summary() -> dict:
    return {**_totals, "cost_usd": round(cost_usd(), 4)}


def format_line() -> str:
    s = _totals
    return (
        f"{s['calls']} API calls · in {s['input']:,} / out {s['output']:,} tok"
        f" (cache read {s['cache_read']:,}, write {s['cache_write']:,}) · ~${cost_usd():.2f}"
    )


def reset() -> None:
    for k in _totals:
        _totals[k] = 0
