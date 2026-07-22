"""Layer 3 — the receipt: what actually went on the wire.

Layers 0-2 protect the copy up TO the wire: David approves a rendered artifact
(approved_artifact), the send verifies its sha256 and transmits those exact
bytes without re-rendering. Nothing recorded what came out the other side.

That gap mattered in two concrete ways:

  1. `review/sent/` held the APPROVAL, moved there BEFORE the send — so a send
     that threw left behind a file indistinguishable from a successful one.
     Nothing in the archive said whether an issue reached anybody.

  2. sent_ledger reads that same directory to tell the agent "you already
     covered these." A failed send therefore burned its own stories: nobody
     received them, and the repeat guard dropped them from future issues.
     That is the № 007 failure class again — content vanishing because a guard
     read something it shouldn't have trusted.

So every send now writes a receipt next to the archived payload, recording the
outcome and a sha256 of the exact HTML string handed to the mailer. The receipt
is the archive's answer to "did this ship, and was it what he approved?"

Fail-loud, not fail-silent: a receipt whose shipped hash differs from the
approved hash means bytes changed between the gate and the wire. That aborts
with a non-zero exit so the workflow's failure alert fires.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SENT_DIR = REPO / "review" / "sent"

RECEIPT_VERSION = 1

# Files in review/sent/ that are NOT issue payloads. sent_ledger globs this
# directory, so anything sharing the "<nl>-*.json" shape has to be excluded by
# name or it steals a slot in the recent-issues window.
NON_PAYLOAD_SUFFIXES = (".artifact.json", ".receipt.json")


def digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def stem_for(newsletter: str, issue: Any, stamp: str | None = None) -> str:
    """`<nl>-<issue>-<YYYYMMDD>` — the shared stem of an issue's archived
    payload, artifact and receipt, so one can be found from another."""
    issue_str = f"{issue:03d}" if isinstance(issue, int) else str(issue)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{newsletter}-{issue_str}-{stamp}"


def receipt_path(stem: str, sent_dir: Path | None = None) -> Path:
    """`sent_dir` is threaded through from the caller on purpose. Deriving it
    from this module's own REPO made the receipt escape test isolation: the send
    tests redirect nightly_send.REPO and approved_artifact.REPO at a tmp dir, and
    receipts kept landing in the real review/sent/ — polluting the repo while the
    assertions passed anyway, which is the worst of both."""
    return (sent_dir or SENT_DIR) / f"{stem}.receipt.json"


def build(art: dict[str, Any], *, status: str, html_sent: str, subject_sent: str,
          to: list[str], bcc: list[str], message_id: str = "",
          error: str = "") -> dict[str, Any]:
    """Assemble the receipt. `html_sent` must be the SAME string object passed
    to the mailer — hashing a re-read of the artifact would only prove the file
    matches itself, which verify() already established before the wire."""
    shipped = digest(html_sent)
    approved = art.get("html_sha256") or ""
    return {
        "receipt_version": RECEIPT_VERSION,
        "newsletter": art.get("newsletter"),
        "issue_number": art.get("issue_number"),
        "status": status,                      # "sent" | "failed"
        "approved_sha256": approved,
        "shipped_sha256": shipped,
        "hash_match": bool(approved) and shipped == approved,
        "subject_sent": subject_sent,
        "subject_approved": art.get("subject") or "",
        "subject_match": subject_sent == (art.get("subject") or ""),
        "to": list(to),
        "bcc_count": len(bcc),
        "recipient_count": len(to) + len(bcc),
        "message_id": message_id,
        "error": error,
        "approved_at": art.get("approved_at") or "",
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write(receipt: dict[str, Any], stem: str, sent_dir: Path | None = None) -> Path:
    p = receipt_path(stem, sent_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    return p


def load(stem: str, sent_dir: Path | None = None) -> dict[str, Any] | None:
    p = receipt_path(stem, sent_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def check(receipt: dict[str, Any]) -> tuple[bool, str]:
    """Did the right bytes reach the wire? Anything false here means the send
    path modified content after David's approval — the one thing Layers 0-2
    exist to make impossible, so it is worth shouting about."""
    if not receipt.get("approved_sha256"):
        return False, "artifact carried no approved sha256 — cannot prove what shipped"
    if not receipt.get("hash_match"):
        return False, (f"SHIPPED HTML != APPROVED HTML "
                       f"(approved {receipt['approved_sha256'][:12]}, "
                       f"shipped {receipt['shipped_sha256'][:12]})")
    if not receipt.get("subject_match"):
        return False, (f"shipped subject != approved subject "
                       f"({receipt.get('subject_sent')!r} vs {receipt.get('subject_approved')!r})")
    return True, "shipped bytes match the approval"


def shipped(stem: str, sent_dir: Path | None = None) -> bool:
    """Did this archived issue actually reach recipients?

    No receipt means the issue predates Layer 3 — every one of those did ship,
    so absence must read as shipped. Only an explicit failure excludes it.
    Getting this backwards would un-cover the whole archive at once and hand
    the agent a licence to repeat months of stories."""
    r = load(stem, sent_dir)
    if r is None:
        return True
    return r.get("status") == "sent"


def alert(text: str) -> str:
    """Best-effort shout on a mismatch. The workflow also alerts on a non-zero
    exit; this adds the detail that a generic 'job failed' ping cannot carry."""
    try:
        from scripts.notify_cloud import _send_telegram, _send_email
    except Exception as e:  # noqa: BLE001
        return f"skip: notifier unavailable ({e})"
    tg = _send_telegram(text)
    em = _send_email("🚨 Newsletter send integrity FAILED", text)
    return f"telegram={tg} email={em}"
