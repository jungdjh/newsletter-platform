"""Anomaly ranking + the hard repeat guard.

Origin: Ledger № 007 (2026-07-21) showed "⚠ 7 anomaly note(s) — review before
sending" where 6 needed no action. David: "I don't really even know what all
these mean." A gate that gets skimmed stops working.

The real Ledger № 007 notes are the fixture below — if the classifier ever
regresses on them, this fails.
"""
from pathlib import Path

import pytest

from scripts import anomaly_rank as ar
import scripts.run_newsletter as rn

LEDGER_007 = [
    "PaymentsDive surcharge article (825609) and M&A article (825653) did not return "
    "published_at from web_fetch; dates confirmed as 2026-07-20 from RSS feed metadata. "
    "freshness_confirmed set on surcharge story.",
    "PYMNTS CBDC ban article did not return published_at from web_fetch; search result "
    "showed '1 week ago' from July 21 = July 14, exactly at EARLIEST_ACCEPTABLE_DATE "
    "boundary. freshness_confirmed set.",
    "CoinDesk GENIUS Act article (2026-07-19 per URL) did not return published_at from "
    "web_fetch; date confirmed from URL path and search result '2 days ago'. "
    "freshness_confirmed set on bench item.",
    "9to5Mac GWM Car Keys article (2026-07-17) did not return published_at from web_fetch; "
    "date confirmed from URL path and search result '4 days ago'.",
    "No reader feedback received since last run (notion_get_feedback returned empty array).",
    "Hero image sourced from TechCrunch PayPal article; vision-checked and approved.",
]


# ---- classification ----

def test_resolved_date_fallbacks_are_not_decisions():
    # The four "no published_at, but I confirmed it another way" notes are the
    # system working. They must not sit in the ⚠ banner.
    for note in (LEDGER_007[0], LEDGER_007[2], LEDGER_007[3]):
        assert ar.severity(note) == ar.NOTE, note[:60]


def test_freshness_boundary_still_decides():
    # Confirmed AND borderline — the boundary must win over the confirmation.
    assert ar.severity(LEDGER_007[1]) == ar.DECIDE


def test_passing_guards_are_notes():
    assert ar.severity(LEDGER_007[4]) == ar.NOTE   # no reader feedback
    assert ar.severity(LEDGER_007[5]) == ar.NOTE   # vision-checked and approved


def test_top3_shortfall_still_decides():
    assert ar.severity("Top-3 floor SHORT: only 2 top stories — expected 3, and the "
                       "bench had no reserves to promote. Review before sending.") == ar.DECIDE


def test_unrecognized_prose_defaults_to_decide():
    # Fail loud: a note nobody taught us to read is the one worth surfacing.
    assert ar.severity("something nobody has ever written before") == ar.DECIDE


def test_explicit_dict_severity_wins():
    assert ar.severity({"severity": "note", "message": "Review before sending."}) == ar.NOTE
    assert ar.severity({"severity": "decide", "message": "date confirmed"}) == ar.DECIDE


def test_split_keeps_everything_and_ranks_ledger_007():
    decides, notes = ar.split(LEDGER_007)
    assert len(decides) + len(notes) == len(LEDGER_007)   # nothing dropped
    assert decides == [LEDGER_007[1]]                     # only the boundary case
    assert "need" in ar.banner(decides, notes)


def test_empty_and_none_are_safe():
    assert ar.split(None) == ([], [])
    assert ar.split([""]) == ([], [])
    assert ar.banner([], []) == ""


# ---- hard repeat guard ----

def _story(h, url):
    return {"headline": h, "source_url": url}


def test_repeat_dropped_from_top_and_bench(monkeypatch):
    monkeypatch.setattr("scripts.sent_ledger.recently_covered",
                        lambda nl, limit=6: [{"headline": "old", "url": "https://ex.com/a"}])
    top = [_story("repeat", "https://www.EX.com/a/"), _story("fresh", "https://ex.com/b")]
    bench = [_story("bench repeat", "https://ex.com/a")]
    top2, bench2, anoms = rn._drop_repeats("download", top, bench)
    assert [s["headline"] for s in top2] == ["fresh"]     # normalized www/case/slash
    assert bench2 == []                                    # bench repeat pulled too
    assert len(anoms) == 2
    assert all(a["severity"] == "decide" for a in anoms)


def test_no_ledger_means_no_drops(monkeypatch):
    monkeypatch.setattr("scripts.sent_ledger.recently_covered", lambda nl, limit=6: [])
    top = [_story("a", "https://ex.com/a")]
    assert rn._drop_repeats("download", top, [])[0] == top


def test_repeat_guard_never_raises(monkeypatch):
    def boom(nl, limit=6):
        raise RuntimeError("archive unreadable")
    monkeypatch.setattr("scripts.sent_ledger.recently_covered", boom)
    top = [_story("a", "https://ex.com/a")]
    top2, bench2, anoms = rn._drop_repeats("download", top, [])
    assert top2 == top and anoms == []      # fail-open: never block a run


def test_norm_url_identity():
    n = rn._norm_url
    assert n("https://www.Ex.com/A/") == n("http://ex.com/a")
    assert n("") == ""


# ---- 2026-07-21 incident: № 007 shipped empty ----
# nightly_send moves the approved payload into review/sent/ BEFORE invoking
# run_newsletter --from-payload, and recently_covered() globs review/sent/ — so
# the repeat guard read the issue being sent and dropped all of its own stories.
# Two independent regressions: the guard must not run in replay mode, and an
# empty issue must never render or send whatever emptied it.

def test_repeat_guard_would_self_cannibalize_a_sent_payload(monkeypatch):
    # Documents the exact mechanism: the issue's own stories look "covered".
    monkeypatch.setattr("scripts.sent_ledger.recently_covered",
                        lambda nl, limit=6: [{"headline": "Stripe/PayPal",
                                              "url": "https://ex.com/paypal"}])
    top = [_story("Stripe and Advent make $53.4 billion bid for PayPal",
                  "https://ex.com/paypal")]
    kept, _, anoms = rn._drop_repeats("ledger", top, [])
    assert kept == [] and len(anoms) == 1      # this is why replay mode must skip it


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "scripts" / "nightly_send.py").exists(),
    reason="send path is operational code, not mirrored to the public repo",
)
def test_empty_issue_aborts_instead_of_sending(monkeypatch, tmp_path, capsys):
    """The gate that would have stopped № 007 regardless of what emptied it.

    Drives main() end-to-end in replay mode with a payload whose top tier is
    empty, and asserts it returns non-zero WITHOUT ever calling the sender."""
    import json
    import sys as _s
    import scripts.tools.gmail_send as gmail_send

    sent = []
    monkeypatch.setattr(gmail_send, "send", lambda *a, **k: sent.append(a) or "id")

    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({
        "content": {"top_stories": [], "other_news": [{"track": "T", "headline": "o",
                                                       "subtitle": "s",
                                                       "source_url": "https://e/o"}]},
        "meta": {"issue_number": 7, "date_dd_mm_yy": "21.07.26", "weekday_str": "Tue",
                 "edition_label": "MORNING EDI.", "filed_time_ct": "07:00 CT",
                 "min_read": 6},
    }))
    monkeypatch.setattr(_s, "argv", [
        "run_newsletter.py", "--newsletter", "ledger", "--from-payload", str(p),
        "--skip-editor", "--skip-live-verify", "--to", "nobody@example.com"])

    rc = rn.main()
    assert rc != 0, "an empty issue must not exit clean"
    assert sent == [], "an empty issue must never reach the sender"
    err = capsys.readouterr().err
    assert "ABORT" in err and "refusing to render or send" in err, err[-300:]
