"""Entry point: generate a newsletter for an audience and render it to HTML.

The pipeline: agent loop (research + fact-checked draft) → Sr. Editor advisory
review → freshness gate → Outlook-safe HTML + plaintext. Output is written to
disk; this build does not send email (delivery is intentionally out of scope —
see docs/design-decisions.md).

Usage:
    # Generate a fresh issue for an audience (needs ANTHROPIC_API_KEY):
    python -m scripts.run_newsletter --newsletter ai-pms --save-html out.html

    # $0 replay: re-render a previously saved payload, no API calls:
    python -m scripts.run_newsletter --newsletter ai-pms \\
        --from-payload review/ai-pms-ranked.json --skip-editor --save-html out.html

`--newsletter` is the audience slug; it must have a briefs/<slug>.json spec
(create one with scripts.brief_generator).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# agent_loop and sr_editor are imported lazily inside main() so that
# --from-payload + --skip-editor works locally with no ANTHROPIC_API_KEY
# (zero-API replay for iterating on rendering).
from scripts.render_html import render_newsletter  # noqa: E402

# Display timezone for the issue meta (US Central). Adjust to taste.
CT_OFFSET_HOURS = -5


def _drop_stale_other_news(items, earliest_date, today_date):
    """Strict freshness gate for Other News: drop any item whose published_at is
    before earliest_date. Unlike Top Stories (flagged, never auto-dropped — an
    empty issue is worse), Other News are low-stakes skim items, so stale ones
    are removed outright. Items with missing/unparseable published_at are KEPT —
    we can't prove staleness, and we never want to drop on a guess.
    Returns (kept_items, dropped_messages)."""
    from dateutil import parser as _dp
    kept: list[dict] = []
    dropped: list[str] = []
    for item in items:
        pub = item.get("published_at")
        if pub:
            try:
                pub_dt = _dp.parse(pub).date()
            except Exception:  # noqa: BLE001 — unparseable date → keep, can't prove stale
                pub_dt = None
            if pub_dt is not None and pub_dt < earliest_date:
                age = (today_date - pub_dt).days
                dropped.append(
                    f"DROPPED stale Other News '{item.get('headline', '')[:60]}' — "
                    f"published {pub_dt.isoformat()} ({age}d old, before "
                    f"{earliest_date.isoformat()} cutoff)")
                continue
        kept.append(item)
    return kept, dropped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--newsletter", required=True,
                        help="audience slug with a briefs/<slug>.json spec, e.g. 'ai-pms'")
    parser.add_argument("--issue", type=int, default=1, help="issue number to stamp (default 1).")
    parser.add_argument("--save-html", help="path to write the rendered HTML to.")
    parser.add_argument("--save-payload", help="write the structured payload (with meta) to this JSON "
                                               "path — enables $0 re-rendering later via --from-payload.")
    parser.add_argument("--skip-editor", action="store_true", help="skip the Sr. Editor advisory review.")
    parser.add_argument("--demo", action="store_true",
                        help="public render: omit the reply-to footer + 'internal use only' notice.")
    parser.add_argument("--print-plaintext", action="store_true", help="also print the plaintext body.")
    parser.add_argument("--from-payload",
                        help="replay mode: skip the agent loop and load the payload from this JSON file "
                             "(a saved run or a tests/fixtures payload). Pairs with --skip-editor for $0 "
                             "iteration on rendering.")
    args = parser.parse_args()

    newsletter = args.newsletter
    issue_number = args.issue
    print(f"[{newsletter}] Starting run at {datetime.now(timezone.utc).isoformat()}")

    now_utc = datetime.now(timezone.utc)
    ct_now = now_utc + timedelta(hours=CT_OFFSET_HOURS)
    meta = {
        "issue_number": issue_number,
        "date_dd_mm_yy": ct_now.strftime("%d.%m.%y"),
        "weekday_str": ct_now.strftime("%a"),
        "edition_label": "MORNING EDI." if ct_now.hour < 12 else "AFTERNOON EDI.",
        "filed_time_ct": ct_now.strftime("%H:%M CT"),
        "min_read": 6,
    }
    today_date_iso = ct_now.strftime("%Y-%m-%d")
    current_time_ct = ct_now.strftime("%H:%M CT")

    # 1. Agent loop → story payload (or replay a saved payload)
    if args.from_payload:
        print(f"[{newsletter}] Replay mode — loading payload from {args.from_payload}")
        saved = json.loads(Path(args.from_payload).read_text())
        if "content" in saved:
            payload = saved["content"]
            if "meta" in saved:
                meta = saved["meta"]
                issue_number = int(meta.get("issue_number", issue_number))
        else:
            payload = saved
    else:
        print(f"[{newsletter}] Running agent loop...")
        from scripts.agent_loop import run_agent  # lazy import — needs anthropic
        try:
            payload = run_agent(
                newsletter=newsletter,
                prompt_text="",
                state={"last_run_at": "never", "last_feedback_check_at": "never"},
                today_date_iso=today_date_iso,
                issue_number=issue_number,
                editor_must_fix=None,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{newsletter}] Agent loop failed: {e}", file=sys.stderr)
            traceback.print_exc()
            return 2

    top = payload.get("top_stories", []) or []
    print(f"[{newsletter}] Top stories: {len(top)}")
    for i, s in enumerate(top):
        print(f"  {i+1:02d}{'  [BIG SIGNAL]' if i == 0 else ''}  {s.get('headline','')[:80]}")
    print(f"[{newsletter}] Other news items: {len(payload.get('other_news', []))}")

    # Belt-and-suspenders freshness check at the orchestration layer. Stale Top
    # Stories are surfaced loudly (appended to anomalies) for the human reviewer;
    # stale Other News are dropped outright.
    from datetime import date as _date, timedelta as _timedelta
    _y, _m, _d = (int(x) for x in today_date_iso.split("-"))
    _earliest = _date(_y, _m, _d) - _timedelta(days=3)
    stale_findings: list[str] = []
    for i, s in enumerate(top):
        pub = s.get("published_at")
        if not pub:
            continue
        try:
            from dateutil import parser as _dp
            pub_dt = _dp.parse(pub).date()
        except Exception:  # noqa: BLE001
            continue
        if pub_dt < _earliest:
            age_days = (_date(_y, _m, _d) - pub_dt).days
            msg = (f"Story {i+1:02d} published {pub_dt.isoformat()} ({age_days}d old, beyond "
                   f"{_earliest.isoformat()} cutoff) — agent freshness gate failed")
            stale_findings.append(msg)
            print(f"[{newsletter}] STALE: {msg}", file=sys.stderr)
    if stale_findings:
        payload.setdefault("anomalies", []).extend(stale_findings)

    kept_other, dropped_other = _drop_stale_other_news(
        payload.get("other_news") or [], _earliest, _date(_y, _m, _d))
    if dropped_other:
        payload["other_news"] = kept_other
        payload.setdefault("anomalies", []).extend(dropped_other)
        for _msg in dropped_other:
            print(f"[{newsletter}] {_msg}", file=sys.stderr)

    # 2. Sr. Editor — ADVISORY only. Produces a concerns list, never blocks.
    editor_concerns: dict[str, Any] | None = None
    if not args.skip_editor:
        print(f"[{newsletter}] Sr. Editor advisory review...")
        from scripts.sr_editor import review as editor_review  # lazy import — needs anthropic
        editor_concerns = editor_review(
            newsletter=newsletter,
            story_payload=payload,
            today_date_iso=today_date_iso,
            current_time_ct=current_time_ct,
        )
        print(f"[{newsletter}] Sr. Editor: {editor_concerns['verdict']} with "
              f"{len(editor_concerns.get('must_fix', []))} concerns")
        for c in editor_concerns.get("must_fix", []):
            print(f"  • {c}")

    # 3. Render — editor concerns attached as a top banner if any
    print(f"[{newsletter}] Rendering HTML...")
    if args.save_payload:
        Path(args.save_payload).write_text(
            json.dumps({"newsletter": newsletter, "content": payload, "meta": meta},
                       indent=2, ensure_ascii=False))
        print(f"[{newsletter}] payload saved → {args.save_payload}")

    html, plaintext = render_newsletter(newsletter, payload, meta, editor_concerns=editor_concerns,
                                        include_reply_footer=not args.demo)

    if args.save_html:
        Path(args.save_html).write_text(html)
        print(f"[{newsletter}] HTML written to {args.save_html}")

    if args.print_plaintext:
        print("\n--- PLAINTEXT ---")
        print(plaintext)

    print(f"\n[{newsletter}] ✓ Done. Issue № {issue_number:03d} · "
          f"{len(top)} top stories · {len(payload.get('other_news', []))} other news.")
    if payload.get("anomalies"):
        print(f"  Anomalies: {payload['anomalies']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
