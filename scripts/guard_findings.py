"""Blocking guard findings — the guards that must halt a send, not narrate one.

Every deterministic guard in this pipeline used to end the same way: append a
string to `payload["anomalies"]` and print to stderr. That list is consumed in
exactly one place — `build_review.py` renders it into the review console — and
nothing ever gated on it. On 2026-07-21 the Top-3 floor logged
`only 0 top stories — expected 3` and the issue reached its readers two seconds
later as a masthead, three Other-News briefs and a footer.

A loud log is not a gate. This module gives the guards that MATTER a shape the
wire can refuse:

    anomalies.append(guard_findings.block("top3_floor", "only 1 top story ..."))

`approved_artifact.build()` copies every blocking finding onto the frozen
artifact. `approved_artifact.verify()` refuses to ship while any of them is unacknowledged.
Acknowledgement is an explicit act by the reviewer, recorded on the artifact, so
"David saw this and shipped anyway" becomes a durable, auditable fact instead of
a click that left no trace.

ONE LIMIT, STATED PLAINLY so the next reader does not over-trust this:
  guard_findings and acknowledged are not covered by html_sha256, so editing
  the artifact JSON by hand can disarm the gate with the digest still valid.

(There used to be a second: `run_newsletter.py` had a direct send that resolved
the real recipient list and never called verify(). Closed 2026-08-01 — that path
now refuses outright, so verify() on the nightly_send path is no longer being
asked to stand in for a gate that did not exist.)

WHY THIS IS NOT SIMPLY "BLOCK ON EVERY `decide` ANOMALY"
-------------------------------------------------------
Measured against the real archive before this was written: live payloads carry
1-10 decide-tier notes each (median ~3), and 175 of 175 saved anomalies are
free-text prose the AGENT wrote about its own sourcing — "og:image rejected,
hero set to null", "publisher returned 403, used the search snippet",
"published_at not returned by web_fetch". Gating on those would demand up to ten
acknowledgements a night for notes that need no action, and would train the
reviewer to clear them reflexively. That is precisely the "gate you learn to
skim" failure `anomaly_rank` was written to fix, re-introduced one layer down.

So blocking findings are a small, explicit, curated set, raised only by
deterministic code and never inferred from prose. Adding one is a deliberate act.
The current set is defined by the four call sites in `run_newsletter.py`:

  top3_floor    fewer top stories than the declared floor (the № 007 shape)
  fabrication   a figure the numeric guard could not tie to its source text
  live_verify   an excerpt absent from the live source page
  stale_story   a top story published before the freshness cutoff

Severity for display is DECIDE: `anomaly_rank` maps BLOCK onto the red banner so
a blocking finding is never quieter than an ordinary decision.
"""
from __future__ import annotations

import hashlib
from typing import Any

BLOCK = "block"


def block(code: str, message: str) -> dict[str, str]:
    """A finding that must halt the send until a human acknowledges it."""
    return {"severity": BLOCK, "code": code, "message": str(message)}


def is_block(a: Any) -> bool:
    return isinstance(a, dict) and str(a.get("severity") or "").lower() == BLOCK


def finding_id(a: Any) -> str:
    """Stable 12-hex id for one finding, over code + message.

    Acknowledgement is keyed on this rather than on list position, so a reviewer
    cannot clear finding #2 by acknowledging #1, and re-running generation with
    genuinely different content produces genuinely different ids."""
    code = str(a.get("code") or "") if isinstance(a, dict) else ""
    msg = str(a.get("message") or "") if isinstance(a, dict) else str(a)
    return hashlib.sha256(f"{code}|{msg}".encode("utf-8")).hexdigest()[:12]


def blocking(anomalies: list[Any] | None) -> list[dict[str, str]]:
    """Every blocking finding in an anomaly list, with its id attached."""
    out: list[dict[str, str]] = []
    for a in anomalies or []:
        if is_block(a):
            out.append({"id": finding_id(a),
                        "code": str(a.get("code") or ""),
                        "message": str(a.get("message") or "")})
    return out


def unacknowledged(findings: list[dict[str, Any]] | None,
                   acknowledged: list[str] | None) -> list[dict[str, Any]]:
    """Findings whose id is not in the acknowledged set. Fail closed: an
    unparseable or missing acknowledgement list clears nothing."""
    ack = {str(x) for x in (acknowledged or []) if x}
    return [f for f in (findings or []) if str(f.get("id") or "") not in ack]
