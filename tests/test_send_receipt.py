"""Layer 3 — archive exactly what shipped, verified against the approval.

Layers 0-2 protect the copy up to the wire. Nothing recorded what came out the
other side, which left two holes:

  1. review/sent/ held the APPROVAL, moved there before the send. A send that
     threw left a file identical in shape to a successful one — the archive
     could not answer "did this reach anybody?"

  2. sent_ledger reads that directory to tell the agent what is already
     covered. A failed send therefore burned its own stories: nobody got them,
     and the repeat guard dropped them from future issues. Same shape as
     № 007 — content vanishing because a guard trusted the wrong input.

These pin the receipt, and the ledger's new refusal to count a failed send.
"""
import json
from pathlib import Path

import pytest

from scripts import send_receipt as sr

_ART = {
    "newsletter": "nursing",
    "issue_number": 2,
    "subject": "Nursing № 002 — Monday",
    "html": "<html><body>three stories</body></html>",
    "approved_at": "2026-07-21T02:00:00Z",
}
_ART["html_sha256"] = sr.digest(_ART["html"])


@pytest.fixture(autouse=True)
def _tmp_sent(tmp_path, monkeypatch):
    """Point the receipt store at a throwaway dir — never the real archive."""
    monkeypatch.setattr(sr, "SENT_DIR", tmp_path / "sent")
    return tmp_path / "sent"


def _sent_receipt(**over):
    kw = dict(status="sent", html_sent=_ART["html"], subject_sent=_ART["subject"],
              to=["a@example.com"], bcc=["b@example.com", "c@example.com"],
              message_id="smtp-ok:3-recipient(s)")
    kw.update(over)
    return sr.build(_ART, **kw)


# ---- the receipt records what actually went out ----------------------------

def test_receipt_matches_when_the_approved_bytes_shipped():
    r = _sent_receipt()
    assert r["hash_match"] is True
    assert r["subject_match"] is True
    assert r["shipped_sha256"] == _ART["html_sha256"]
    assert r["recipient_count"] == 3
    assert sr.check(r) == (True, "shipped bytes match the approval")


def test_altered_html_between_gate_and_wire_is_caught():
    """The whole point: if anything rewrites the body after approval, the
    receipt must not quietly agree that the right thing shipped."""
    r = _sent_receipt(html_sent="<html><body>zero stories</body></html>")
    assert r["hash_match"] is False
    ok, why = sr.check(r)
    assert ok is False
    assert "SHIPPED HTML != APPROVED HTML" in why


def test_altered_subject_is_caught():
    r = _sent_receipt(subject_sent="Nursing № 003 — wrong issue")
    ok, why = sr.check(r)
    assert ok is False
    assert "subject" in why


def test_an_artifact_with_no_approved_hash_cannot_prove_anything():
    art = {k: v for k, v in _ART.items() if k != "html_sha256"}
    r = sr.build(art, status="sent", html_sent=_ART["html"], subject_sent=_ART["subject"],
                 to=["a@example.com"], bcc=[], message_id="x")
    ok, why = sr.check(r)
    assert ok is False
    assert "cannot prove" in why


def test_receipt_round_trips_to_disk():
    stem = sr.stem_for("nursing", 2, "20260727")
    assert stem == "nursing-002-20260727"
    sr.write(_sent_receipt(), stem)
    back = sr.load(stem)
    assert back["message_id"] == "smtp-ok:3-recipient(s)"
    assert back["status"] == "sent"


def test_a_failed_send_is_recorded_as_failed():
    stem = sr.stem_for("nursing", 2, "20260727")
    sr.write(_sent_receipt(status="failed", message_id="", error="SMTP timeout"), stem)
    assert sr.load(stem)["error"] == "SMTP timeout"
    assert sr.shipped(stem) is False


# ---- "did it ship?" — the question the ledger asks --------------------------

def test_no_receipt_means_shipped_so_the_archive_stays_covered():
    """Every issue sent before Layer 3 lacks a receipt and every one of them
    shipped. Reading absence as 'not shipped' would un-cover the whole archive
    at once and let the agent repeat months of stories."""
    assert sr.shipped("nursing-001-20260720") is True


def test_a_garbled_receipt_does_not_crash_the_ledger(_tmp_sent):
    _tmp_sent.mkdir(parents=True, exist_ok=True)
    (_tmp_sent / "nursing-009-20260727.receipt.json").write_text("{not json")
    assert sr.shipped("nursing-009-20260727") is True   # unreadable -> treat as historical


# ---- the ledger consequences ----------------------------------------------

def _write_issue(sent_dir, stem, url, headline):
    sent_dir.mkdir(parents=True, exist_ok=True)
    (sent_dir / f"{stem}.json").write_text(json.dumps({
        "content": {"top_stories": [{"headline": headline, "source_url": url}], "other_news": []}
    }))


def test_ledger_excludes_stories_from_a_failed_send(_tmp_sent, monkeypatch):
    from scripts import sent_ledger
    monkeypatch.setattr(sent_ledger, "SENT_DIR", _tmp_sent)

    _write_issue(_tmp_sent, "nursing-001-20260720", "https://ex.com/shipped", "Shipped story")
    _write_issue(_tmp_sent, "nursing-002-20260727", "https://ex.com/never", "Never delivered")
    sr.write(_sent_receipt(status="failed", error="SMTP timeout"),
             "nursing-002-20260727")

    urls = [c["url"] for c in sent_ledger.recently_covered("nursing")]
    assert "https://ex.com/shipped" in urls
    assert "https://ex.com/never" not in urls, \
        "a story nobody received must stay eligible for a future issue"


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "scripts" / "nightly_send.py").exists(),
    reason="send path is operational code, not mirrored to the public repo",
)
def test_runs_as_a_script_with_an_approval():
    """The workflow invokes `python scripts/nightly_send.py <nl>`, which puts
    scripts/ on sys.path instead of the repo root — so every `from scripts
    import ...` inside main() raises ModuleNotFoundError.

    nightly_send was the last workflow script without the repo-root guard, and
    the crash was unreachable in testing: with no approval the script returns at
    the gate ABOVE the first such import, so a bare script run exits 0 and looks
    healthy. This test therefore has to seed an approval — running it without
    one reproduces the blind spot instead of the bug.

    Left unfixed, the first genuinely approved send would have been the first
    execution of that line. That is Monday's nursing send."""
    import subprocess, sys as _sys, pathlib, json as _json
    from scripts import approved_artifact as aa

    repo = pathlib.Path(__file__).resolve().parent.parent
    nl = "scripttest"
    html = "<html><body>probe</body></html>"
    art = {"artifact_version": aa.ARTIFACT_VERSION, "newsletter": nl, "issue_number": 42,
           "subject": "probe", "html": html, "plaintext": "p",
           "html_sha256": aa.html_digest(html),
           "recipients_expected": ["a@example.com"], "to": ["a@example.com"], "bcc": [],
           "reply_to": "a@example.com", "top_story_count": 3, "other_news_count": 0,
           "images": [], "approved_at": ""}
    art["artifact_sha256"] = aa.artifact_digest(art)
    approved = repo / "review" / "approved" / f"{nl}.json"
    approved.parent.mkdir(parents=True, exist_ok=True)
    approved.write_text("{}")
    aa.artifact_path(nl).write_text(_json.dumps(art))
    try:
        r = subprocess.run([_sys.executable, str(repo / "scripts" / "nightly_send.py"),
                            nl, "--dry-run"],
                           capture_output=True, text=True, cwd=str(repo))
        assert "ModuleNotFoundError" not in r.stderr, r.stderr[-400:]
        assert r.returncode == 0, r.stderr[-400:]
        assert "DRY RUN" in r.stdout, r.stdout[-400:]
    finally:
        approved.unlink(missing_ok=True)
        aa.artifact_path(nl).unlink(missing_ok=True)


def test_ledger_ignores_artifact_and_receipt_files(_tmp_sent, monkeypatch):
    """They match the <nl>-*.json glob and used to eat slots in the window, so
    asking for the last 6 issues quietly looked at far fewer."""
    from scripts import sent_ledger
    monkeypatch.setattr(sent_ledger, "SENT_DIR", _tmp_sent)

    for i in range(1, 4):
        _write_issue(_tmp_sent, f"nursing-00{i}-2026072{i}", f"https://ex.com/{i}", f"Story {i}")
        (_tmp_sent / f"nursing-00{i}-2026072{i}.artifact.json").write_text(
            json.dumps({"html": "<html></html>", "html_sha256": "x"}))
        sr.write(_sent_receipt(), f"nursing-00{i}-2026072{i}")

    urls = [c["url"] for c in sent_ledger.recently_covered("nursing", limit=3)]
    assert sorted(urls) == ["https://ex.com/1", "https://ex.com/2", "https://ex.com/3"], \
        "all three real issues should fit in a window of 3"
