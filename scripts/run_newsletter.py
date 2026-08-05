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
import re
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
from scripts import guard_findings  # noqa: E402


class RecipientMismatch(RuntimeError):
    """Resolved send envelope diverges from the configured recipient list."""


def assert_full_delivery(newsletter: str, configured: list[dict],
                         to_list: list[str], bcc_list: list[str]) -> None:
    """Refuse to send unless the resolved envelope (To + BCC) covers exactly the
    configured recipient list. Guards the unattended cron path against a silent
    collapse to a partial list (e.g. a config-lookup change quietly falling back
    to operator-only). Raises RecipientMismatch — the send step exits non-zero
    and the workflow's failure hook fires the alert. Never sends to fewer people
    than configured.

    (Added after the 2026-07-07 incident review: the send itself was correct —
    To the operator, BCC the rest of the list, all 3 delivered — but nothing
    proved the envelope matched config, so a real drop would have gone unnoticed.)
    """
    want = {r["email"].strip().lower() for r in configured if r.get("email")}
    got = {e.strip().lower() for e in [*to_list, *bcc_list]}
    if want and got != want:
        raise RecipientMismatch(
            f"{newsletter}: recipient mismatch — refusing to send. "
            f"configured({len(want)})={sorted(want)} resolved({len(got)})={sorted(got)}"
        )


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


def _drop_stale_bench(bench, earliest_date, today_date):
    """Strict freshness gate for bench reserves, BEFORE the Top-3 floor runs.
    A bench reserve exists only to become a Top Story (promoted by the floor, or
    swapped in as a review backfill), so a stale reserve is a stale Top Story
    waiting to happen — the floor must never pad the top tier with one. Same rule
    as Other News: drop reserves published before earliest_date; keep any with a
    missing/unparseable date (can't prove staleness). Returns (kept, dropped_msgs)."""
    from dateutil import parser as _dp
    kept: list[dict] = []
    dropped: list[str] = []
    for item in bench:
        pub = item.get("published_at")
        if pub:
            try:
                pub_dt = _dp.parse(pub).date()
            except Exception:  # noqa: BLE001 — unparseable date → keep, can't prove stale
                pub_dt = None
            if pub_dt is not None and pub_dt < earliest_date:
                age = (today_date - pub_dt).days
                dropped.append(
                    f"DROPPED stale bench reserve '{item.get('headline', '')[:60]}' — "
                    f"published {pub_dt.isoformat()} ({age}d old, before "
                    f"{earliest_date.isoformat()} cutoff) — not eligible for Top-tier promotion")
                continue
        kept.append(item)
    return kept, dropped


def _confirmed_fresh(story, earliest_date):
    """True when a story's recency is CONFIRMED — either a parseable published_at
    on/after the window cutoff, OR an explicit `freshness_confirmed` flag the agent
    sets when the source returns no machine-readable date but search results verify
    recency ("9 hours ago" / "July 16"). Undated AND unflagged is NOT confirmed —
    but that is not the same as proven stale (see _ensure_fresh_lead)."""
    if story.get("freshness_confirmed"):
        return True
    pub = story.get("published_at")
    if not pub:
        return False
    try:
        from dateutil import parser as _dp
        return _dp.parse(pub).date() >= earliest_date
    except Exception:  # noqa: BLE001 — unparseable date → not confirmed
        return False


def _ensure_fresh_lead(top_stories, earliest_date, today_date):
    """The Big Signal (lead) must be a CONFIRMED-fresh story (dated in-window, or
    freshness-confirmed via search). Demote leading candidates that aren't — but
    distinguish HOW:
      • undated-but-not-provably-stale → moved to the BENCH (never discard on a
        guess; the Top-3 floor can seat it as a lower story, so the issue keeps its
        3-story body instead of collapsing to 1 — the download #008/#011/#012 and
        ai-pms #2 failure);
      • dated-but-out-of-window (PROVEN stale) → dropped outright.
    Lower Top Stories keep the softer flag-not-drop rule. Returns
    (top_stories, dropped_messages, benched_candidates)."""
    top = list(top_stories or [])
    dropped: list[str] = []
    benched: list[dict] = []
    while top and not _confirmed_fresh(top[0], earliest_date):
        s = top.pop(0)
        pub = s.get("published_at")
        if not pub:
            benched.append(s)   # not provably stale — keep as a lower-tier reserve
            dropped.append(
                f"Demoted lead candidate '{s.get('headline', '')[:60]}' to the bench "
                f"— the Big Signal needs a confirmed date; it can still seat as a "
                f"lower story (floor refill)")
        else:
            dropped.append(
                f"DROPPED lead candidate '{s.get('headline', '')[:60]}' — stale "
                f"(published {str(pub)[:10]}, before {earliest_date.isoformat()} cutoff)")
    return top, dropped, benched


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


def _norm_headline(h) -> str:
    """Coarse headline identity: case and stray whitespace are not editorial
    differences. Secondary to the URL — a syndicated story can reach us under
    two URLs with one headline."""
    return " ".join(str(h or "").split()).lower()


def _dedup_buckets(top_stories, other_news, bench):
    """Drop any item that repeats one already seated earlier in the issue.

    This compared the bench against the top tier and nothing else, which left
    `other_news` deduped against nothing. An audit of the sent archive found the
    same article shipped twice in one email — identical headline, identical URL —
    in 4 of 17 issues: ledger-006 and ledger-007 (Top Story 03 running again as
    Other News), download-014, nursing-001. ledger-007 shipped "GENIUS Act
    stablecoin rules still unfinished at 1 year" as Top Story 03 and again as
    Other News 02 off the same coindesk.com URL. Nothing semantic was going on;
    it was set membership nobody checked.

    So: one pass over the three buckets in priority order, first occurrence
    wins, and identity is the NORMALISED HEADLINE.

    URL identity was the obvious choice and it is wrong here. It was written,
    then measured against all 26 stored payloads: it caught the 2 real
    duplicates and also dropped 13 legitimate items. Making the URL key sharper
    (keeping the query, ignoring bare-domain citations) did not help, because
    the false drops are not a normalisation bug. They are the product working as
    intended. `review/pending/nursing.json` cites
    `ohlone.edu/nursing/educational-plan` for two genuinely different facts —
    "Ohlone ADN graduates can bridge to CSU East Bay BSN" in the top tier and
    "Ohlone pre-nursing AS covers most ADN and CSU prereqs" in Other News. One
    reference page backing several items is normal for the institutional briefs.

    Both real duplicates repeat the headline as well as the URL: ledger-007 ran
    "GENIUS Act stablecoin rules still unfinished at 1 year" off the same
    coindesk URL in both the top tier and Other News, and ledger-006 did the
    same. So the headline alone catches every duplicate in the archive and drops
    nothing legitimate, which is why the URL check is gone rather than kept "for
    safety" — an unused second key here is not armor, it is 13 deletions.

    Headline identity also covers the syndicated case for free: one story
    reaching us under two hosts shares its headline.

    The known gap, stated rather than guarded against: the same article
    re-titled in each bucket would survive. Nothing in 26 payloads does that. If
    it ever ships, the fix is a headline-similarity threshold, not the URL.

    This is deliberately NOT semantic matching: two DIFFERENT articles on one
    topic are a legitimate issue and both must survive. That judgement stays the
    prompt's.

    Runs ahead of the Top-3 floor, so a top story dropped here gets refilled
    from the bench rather than leaving a hole.

    Returns (top_stories, other_news, bench, dropped_notes). The notes go into
    `anomalies`, which is what the review console surfaces."""
    seen_headlines: set[str] = set()
    kept: dict[str, list] = {}
    notes: list[str] = []
    for label, key, items in (("Top Stories", "top", top_stories),
                              ("Other News", "other", other_news),
                              ("Bench", "bench", bench)):
        out = []
        for item in (items or []):
            headline = _norm_headline(item.get("headline"))
            # An item with no headline has no identity, so it can never be a
            # duplicate of anything. Two untitled items must not cancel out.
            if headline and headline in seen_headlines:
                notes.append(
                    f"{label}: dropped '{str(item.get('headline', ''))[:50]}' — "
                    f"same headline as an item already in this issue.")
                continue
            if headline:
                seen_headlines.add(headline)
            out.append(item)
        kept[key] = out

    # The bench keeps its ORIGINAL url-vs-top check, on top of the headline pass.
    #
    # This predates cross-bucket dedup and is deliberately preserved. It has the
    # same false-positive shape described above (3 bench items in the archive
    # share a top story's URL under a different headline), but the cost is not
    # the same: the bench is a RESERVE, so a wrongly dropped reserve only means
    # one fewer candidate for the Top-3 floor to promote. Nothing leaves the
    # issue. Dropping from Top Stories or Other News deletes published content,
    # which is why those two get the precise check and the bench keeps the
    # cautious one.
    top_urls = {_norm_url(s.get("source_url")) for s in kept["top"]
                if _norm_url(s.get("source_url"))}
    bench_out = []
    for b in kept["bench"]:
        if _norm_url(b.get("source_url")) in top_urls:
            notes.append(f"Bench: dropped '{str(b.get('headline', ''))[:50]}' — "
                         f"same source as a Top Story.")
        else:
            bench_out.append(b)
    kept["bench"] = bench_out

    return kept["top"], kept["other"], kept["bench"], notes


# The declared top-tier floor. The post-№ 007 hard stop only refuses 0 stories,
# so this constant is what the BLOCKING check at the call site compares against —
# a guard fitted to the invariant the system documents, not to the one value an
# incident happened to produce.
TOP_STORY_FLOOR = 3


# A bench reserve the WRITER could not verify must never be promoted. On
# 2026-07-29 the agent fetched a funding story, got HTTP 403,
# demoted it to the bench and wrote the caveat into its own source_excerpt —
# and the floor promoted it into slot 03 anyway, because the floor only counted
# stories. The reader rejected the issue. The brief already forbids this in
# capitals ("NEVER promote a bench reserve into the Top tier just to reach a
# count — a bench story must clear the SAME three filters"); the code did not
# agree with the brief, and the code runs last.
#
# The signal is the writer's own words. When it cannot verify a source it says
# so in the excerpt rather than inventing one, so an excerpt that admits it is
# unverified is a self-declared "do not promote".
_UNVERIFIED_EXCERPT = re.compile(
    r"unverified|could ?n.?t verify|could not verify|not verified|"
    r"unable to verify|could not fetch|could ?n.?t fetch|fetch ?error|"
    r"http\s*4\d\d|http\s*5\d\d|returned\s*http|paywall|"
    r"facts? (?:could not|couldn.?t) be (?:verified|confirmed)",
    re.I,
)

# A Top Story's claims are quote-anchored to source_excerpt; the Sr. Editor
# verifies against it. An excerpt too short to hold 2-4 verbatim sentences
# cannot anchor anything, so it is not promotable material either.
_MIN_PROMOTABLE_EXCERPT = 80


def _promotion_blocker(story):
    """Why this bench reserve may not be promoted, or None if it may.

    Kept as a reason string rather than a bool so the shortfall anomaly can
    name the story AND the cause — a silent skip would read as "thin week"
    when the real cause is "we found it and could not stand it up".

    `bench_only` is the writer's own refusal and outranks the quality checks.
    _drop_repeats catches a repeat by URL, but the same subject under a second
    URL is an editorial judgement only the writer can make — and on 2026-08-02
    it made it, wrote "SFSU ADN-BSN already covered in prior issues — moved to
    bench only" into the anomalies, and the floor promoted that story into the
    LEAD slot anyway, because prose is not a signal this function can read.
    Now it is one."""
    bench_only = (story or {}).get("bench_only")
    if isinstance(bench_only, str) and bench_only.strip():
        return f"writer marked bench-only: {bench_only.strip()[:80]}"
    if bench_only is True:            # tolerate a bare bool from an older payload
        return "writer marked bench-only"

    excerpt = (story or {}).get("source_excerpt") or ""
    if not excerpt.strip():
        return "no source_excerpt"
    if _UNVERIFIED_EXCERPT.search(excerpt):
        return "source_excerpt declares the source unverified"
    if len(excerpt.strip()) < _MIN_PROMOTABLE_EXCERPT:
        return f"source_excerpt too short to anchor claims ({len(excerpt.strip())} chars)"
    return None


def _enforce_top3_floor(top_stories, bench):
    """Guarantee 3 top stories when the bench allows AND the reserve is fit to
    promote. Promote highest-ranked bench reserves into the top tier until there
    are 3, SKIPPING any reserve that fails _promotion_blocker (unverified or
    unanchored source); if the gap can't be filled, emit a LOUD anomaly so the
    shortfall surfaces in review — never pad the top tier with a thin other_news
    item, and never with a story the writer could not stand up.
    Deterministic + unit-testable.
    Returns (top_stories, remaining_bench, anomalies). See ticket §4A.

    The floor is a floor, not a quota. Three verified stories is the goal; two
    verified stories beats three where one is unverified, because the whole
    brief trades on every claim being quote-anchored.

    Stays PURE and prose-only on purpose: review_server.build_approved_payload
    reuses it for bench promotion and discards the notes. The blocking finding is
    raised by the caller in main(), where the shortfall is policy rather than
    content logic."""
    top = list(top_stories or [])
    reserve = list(bench or [])
    orig = len(top)
    anomalies: list[str] = []
    skipped: list[str] = []
    held: list = []
    while len(top) < TOP_STORY_FLOOR and reserve:
        cand = reserve.pop(0)
        blocker = _promotion_blocker(cand)
        if blocker:
            # Not promotable, but still a real bench story — keep it on the
            # bench rather than dropping it, so the reviewer can see it and
            # decide. Only its promotion is refused, not its existence.
            held.append(cand)
            headline = (cand or {}).get("headline") or "(untitled)"
            skipped.append(f"'{headline[:60]}' ({blocker})")
            continue
        top.append(cand)
    reserve = held + reserve
    promoted = len(top) - orig
    if promoted:
        anomalies.append(
            f"Top-3 floor: promoted {promoted} bench "
            f"stor{'y' if promoted == 1 else 'ies'} to fill the top tier (had {orig}).")
    if skipped:
        anomalies.append(
            f"Top-3 floor: refused to promote {len(skipped)} bench "
            f"stor{'y' if len(skipped) == 1 else 'ies'} on quality — "
            + "; ".join(skipped)
            + ". A short issue is correct here; an unverified Top Story is not.")
    if len(top) < TOP_STORY_FLOOR:
        why = ("the bench had no reserves to promote" if not skipped
               else "the only reserves available failed the promotion check")
        anomalies.append(
            f"Top-3 floor SHORT: only {len(top)} top "
            f"stor{'y' if len(top) == 1 else 'ies'} — expected {TOP_STORY_FLOOR}, and "
            f"{why}. Review before sending.")
    return top, reserve, anomalies


def _norm_url(u: str) -> str:
    """Coarse URL identity for repeat detection: scheme/host case, trailing
    slash and tracking query are not editorial differences."""
    from urllib.parse import urlsplit
    try:
        p = urlsplit((u or "").strip())
    except ValueError:
        return (u or "").strip().lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/").lower()
    return f"{host}{path}" if host else path


def _drop_repeats(newsletter: str, top_stories: list, bench: list):
    """Hard guard against re-running a story the newsletter already sent.

    `sent_ledger.covered_block` already tells the agent "don't repeat these",
    but that is a prompt hint — a hint the model can miss, and a repeat that
    reaches the inbox is the kind of error a reader notices immediately. This
    enforces it deterministically after generation: repeats are dropped from
    BOTH the top tier and the bench (so the Top-3 floor can't promote another
    one), and each drop raises a decision-level anomaly.

    Returns (top, bench, anomalies). Never raises — a missing archive just
    means nothing has shipped yet."""
    try:
        from scripts.sent_ledger import recently_covered
        covered = {_norm_url(c["url"]): c.get("headline", "") for c in recently_covered(newsletter)}
    except Exception as e:  # noqa: BLE001
        print(f"[{newsletter}] repeat guard skipped ({e})", file=sys.stderr)
        return top_stories, bench, []
    if not covered:
        return top_stories, bench, []

    anomalies: list = []

    def _keep(items: list, where: str) -> list:
        out = []
        for s in items:
            key = _norm_url(s.get("source_url") or "")
            if key and key in covered:
                anomalies.append({
                    "severity": "decide",
                    "message": (f"repeat dropped from {where}: "
                                f"\"{(s.get('headline') or '').strip()[:80]}\" — this URL already "
                                f"shipped in a recent issue. Review before sending."),
                })
                continue
            out.append(s)
        return out

    return _keep(top_stories, "the top tier"), _keep(bench, "the bench"), anomalies


def _brand_field_lead(newsletter: str, lead: dict) -> bool:
    """Give an audience with no configured hero its OWN branded field.

    Without this every such audience falls back to the single shared
    assets/hero/default.jpg — the same generic photo on every audience that has
    no hero of its own. A field in the audience's own accent always belongs,
    costs nothing, and can never be the wrong picture (Track B, all-newsletters
    scope locked by David 2026-07-22).

    Skipped for Card News audiences — those bake the whole Story-01 card in
    _compose_lead_hero, and that path stays byte-identical. Also skipped when
    the audience configured a real `fallback_hero_url`: an explicit choice wins.

    The file is named per AUDIENCE, not per issue: the field is a pure function
    of the accent, so a re-run rewrites identical bytes and git sees no diff.

    Returns True when it set the lead's hero. Fail-open — any error returns
    False so the caller still applies the configured/shared fallback."""
    try:
        if _is_card_news(newsletter):
            return False
        from scripts import audience_config
        entry = audience_config.load_palettes().get(newsletter) or {}
        configured = entry.get("fallback_hero_url")
        if isinstance(configured, str) and configured.strip():
            return False
        from scripts.compose_hero import FIELD_H, FIELD_W, compose_brand_field
        accent = (entry.get("chassis") or {}).get("accent", "#0EA5A5")
        fname = f"{newsletter}-field.jpg"
        compose_brand_field(
            accent, out_path=REPO_ROOT / "assets" / "hero" / "composed" / fname)
        url = audience_config._resolve_asset(f"hero/composed/{fname}")
        if not url.lower().startswith(("http://", "https://")):
            return False  # no ASSET_BASE_URL yet — let the normal path warn
        lead["hero_image_url"] = url
        lead["hero_image_w"], lead["hero_image_h"] = FIELD_W, FIELD_H
        lead.setdefault("hero_provenance", {}).update({
            "source": "branded-field", "grade": "hero",
            "reason": "no photo survived sourcing; branded field in the audience accent"})
        print(f"[{newsletter}] lead hero backfilled with branded field", file=sys.stderr)
        return True
    except Exception:
        return False


def _backfill_lead_hero(newsletter: str, payload: dict) -> None:
    """Guarantee the lead (Big Signal / Story 01) carries a hero image.

    The agent scrapes og:images from each story's source; platform audiences
    (nursing, health) source from sites that expose no usable og:image, so the
    lead can render imageless. David's locked rule: every issue must lead with a
    visual. When Story 01 has no valid hero after generation, fall back to the
    audience's configured `fallback_hero_url` (config/audiences/<pack>/palettes.json).

    Big-Signal-only (Stories 02+ stay text-only per the locked hero rule),
    idempotent (never overwrites a real og:image the agent found), and fail-open
    (any error is swallowed — a missing hero must never break a generate run)."""
    try:
        top = payload.get("top_stories") or []
        if not top or not isinstance(top[0], dict):
            return
        lead = top[0]
        cur = lead.get("hero_image_url")
        if isinstance(cur, str) and cur.strip().lower().startswith(("http://", "https://")):
            return  # agent already found a real hero — leave it
        from scripts import audience_config
        if _brand_field_lead(newsletter, lead):
            return
        fallback = audience_config.load_fallback_hero(newsletter)
        if fallback:
            lead["hero_image_url"] = fallback
            if fallback.lower().startswith(("http://", "https://")):
                print(f"[{newsletter}] lead hero backfilled from audience fallback", file=sys.stderr)
            else:
                # Relative ref = no ASSET_BASE_URL/REVIEW_URL at generate time; render's
                # _safe_url will drop it and the lead would render imageless. Fail loud.
                print(f"[{newsletter}] WARNING: fallback hero URL is not absolute ({fallback!r}); "
                      f"set ASSET_BASE_URL/REVIEW_URL so the lead renders a visual.", file=sys.stderr)
    except Exception:
        return  # fail-open: never block a generate run on a missing hero


def _is_card_news(newsletter: str) -> bool:
    """True when the audience opted into the Card News layout (branding entry
    `card_news: true` in config/audiences/<pack>/palettes.json)."""
    try:
        from scripts import audience_config
        return bool((audience_config.load_palettes().get(newsletter) or {}).get("card_news"))
    except Exception:
        return False


def _load_hero_manifest(newsletter: str) -> list:
    """Curated hero library for an audience: assets/hero/<newsletter>/manifest.json.
    Shape: {"images": [{file, topics[], grade, license, source_url, added, width, height, reason}]}.
    Returns [] when absent (fail-open — the lead still gets the generic fallback)."""
    try:
        mp = REPO_ROOT / "assets" / "hero" / newsletter / "manifest.json"
        if not mp.exists():
            return []
        data = json.loads(mp.read_text())
        return data.get("images", data) if isinstance(data, (dict, list)) else []
    except Exception:
        return []


def _newsletter_geo(newsletter: str) -> str:
    """The audience's geography string (e.g. 'San Francisco Bay Area, California')
    from its brief spec, or '' when unknown. Feeds the hero fit-check's place
    guard so a geographically specific newsletter never carries a foreign photo."""
    try:
        p = REPO_ROOT / "briefs" / f"{newsletter}.json"
        if p.exists():
            return str((json.loads(p.read_text()) or {}).get("geography", "") or "")
    except Exception:
        pass
    return ""


def _select_hero(manifest: list, track: str, *, want_hero_grade: bool, used: set, seed: int):
    """Pick a library image for a story slot. Deterministic rotation by `seed`
    (issue_number-derived) so consecutive issues pick different heroes without
    any persisted counter; `used` guarantees distinct images within one issue.

    Lead (want_hero_grade): topic + hero-grade first, else any hero-grade.
    Stories 02+: topic + (hero|card) grade only — no topic match → no image
    (the story renders text-only), per the ticket waterfall."""
    track_l = (track or "").lower()

    def topic_match(e) -> bool:
        for tp in (e.get("topics") or []):
            tp_l = str(tp).lower()
            if tp_l and (tp_l in track_l or track_l in tp_l):
                return True
        return False

    avail = [e for e in manifest if isinstance(e, dict) and e.get("file") and e["file"] not in used]
    if want_hero_grade:
        tiers = [
            [e for e in avail if e.get("grade") == "hero" and topic_match(e)],
            [e for e in avail if e.get("grade") == "hero"],
        ]
    else:
        tiers = [
            [e for e in avail if e.get("grade") in ("hero", "card") and topic_match(e)],
        ]
    for tier in tiers:
        if tier:
            return tier[seed % len(tier)]
    return None


def _cardnews_audience_desc(newsletter: str) -> str:
    """Short audience descriptor for grade_image's audience_ok judgment
    (e.g. 'The Pre-Nursing Brief — Your weekly guide to prereqs, the TEAS ...')."""
    try:
        from scripts import audience_config
        pal = audience_config.load_palettes().get(newsletter) or {}
        desc = f'{pal.get("name", newsletter)} — {pal.get("subtitle", "")}'.strip(" —")
        return desc or newsletter
    except Exception:
        return newsletter


def _try_publisher_image(newsletter: str, story: dict, is_lead: bool) -> dict:
    """Tier 1 of the waterfall: the article's OWN og:image, vetted + graded.

    Returns a dict with a "status" the caller acts on:
      - {"status": "use", url, grade, reason, w, h} — a hero/card-grade image to apply.
      - {"status": "reject", reason}  — the candidate was graded and REJECTED
            (irrelevant / off-audience / too small); the caller drops any agent hero
            and falls back to the curated library.
      - {"status": "skip", reason}    — could NOT grade (no API key, no candidate URL,
            fetch/vision error). The caller must PRESERVE a valid agent-found hero
            rather than discard an unvetted-but-real image (old contract).

    Grades the agent's already-found hero when present, else re-fetches the source
    URL for its og:image (the bounded-GET fix now surfaces CDN images the agent's
    check used to silently skip). Fail-open: any error is a skip, never a crash."""
    try:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"status": "skip", "reason": "no API key for grading"}
        from scripts.tools import og_image, web_fetch

        cur = story.get("hero_image_url")
        cand_url = None
        cand_size = None
        if isinstance(cur, str) and cur.strip().lower().startswith(("http://", "https://")):
            cand_url = cur.strip()  # agent already found one — grade it (no re-fetch)
        else:
            src = story.get("source_url")
            if not (isinstance(src, str) and src.strip().lower().startswith(("http://", "https://"))):
                return {"status": "skip", "reason": "no source url"}
            r = web_fetch.fetch(src)
            cand_url = r.get("og_image_url")
            cand_size = r.get("og_image_size_bytes")
        if not cand_url:
            return {"status": "skip", "reason": "no publisher og:image"}

        v = og_image.grade_image(
            cand_url, story.get("headline", ""),
            track=story.get("track", ""),
            audience=_cardnews_audience_desc(newsletter),
            image_size_bytes=cand_size)
        if v.get("grade") in ("hero", "card"):
            return {"status": "use", "url": cand_url, "grade": v["grade"],
                    "reason": v.get("reason", ""), "w": v.get("width"), "h": v.get("height")}
        return {"status": "reject", "reason": v.get("reason", "rejected")}
    except Exception:
        return {"status": "skip", "reason": "image vetting error"}


def _apply_image(story: dict, url: str, source: str, grade, reason: str,
                 w=None, h=None, file=None) -> None:
    """Set a story's hero + provenance. Records pixel dims when known so the
    renderer can pin width/height (no reflow)."""
    story["hero_image_url"] = url
    if w and h:
        story["hero_image_w"] = w
        story["hero_image_h"] = h
    prov = {"source": source, "grade": grade, "reason": reason}
    if file:
        prov["file"] = file
    story["hero_provenance"] = prov


def _source_cardnews_library_images(newsletter: str, payload: dict, issue_number: int) -> None:
    """Card-News image waterfall (per story, deterministic, fail-open):
      Tier 1 — the article's own og:image, vetted + graded (og_image.grade_image);
      Tier 2 — the curated CC0 library, track-matched + issue-rotated;
      Tier 3 — none (lead → the audience default via _backfill_lead_hero; 02+ →
               text-only). Every miss/rejection is noted in payload["anomalies"] so
      David sees WHY in review. Provenance {source, grade, reason} recorded per story."""
    try:
        manifest = _load_hero_manifest(newsletter)
        from scripts import audience_config
        from scripts import hero_fit
        top = payload.get("top_stories") or []
        used: set = set()
        anomalies = payload.setdefault("anomalies", [])
        # Move 1 (image fit-check) — PLACE guard, up front: a geographically
        # specific newsletter must never carry a photo from a different country.
        # This is what put an Indonesian classroom on a California story. Drop
        # those library images before selection (deterministic, no API call).
        geo = _newsletter_geo(newsletter)
        audience_desc = _cardnews_audience_desc(newsletter)
        if manifest and geo:
            kept = []
            for e in manifest:
                ok, why = hero_fit.place_ok(e.get("place"), geo)
                if ok:
                    kept.append(e)
                else:
                    anomalies.append(f"library image '{e.get('file')}' excluded: {why}")
            manifest = kept
        for idx, story in enumerate(top):
            if not isinstance(story, dict):
                continue
            # Idempotent: a story already sourced (has provenance) is left untouched —
            # so a --from-payload replay of a sourced payload re-renders at $0 (no
            # re-fetch, no re-grade); only raw/un-sourced payloads get sourced.
            if story.get("hero_provenance"):
                continue
            is_lead = (idx == 0)

            # Tier 1 — publisher og:image (vetted + graded).
            pub = _try_publisher_image(newsletter, story, is_lead)
            status = pub.get("status")
            if status == "use":
                _apply_image(story, pub["url"], "publisher", pub["grade"],
                             pub.get("reason", ""), pub.get("w"), pub.get("h"))
                continue

            cur = story.get("hero_image_url")
            has_agent_hero = isinstance(cur, str) and cur.strip().lower().startswith(("http://", "https://"))
            if status == "reject":
                # Graded and rejected — drop it (incl. stale dims/provenance) so it
                # can't linger, and record WHY before falling to the library.
                for k in ("hero_image_url", "hero_image_w", "hero_image_h", "hero_provenance"):
                    story.pop(k, None)
                anomalies.append(
                    f"story {idx+1:02d}: publisher og:image rejected "
                    f"({pub.get('reason','')}) — falling back")
            elif has_agent_hero:
                # Couldn't grade (skip), but the agent found a REAL og:image — keep it
                # rather than discard an unvetted-but-real image (the old contract).
                story.setdefault("hero_provenance", {
                    "source": "publisher", "grade": "unknown",
                    "reason": f"agent og:image (ungraded: {pub.get('reason','')})"})
                continue
            # else: skip with no agent hero → fall through to the library.

            # Tier 2 — curated library.
            pick = _select_hero(manifest, story.get("track", ""),
                                want_hero_grade=is_lead, used=used,
                                seed=issue_number + idx) if manifest else None
            if pick:
                resolved = audience_config._resolve_asset(f"hero/{newsletter}/{pick['file']}")
                # Move 1 (image fit-check) — SUBJECT/AUDIENCE: the pick already
                # cleared the place guard; now run the same vision grader
                # publisher images go through, against THIS story. No pass ->
                # text-only ("a wrong photo is worse than no photo").
                fit_ok, fit_why = hero_fit.hero_fit(
                    story, pick, newsletter_geo=geo, audience_desc=audience_desc,
                    resolved_url=resolved)
                if fit_ok:
                    used.add(pick["file"])
                    _apply_image(
                        story, resolved,
                        "library", pick.get("grade"), pick.get("reason", "track match"),
                        pick.get("width"), pick.get("height"), file=pick["file"])
                    continue
                anomalies.append(
                    f"story {idx+1:02d}: library image '{pick['file']}' failed "
                    f"fit-check ({fit_why}) — text-only")

            # Tier 3 — nothing suitable.
            if is_lead:
                anomalies.append(
                    f"story 01: no usable publisher og:image and no hero-grade library "
                    f"match for track '{story.get('track','')}' — using audience default")
            else:
                anomalies.append(
                    f"story {idx+1:02d}: no usable publisher og:image and no library "
                    f"match for track '{story.get('track','')}' — rendered text-only")
    except Exception:
        return  # fail-open: image sourcing must never break a generate run


def _source_story_images(newsletter: str, payload: dict, issue_number: int) -> None:
    """Unified image sourcing at the render seam. Card-News audiences enrich all
    top stories from the curated library first; then the universal lead-hero
    guarantee runs for everyone (idempotent — it only fills a still-empty lead)."""
    if _is_card_news(newsletter):
        _source_cardnews_library_images(newsletter, payload, issue_number)
    _backfill_lead_hero(newsletter, payload)


def _compose_lead_hero(newsletter: str, payload: dict, issue_number: int) -> None:
    """Card News 'A' treatment (Phase 3): bake the Story-01 overlay onto a
    HERO-grade lead image per design-spec-v7 and stamp the payload with
    `composed_hero_url` + `composed_hash`.

    - Fires only for card-news audiences whose lead image graded "hero"
      (card-grade leads stay on the live-text B split card).
    - Writes assets/hero/composed/<nl>-<issue>.jpg — the nightly workflow commits
      it, the Railway assets route serves it (no new hosts).
    - The hash is computed over the SAME length-capped view the renderer sees;
      the renderer uses the baked card only while the hash matches, so an HITL
      console edit to the lead's text falls back to B (no stale baked text).
    - Idempotent (skips if already composed for this text) and fail-open: any
      failure just leaves the B split card in place."""
    try:
        if not _is_card_news(newsletter):
            return
        top = payload.get("top_stories") or []
        if not top or not isinstance(top[0], dict):
            return
        lead = top[0]
        prov = lead.get("hero_provenance") or {}
        url = lead.get("hero_image_url")
        has_url = isinstance(url, str) and url.strip().lower().startswith(("http://", "https://"))
        has_photo = prov.get("grade") == "hero" and has_url
        # A CARD-grade lead keeps its old behaviour: no compose, and the live-text
        # B split card renders the photo. Branding over a usable photo would throw
        # away a real image — the branded field is for having NO image at all.
        if has_url and not has_photo:
            return

        from scripts.render_html import _enforce_top_stories_caps, composed_inputs_hash
        capped = _enforce_top_stories_caps([dict(lead)])[0]
        want_hash = composed_inputs_hash(capped)
        if lead.get("composed_hash") == want_hash and lead.get("composed_hero_url"):
            return  # already composed for this exact text (replay)

        # Source image bytes: read the library file straight from the checkout
        # when we have it; otherwise a bounded fetch of the (already graded) URL.
        img_bytes = None
        if has_photo:
            if prov.get("source") == "library" and prov.get("file"):
                fp = REPO_ROOT / "assets" / "hero" / newsletter / str(prov["file"])
                if fp.exists():
                    img_bytes = fp.read_bytes()
            if img_bytes is None:
                from scripts.tools.og_image import _download_image_capped
                got = _download_image_capped(url.strip())
                if got:
                    img_bytes = got[0]
        # No photo survived sourcing/fit-check → brand it rather than ship a bare
        # text lead. David's locked rule: Story 01 always carries a visual, and a
        # branded field can never be the *wrong* picture. See Track B in
        # Handoff/newsletter-image-strategy.
        branded = img_bytes is None

        from urllib.parse import urlparse
        from scripts import audience_config
        from scripts.compose_hero import compose_branded, compose_hero

        entry = audience_config.load_palettes().get(newsletter) or {}
        chassis = entry.get("chassis") or {}
        # Bake the Korean box only for Korean-enabled audiences (mirrors the
        # renderer's wants_korean gate). The hash was taken BEFORE this pop, from
        # the same fields the renderer hashes, so edit-detection is unaffected.
        bake_view = dict(capped)
        if not entry.get("wants_korean"):
            bake_view.pop("korean_takeaway", None)
        src = lead.get("source_url") or ""
        label = lead.get("source_name") or lead.get("publisher") or ""
        if not label and src:
            net = urlparse(src).netloc.lower()
            label = net[4:] if net.startswith("www.") else net

        fname = f"{newsletter}-{issue_number:03d}.jpg"
        out_path = REPO_ROOT / "assets" / "hero" / "composed" / fname
        accent = chassis.get("accent", "#0EA5A5")
        accent_pale = chassis.get("tint", "#D6F5F5")
        if branded:
            compose_branded(bake_view, accent=accent, accent_pale=accent_pale,
                            source_label=label, out_path=out_path)
        else:
            compose_hero(img_bytes, bake_view, accent=accent, accent_pale=accent_pale,
                         source_label=label, out_path=out_path)
        lead["composed_hero_url"] = audience_config._resolve_asset(f"hero/composed/{fname}")
        lead["composed_hash"] = want_hash
        lead.setdefault("hero_provenance", {})["composed"] = "branded" if branded else "photo"
        print(f"[{newsletter}] composed '{'branded' if branded else 'A'}' hero "
              f"→ assets/hero/composed/{fname}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[{newsletter}] hero compose failed (falling back to B card): {e}", file=sys.stderr)
        return  # fail-open: the B split card is always a valid outcome


def _assert_not_unreviewed_send(args) -> int:
    """Refuse the one invocation that mails fresh, unreviewed content to a real audience.

    This script GENERATES an issue. It does not own the wire to a configured
    audience — `nightly_send.py` does, and only after `approved_artifact.verify()`
    clears the frozen bytes a human actually read.

    But a bare `run_newsletter.py --newsletter <nl>` (no --dry-run, no
    --create-draft-only, no --to) runs the agent loop and then resolves the real
    recipient list from config/audiences/<pack>/recipients.json and sends it.
    Nothing in that path consults an approval. It is the same shape as the two
    incidents this repo already carries scar tissue for — № 007 shipping empty
    on 2026-07-21, and the unauthorized 2026-07-23 send — except it skips review
    entirely rather than racing it.

    No caller uses this path: nightly-generate.yml passes --create-draft-only,
    preview-send.yml passes --to, demo_server.py passes --dry-run, and
    nightly-send.yml calls nightly_send.py. Verified 2026-08-01 before closing
    it, so this breaks nothing that exists — it only removes the foot-gun.

    Deliberately no env-var escape hatch. An override here would reconstitute
    exactly the hole being closed, and the legitimate need it would serve
    (send these bytes to these people) is what the approve → release → send
    path is for.
    """
    if args.dry_run or args.create_draft_only or args.to:
        return 0
    nl = args.newsletter
    print(
        f"[{nl}] REFUSED — this would mail freshly-generated, unreviewed content to the\n"
        f"  configured audience for '{nl}'. run_newsletter.py generates; it does not send\n"
        f"  to a real list.\n\n"
        f"  To send the issue a human approved:\n"
        f"      python scripts/nightly_send.py {nl}\n"
        f"  To generate a draft for review:   --create-draft-only --save-payload ...\n"
        f"  To render without sending:        --dry-run\n"
        f"  To preview to one address:        --to you@example.com\n",
        file=sys.stderr,
    )
    return 5


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
    parser.add_argument("--skip-live-verify", action="store_true", help="Skip live source re-fetch excerpt verification (network op; kill switch).")
    parser.add_argument("--demo", action="store_true", help="Public/GitHub render: omit the 'reply to this email' footer (only shown when actually sending).")
    parser.add_argument("--save-payload", help="Write the structured story payload (with meta) to this JSON path — enables $0 re-rendering later via --from-payload.")
    parser.add_argument(
        "--from-payload",
        help=(
            "Replay mode: skip the agent loop and load the story payload from a "
            "JSON file. Use a test fixture (tests/fixtures/*_input.json) or any "
            "previously-saved agent output. Combine with --skip-editor + --dry-run "
            "+ --save-html for a fully-local zero-API iteration loop on render + "
            "send-path changes. (Card News: an already-sourced payload re-renders "
            "at $0 — image sourcing is idempotent; a raw, un-sourced payload with "
            "ANTHROPIC_API_KEY set will re-vet publisher images, so unset the key to "
            "keep replays offline.)"
        ),
    )
    args = parser.parse_args()

    rc = _assert_not_unreviewed_send(args)
    if rc:
        return rc

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
    from scripts.compact_prompts import get_freshness_window
    _y, _m, _d = (int(x) for x in today_date_iso.split("-"))
    _win_min, _win_max = get_freshness_window(newsletter)
    _earliest = freshness_floor(today_date_iso, state.get("last_run_at"), _win_min, _win_max)
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
            # BLOCKING. Other News and the bench DROP a stale item outright;
            # the top tier only ever flagged one and shipped it, so slots 02 and
            # 03 were the freshness gap. Blocking rather than dropping keeps the
            # existing "the model may have judgement we lack" intent: the
            # reviewer can still ship it, but must say so.
            stale_findings.append(guard_findings.block("stale_story", msg))
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
        for _msg in dropped_other:
            print(f"[{newsletter}] {_msg}", file=sys.stderr)

    # Drop any item that repeats one already seated earlier in the issue, across
    # all three buckets, BEFORE the floor could promote a repeat off the bench.
    # Semantic near-dups are the prompt's job; this only catches the same article.
    top_deduped, other_deduped, bench_deduped, dedup_notes = _dedup_buckets(
        payload.get("top_stories") or [], payload.get("other_news") or [],
        payload.get("bench") or [])
    payload["top_stories"] = top_deduped
    payload["other_news"] = other_deduped
    payload["bench"] = bench_deduped
    if dedup_notes:
        payload.setdefault("anomalies", []).extend(dedup_notes)
        for _n in dedup_notes:
            print(f"[{newsletter}] {_n}", file=sys.stderr)

    # Freshness gate on the bench too — a stale reserve must never be promoted
    # into the Top tier by the floor (nor swapped in as a review backfill). Same
    # cutoff as Top Stories / Other News; runs BEFORE the floor so the floor can
    # only pad with FRESH reserves (a thin-but-fresh issue beats a padded stale one).
    bench_fresh, stale_bench_notes = _drop_stale_bench(
        payload.get("bench") or [], _earliest, _date(_y, _m, _d))
    payload["bench"] = bench_fresh
    if stale_bench_notes:
        payload.setdefault("anomalies", []).extend(stale_bench_notes)
        for _n in stale_bench_notes:
            print(f"[{newsletter}] {_n}", file=sys.stderr)

    # Lead must be confirmed fresh. The Big Signal is the highest-stakes slot, so
    # an undated or out-of-window lead (a stale story sneaking in via a source with
    # no date) is dropped from the top tier — the floor below then refills from
    # FRESH reserves. Lower Top Stories keep the softer flag-not-drop rule.
    top_fresh_lead, lead_notes, lead_benched = _ensure_fresh_lead(
        payload.get("top_stories") or [], _earliest, _date(_y, _m, _d))
    payload["top_stories"] = top_fresh_lead
    if lead_benched:
        # Demoted-but-not-stale lead candidates rejoin the bench so the floor can
        # seat them as lower stories — appended AFTER existing reserves, so a
        # confirmed-fresh reserve (if any) still leads when the floor refills an
        # empty tier. This is the fix for the "collapses to 1 story" bug.
        payload["bench"] = (payload.get("bench") or []) + lead_benched
    if lead_notes:
        payload.setdefault("anomalies", []).extend(lead_notes)
        for _n in lead_notes:
            print(f"[{newsletter}] {_n}", file=sys.stderr)

    # Repeat guard — GENERATE ONLY. Never in replay/send mode.
    #
    # This shipped broken on 2026-07-21 and emptied an already-approved issue in
    # front of real recipients. nightly_send MOVES the approved payload into review/sent/
    # *before* invoking this script with --from-payload, and recently_covered()
    # globs review/sent/<nl>-*.json — so the guard read the very issue being sent
    # and saw all three of its own stories as "already covered". It dropped all
    # three; the Top-3 floor then reported "only 0 top stories" with nothing left
    # to promote, and the send went out empty.
    #
    # Generate-time is also where this belongs on the merits: a drop must happen
    # BEFORE David reviews, never silently after he has approved an issue.
    if not args.from_payload:
        _top_dd, _bench_dd, repeat_notes = _drop_repeats(
            newsletter, payload.get("top_stories") or [], payload.get("bench") or [])
        payload["top_stories"], payload["bench"] = _top_dd, _bench_dd
        if repeat_notes:
            payload.setdefault("anomalies", []).extend(repeat_notes)
            for _n in repeat_notes:
                print(f"[{newsletter}] {_n['message']}", file=sys.stderr)

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
    # ...and a shortfall BLOCKS — but the blocking finding is raised at APPROVAL
    # time, in approved_artifact.build(), not here. Two reasons. The top tier can
    # still shrink after this point (the reviewer drops a story in the console),
    # so a check here would miss the most common way an issue goes short. And
    # raising it in both places would produce two findings with two different
    # messages for one problem, i.e. two checkboxes — the "tick eight boxes for
    # three facts" failure that teaches reflexive clearing.
    # The floor is therefore enforced once, against the FINAL approved content.

    # HARD STOP: an issue with no top stories is not an issue. On 2026-07-21 an
    # issue reached its readers as a masthead, three Other-News briefs and a
    # footer, because the (then-buggy) repeat guard emptied the top tier and
    # nothing downstream refused to send. The Top-3 floor logged "only 0 top
    # stories" and the run continued regardless — a loud log is not a gate.
    # This is the gate: never render or send an empty issue, whatever emptied it.
    if not (payload.get("top_stories") or []):
        print(f"[{newsletter}] ABORT: 0 top stories — refusing to render or send an "
              f"empty issue. Nothing was sent; re-approve after fixing the payload.",
              file=sys.stderr)
        return 5

    # If, after the floor, the lead still can't be date-confirmed (no fresh story
    # anywhere), surface it loudly — better a flagged issue than a silent stale lead.
    _lead_now = payload.get("top_stories") or []
    if _lead_now and not _confirmed_fresh(_lead_now[0], _earliest):
        _msg = ("Lead is not date-confirmed — no story with a confirmed publish "
                "date was available for the Big Signal. Review before sending.")
        payload.setdefault("anomalies", []).append(_msg)
        print(f"[{newsletter}] {_msg}", file=sys.stderr)

    # Image sourcing. Card-News audiences enrich every top story from the curated
    # CC0 library (track-matched, hero-grade preferred for the lead, rotated by
    # issue); the universal lead-hero guarantee then fills any still-empty Big
    # Signal from the audience default. Idempotent + fail-open (a missing image
    # must never break a generate run). David's rule: always lead with a visual.
    _source_story_images(newsletter, payload, issue_number)

    # Card News 'A' hero (Phase 3): bake the Story-01 overlay onto a hero-grade
    # lead image. Idempotent + fail-open; the renderer falls back to the live-text
    # B card whenever the baked text no longer matches the story (HITL edits).
    _compose_lead_hero(newsletter, payload, issue_number)

    # Deterministic numeric guard: every currency / percent / ratio in a story's
    # copy must appear in that story's own saved source excerpt. A figure that
    # isn't there is a likely fabrication — the "$21,640/yr gap" / "32 of 200"
    # failure the LLM editor let through. Findings are appended to anomalies and
    # force the editor verdict to FAIL (below) so the human reviewer can't miss it.
    from scripts.numeric_guard import check_payload as _numeric_check
    numeric_findings = _numeric_check(payload.get("top_stories") or [])
    if numeric_findings:
        # BLOCKING. Until now these forced editor verdict=FAIL, whose only
        # consumer is a banner in the rendered HTML — and approved_artifact
        # re-renders WITHOUT editor_concerns, so the FAIL text never reached the
        # bytes that ship. A fabricated figure was detected, announced, and
        # mailed if the reviewer did not read the banner.
        for _n in numeric_findings:
            payload.setdefault("anomalies", []).append(
                guard_findings.block("fabrication", _n))
            print(f"[{newsletter}] FABRICATION? {_n}", file=sys.stderr)

    # Live source verification: re-fetch each top story's source and confirm the
    # saved excerpt is actually IN it — catches an agent that fabricated BOTH a
    # claim and a matching excerpt (which the excerpt-based guard + editor trust).
    # Network op: graceful (a fetch failure is advisory, never a FAIL) and
    # skippable via --skip-live-verify. Only a clearly-absent excerpt hard-fails.
    live_findings: list[str] = []
    live_fail = False
    if not args.skip_live_verify:
        from scripts.live_verify import verify_payload as _live_verify
        from scripts.live_verify import verify_other_news as _live_other
        try:
            top_find, top_fail = _live_verify(payload.get("top_stories") or [])
            # Other News has no saved excerpt — verify its numbers against the live
            # source so a fabricated stat can't hide there (the guard's blind spot).
            other_find, other_fail = _live_other(payload.get("other_news") or [])
            live_findings = top_find + other_find
            live_fail = top_fail or other_fail
        except Exception as _e:  # noqa: BLE001 — verification must never abort a run
            print(f"[{newsletter}] live-verify skipped ({type(_e).__name__}: {_e})", file=sys.stderr)
        if live_findings:
            # Only a clearly-ABSENT excerpt blocks. Unverified/partial stay
            # advisory prose: a publisher that starts 403ing the crawler must not
            # halt every send, or the guard becomes the outage.
            for _n in live_findings:
                if "FABRICATED" in _n:
                    payload.setdefault("anomalies", []).append(
                        guard_findings.block("live_verify", _n))
                else:
                    payload.setdefault("anomalies", []).append(_n)
                print(f"[{newsletter}] LIVE-CHECK {_n}", file=sys.stderr)

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

    # The deterministic guards override the LLM verdict: an unsupported figure OR
    # an excerpt absent from its live source FAILS the draft even if the editor
    # (or --skip-editor) let it pass. Hard backstop behind the advisory editor.
    hard_findings = list(numeric_findings)
    if live_fail:
        hard_findings += [f for f in live_findings if "FABRICATED" in f]
    if hard_findings:
        editor_concerns = editor_concerns or {"verdict": "PASS", "must_fix": [], "notes": ""}
        editor_concerns["verdict"] = "FAIL"
        editor_concerns["must_fix"] = list(editor_concerns.get("must_fix", [])) + hard_findings
        print(f"[{newsletter}] Guards FAILED the draft: {len(hard_findings)} hard finding(s)", file=sys.stderr)

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

    # Per-run token + cost (generation + editor), before any send/dry-run branch
    # so every run reports it.
    from scripts import usage_meter
    print(f"[{newsletter}] Usage: {usage_meter.format_line()}")

    if args.dry_run:
        print(f"[{newsletter}] Dry run complete. Not sending.")
        print("\n--- PLAINTEXT ---")
        print(plaintext)
        return 0

    # 4. Recipients
    if args.to:
        to_list = args.to
        bcc_list: list[str] = []
        reply_to = None
        print(f"[{newsletter}] Recipient override: {to_list}")
    else:
        # Recipients come from the per-audience config pack (config/audiences/
        # <pack>/recipients.json), mirror-excluded. The Notion recipients DS is
        # DEPRECATED — kept only as a transition fallback when config is empty.
        from scripts import audience_config
        recipients = audience_config.load_recipients(newsletter)
        rec_source = "config"
        config_recipients = list(recipients)  # source-of-truth snapshot for the delivery guard
        if not recipients:
            print(f"[{newsletter}] No config recipients — falling back to Notion DS (DEPRECATED)...")
            from scripts.tools import notion_client  # lazy import
            recipients = notion_client.get_active_recipients(newsletter)
            rec_source = "notion"
        all_emails = [r["email"] for r in recipients if r.get("email")]
        # Send TO the operator (first list entry = David for every audience, so the
        # right inbox per audience), BCC everyone else so addresses stay private.
        # OPERATOR_EMAIL env overrides.
        operator_email = (
            os.environ.get("OPERATOR_EMAIL")
            or (all_emails[0] if all_emails else None)
            or audience_config.operator_email()
        )
        if operator_email:
            to_list = [operator_email]
            bcc_list = [e for e in all_emails if e.lower() != operator_email.lower()]
        else:
            to_list = all_emails
            bcc_list = []
        reply_to = operator_email
        print(f"[{newsletter}] Recipients ({rec_source}): To {to_list}; BCC {len(bcc_list)}")
        # Fail loudly: on the unattended cron path (no operator override), the
        # resolved envelope must cover exactly the configured list. A silent drop
        # to a partial list aborts the send + fires the failure alert.
        if config_recipients and not os.environ.get("OPERATOR_EMAIL"):
            assert_full_delivery(newsletter, config_recipients, to_list, bcc_list)

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
            reply_to=reply_to,
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
    # release job runs WITHOUT --create-draft-only).
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
