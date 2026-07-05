"""Entry point: generate and send a newsletter end-to-end.

Usage:
    python scripts/run_newsletter.py --newsletter <audience>
                                      [--dry-run]
                                      [--to <email>]
                                      [--create-draft-only]
                                      [--save-html <path>]

In production (GitHub Actions):
    python scripts/run_newsletter.py --newsletter <audience>

For first-week test runs (just operator + reviewer):
    python scripts/run_newsletter.py --newsletter <audience> \\
        --to operator@example.com --to reviewer@example.com \\
        --create-draft-only

For local dry runs (skip Gmail, just write HTML to disk):
    python scripts/run_newsletter.py --newsletter <audience> \\
        --dry-run --save-html /tmp/out.html
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# agent_loop, sr_editor, and notion/gmail tools are imported lazily inside
# main() so that --from-payload + --skip-editor + --dry-run works locally
# without ANTHROPIC_API_KEY or the anthropic SDK installed (zero-API replay).
from scripts.render_html import render_newsletter, _effective_palette  # noqa: E402


# Sr. Editor is now ADVISORY, not a hard gate. The agent runs once. Editor
# produces a concerns list that ships attached to the draft as a top banner
# for human review. No regeneration loops — the human is the final editor.
# (Old MAX_EDITOR_RETRIES setting is now meaningless; we always run once.)

# US Central Time = UTC-6 (CDT) or UTC-5 (CST). May 18 = CDT (DST).
CT_OFFSET_HOURS = -5  # CDT in May. Adjust if you need CST in winter.


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


def _build_subject(palette: dict, payload: dict, issue_number) -> str:
    """Lead the subject line with the Big Signal headline — that is what drives
    opens; the sender name already identifies the newsletter in the From field —
    then a compact brand tag. Falls back to brand + issue № on a thin issue with
    no Top Stories. (A bare brand + issue number buries the hook.)"""
    name = str(palette.get("name", "")).strip()
    top = payload.get("top_stories") or []
    if top and isinstance(top[0], dict) and str(top[0].get("headline") or "").strip():
        return f"{str(top[0]['headline']).strip()} · {name}"
    num = f"№ {issue_number:03d}" if isinstance(issue_number, int) else "№ —"
    return f"{name} · {num}"


def _resolve_issue_number(last_issue, payload_issue) -> int:
    """Issue number for a replayed payload: never reuse or regress below the
    next number. A saved payload's issue can be stale (generated when last_issue
    was lower; another issue may have shipped since), so clamp up to
    last_issue+1 rather than trusting the payload blindly."""
    nxt = int(last_issue) + 1
    if payload_issue is None:
        return nxt
    return max(nxt, int(payload_issue))


def _dedup_bench(top_stories, bench):
    """Drop bench reserves that reuse a Top story's source URL — a cheap guard
    against the same article landing both up top and on the bench. Semantic
    near-duplicates on different URLs (e.g. two filings of one legal saga) are
    the prompt's job, not this. Returns (bench, dropped_notes)."""
    def _norm(u):
        return (u or "").split("?")[0].split("#")[0].rstrip("/").lower()
    top_urls = {_norm(s.get("source_url")) for s in (top_stories or []) if _norm(s.get("source_url"))}
    kept, notes = [], []
    for b in (bench or []):
        if _norm(b.get("source_url")) in top_urls:
            notes.append(f"Bench: dropped '{str(b.get('headline', ''))[:50]}' — same source as a Top Story.")
        else:
            kept.append(b)
    return kept, notes


def _enforce_top3_floor(top_stories, bench):
    """Guarantee 3 top stories when the bench allows. Promote highest-ranked
    bench reserves into the top tier until there are 3; if the bench can't fill
    the gap, emit a LOUD anomaly so the shortfall surfaces in review — never pad
    the top tier with a thin other_news item. Deterministic + unit-testable.
    Returns (top_stories, remaining_bench, anomalies). See ticket §4A."""
    top = list(top_stories or [])
    reserve = list(bench or [])
    orig = len(top)
    anomalies: list[str] = []
    while len(top) < 3 and reserve:
        top.append(reserve.pop(0))
    promoted = len(top) - orig
    if promoted:
        anomalies.append(
            f"Top-3 floor: promoted {promoted} bench "
            f"stor{'y' if promoted == 1 else 'ies'} to fill the top tier (had {orig}).")
    if len(top) < 3:
        anomalies.append(
            f"Top-3 floor SHORT: only {len(top)} top "
            f"stor{'y' if len(top) == 1 else 'ies'} — expected 3, and the bench had no "
            f"reserves to promote. Review before sending.")
    return top, reserve, anomalies


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--newsletter", required=True,
                        help="an audience short-name with a briefs/<name>.json spec "
                             "(or a configured audience pack)")
    parser.add_argument("--dry-run", action="store_true", help="Skip Gmail send; just render and exit.")
    parser.add_argument("--create-draft-only", action="store_true", help="Create a Gmail draft instead of sending.")
    parser.add_argument("--to", action="append", default=None, help="Override recipient list (can repeat). When set, no Notion query is done.")
    parser.add_argument("--save-html", help="Optional path to write the rendered HTML to.")
    parser.add_argument("--skip-editor", action="store_true", help="Skip Sr. Editor review (dev mode only).")
    parser.add_argument("--demo", action="store_true", help="Public/GitHub render: omit the 'reply to this email' footer (only shown when actually sending).")
    parser.add_argument("--save-payload", help="Write the structured story payload (with meta) to this JSON path — enables $0 re-rendering later via --from-payload.")
    parser.add_argument(
        "--from-payload",
        help=(
            "Replay mode: skip the agent loop and load the story payload from a "
            "JSON file. Use a test fixture (tests/fixtures/*_input.json) or any "
            "previously-saved agent output. Combine with --skip-editor + --dry-run "
            "+ --save-html for a fully-local zero-API iteration loop on render + "
            "send-path changes."
        ),
    )
    args = parser.parse_args()

    newsletter = args.newsletter
    print(f"[{newsletter}] Starting run at {datetime.now(timezone.utc).isoformat()}")

    # Load state. Platform newsletters (any briefs/<name>.json audience) may not
    # have a state file yet — default to issue 0 / never.
    state_path = REPO_ROOT / "state" / f"{newsletter}.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = {"newsletter": newsletter, "last_issue": 0,
                 "last_run_at": "never", "last_feedback_check_at": "never"}
        print(f"[{newsletter}] no state file — starting at issue 1 (platform newsletter)")
    issue_number = int(state["last_issue"]) + 1

    # Load prompt. run_agent uses make_compact_prompt(newsletter) for the brief;
    # prompts/<name>.md is legacy/unused, so it's optional for platform newsletters.
    prompt_path = REPO_ROOT / "prompts" / f"{newsletter}.md"
    prompt_text = prompt_path.read_text() if prompt_path.exists() else ""

    # Compute meta
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

    # 1. Agent loop → story payload (ONE shot, no regeneration)
    if args.from_payload:
        # Replay mode — skip the agent loop entirely, load a saved payload.
        # Supports two shapes:
        #   (a) Fixture format: {"content": {...}, "meta": {...}, ...}
        #   (b) Raw agent output: {"top_stories": [...], "other_news": [...], ...}
        # If fixture format includes a `meta`, it overrides the computed meta above.
        print(f"[{newsletter}] Replay mode — loading payload from {args.from_payload}")
        saved = json.loads(Path(args.from_payload).read_text())
        if "content" in saved:
            payload = saved["content"]
            if "meta" in saved:
                meta = saved["meta"]
                issue_number = _resolve_issue_number(
                    state["last_issue"], meta.get("issue_number"))
                meta["issue_number"] = issue_number
        else:
            payload = saved
    else:
        print(f"[{newsletter}] Running agent loop...")
        from scripts.agent_loop import run_agent  # lazy import — needs anthropic
        try:
            payload = run_agent(
                newsletter=newsletter,
                prompt_text=prompt_text,
                state=state,
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
        print(f"  {i+1:02d}{'  [BIG SIGNAL]' if i==0 else ''}  {s.get('headline','')[:80]}")
    print(f"[{newsletter}] Other news items: {len(payload.get('other_news', []))}")

    # Belt-and-suspenders freshness check. Brief tells the agent to enforce
    # this; this is the post-hoc sanity check at the orchestration layer.
    # Stale stories aren't auto-rejected (model might have judgment we lack),
    # but they're surfaced loudly + appended to anomalies so the human reviewer
    # sees them immediately. A story whose source URL is dated well before the
    # freshness floor (e.g. 18 days stale) is the failure mode this catches.
    from datetime import date as _date
    from scripts.freshness import freshness_floor  # stdlib-only — replay-safe
    _y, _m, _d = (int(x) for x in today_date_iso.split("-"))
    _earliest = freshness_floor(today_date_iso, state.get("last_run_at"))
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
            msg = f"Story {i+1:02d} published {pub_dt.isoformat()} ({age_days}d old, beyond {_earliest.isoformat()} cutoff) — agent freshness gate failed"
            stale_findings.append(msg)
            print(f"[{newsletter}] STALE: {msg}", file=sys.stderr)
    if stale_findings:
        payload.setdefault("anomalies", []).extend(stale_findings)

    # Other News: same freshness cutoff, but DROP stale items outright (low-stakes
    # skim items — safe to remove). Belt-and-suspenders behind the brief's gate.
    kept_other, dropped_other = _drop_stale_other_news(
        payload.get("other_news") or [], _earliest, _date(_y, _m, _d))
    if dropped_other:
        payload["other_news"] = kept_other
        payload.setdefault("anomalies", []).extend(dropped_other)
        for _m in dropped_other:
            print(f"[{newsletter}] {_m}", file=sys.stderr)

    # Drop any bench reserve that reuses a Top story's source (cheap dup guard)
    # BEFORE the floor could promote it. Semantic near-dups are the prompt's job.
    bench_deduped, dedup_notes = _dedup_bench(
        payload.get("top_stories") or [], payload.get("bench") or [])
    payload["bench"] = bench_deduped
    if dedup_notes:
        payload.setdefault("anomalies", []).extend(dedup_notes)
        for _n in dedup_notes:
            print(f"[{newsletter}] {_n}", file=sys.stderr)

    # Top-3 floor: promote bench reserves into the top tier when short; a
    # shortfall the bench can't fill logs a loud anomaly (never pad with a thin
    # other_news item). This is what prevents a "1 top story, 5 in Other" issue
    # shipping silently. See ticket newsletter-content-flow §4A.
    top_final, bench_left, floor_notes = _enforce_top3_floor(
        payload.get("top_stories") or [], payload.get("bench") or [])
    payload["top_stories"] = top_final
    payload["bench"] = bench_left
    if floor_notes:
        payload.setdefault("anomalies", []).extend(floor_notes)
        for _n in floor_notes:
            print(f"[{newsletter}] {_n}", file=sys.stderr)

    # 2. Sr. Editor — ADVISORY only. Produces concerns list, never blocks.
    editor_concerns: dict[str, Any] | None = None
    if not args.skip_editor:
        print(f"[{newsletter}] Sr. Editor advisory review...")
        from scripts.sr_editor import review as editor_review  # lazy import — needs anthropic
        editor_concerns = editor_review(
            newsletter=newsletter,
            story_payload=payload,
            today_date_iso=today_date_iso,
            current_time_ct=current_time_ct,
            earliest_acceptable_date=_earliest.isoformat(),
        )
        print(f"[{newsletter}] Sr. Editor: {editor_concerns['verdict']} with {len(editor_concerns.get('must_fix', []))} concerns")
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

    if args.dry_run:
        print(f"[{newsletter}] Dry run complete. Not sending.")
        print("\n--- PLAINTEXT ---")
        print(plaintext)
        return 0

    # 4. Recipients
    if args.to:
        to_list = args.to
        bcc_list: list[str] = []
        print(f"[{newsletter}] Recipient override: {to_list}")
    else:
        print(f"[{newsletter}] Fetching active recipients from Notion...")
        from scripts.tools import notion_client  # lazy import
        recipients = notion_client.get_active_recipients(newsletter)
        all_emails = [r["email"] for r in recipients]
        # By convention: send TO operator, BCC everyone else. The operator
        # comes from OPERATOR_EMAIL (env) or the audience config pack; with
        # neither configured, everyone goes in To:.
        from scripts import audience_config
        operator_email = os.environ.get("OPERATOR_EMAIL") or audience_config.operator_email()
        if operator_email:
            to_list = [operator_email]
            bcc_list = [e for e in all_emails if e.lower() != operator_email.lower()]
        else:
            to_list = all_emails
            bcc_list = []
        print(f"[{newsletter}] To: {to_list}; BCC: {len(bcc_list)} recipients")

    # 5. Send
    palette = _effective_palette(newsletter)
    subject = _build_subject(palette, payload, issue_number)

    print(f"[{newsletter}] {'Creating draft' if args.create_draft_only else 'Sending'}...")
    from scripts.tools import gmail_send  # lazy import
    try:
        message_id = gmail_send.send(
            to=to_list,
            subject=subject,
            html_body=html,
            plaintext_body=plaintext,
            bcc=bcc_list or None,
            create_draft_only=args.create_draft_only,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[{newsletter}] Send failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return 4

    print(f"[{newsletter}] {'Draft' if args.create_draft_only else 'Message'} ID: {message_id}")

    # 6. Update state — ONLY on a real send. Draft-only runs (the nightly
    # "generate for review" step) are provisional: they must NOT burn the issue
    # number. The issue advances when the approved issue actually sends (the
    # 7 AM job runs WITHOUT --create-draft-only).
    if args.create_draft_only:
        print(f"[{newsletter}] Draft only — issue {issue_number} stays pending (state not advanced).")
    else:
        state_new = {
            "newsletter": newsletter,
            "last_issue": issue_number,
            "last_run_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_feedback_check_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        state_path.write_text(json.dumps(state_new, indent=2) + "\n")
        print(f"[{newsletter}] State updated: issue {issue_number}, run {state_new['last_run_at']}")

    # 7. Summary
    print(f"\n[{newsletter}] ✓ Run complete.")
    print(f"  Issue: № {issue_number:03d}")
    top = payload.get("top_stories", []) or []
    print(f"  Top stories: {len(top)}")
    for i, s in enumerate(top):
        label = " [BIG SIGNAL]" if i == 0 else ""
        print(f"    {i+1:02d}{label}  {s.get('headline','')[:80]}")
    print(f"  Other news items: {len(payload.get('other_news', []))}")
    if payload.get("anomalies"):
        print(f"  Anomalies: {payload['anomalies']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
