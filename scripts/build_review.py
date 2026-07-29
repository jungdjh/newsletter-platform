#!/usr/bin/env python3
"""Build a side-by-side review page for HITL review on a desktop.

Solves "Gmail can't show our draft and the source article side by side."

Left pane  = what we wrote (headline, summary, implications, the quoted
             source_excerpt — highlighted where it appears in the article).
Right pane = the original article body.

This is Phase 3 v1 of the review console: a static, self-contained HTML page,
no server, no API key, opens in any browser. Component IDs are already in the
markup (data-component=...) so v2 (click-to-instruct feedback) drops in later.

Two modes:
  --bundle review/<file>.json   render an existing review bundle to HTML
  --from-input <payload.json>   build a bundle from a newsletter payload
                                (re-fetches each source_url for the article body;
                                 falls back to source_excerpt if a fetch fails)

Usage:
  python -m scripts.build_review --bundle review/sample-bundle.json --out review.html
  python -m scripts.build_review --from-input review/pending/<audience>.json --out review.html
"""

from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path

from scripts import anomaly_rank
from scripts import guard_findings
from scripts.render_html import coerce_excerpt, _safe_url

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- bundle build

def build_bundle_from_payload(payload_path: Path) -> dict:
    """Turn a newsletter payload (fixture or saved run output) into a review
    bundle, fetching each source article. Falls back to the source_excerpt as
    the article body if a fetch fails (offline / paywalled), so this never
    blocks review."""
    raw = json.loads(payload_path.read_text())
    content = raw.get("content", raw)  # fixture wraps in {content, meta}; raw payload is flat
    meta = raw.get("meta", {})
    stories = [_story_entry(s) for s in content.get("top_stories", [])]
    # Bench reserves get the SAME article-fetch treatment as top stories, so a
    # promoted reserve shows its original article in the console (content-flow C).
    bench = [_story_entry(s) for s in content.get("bench", []) or []]
    other = [{
        "headline": o.get("headline", ""),
        "subtitle": o.get("subtitle", ""),
        "track": o.get("track", ""),
        "source_url": o.get("source_url", ""),
    } for o in content.get("other_news", []) or []]
    return {
        "newsletter": raw.get("newsletter", meta.get("newsletter", "")),
        "issue_number": meta.get("issue_number"),
        "stories": stories,
        "bench": bench,
        "other_news": other,
        # Surface operational notes (e.g. a Top-3 floor shortfall) so the
        # reviewer SEES them in the console, not just in CI logs.
        "anomalies": content.get("anomalies", []) or [],
    }


def _story_entry(s: dict) -> dict:
    """One full-story review entry: prefer the article body the agent captured
    during the run (robust to paywalls/link-rot); only re-fetch when the payload
    predates that; fall back to the excerpt so review never blocks on a fetch."""
    url = s.get("source_url", "")
    article_text = s.get("article_text", "") or ""
    article_title = s.get("article_title", "") or ""
    fetched = bool(article_text)
    if not fetched and url:
        try:
            from scripts.tools import web_fetch  # lazy — needs network
            r = web_fetch.fetch(url)
            article_text = r.get("body_text", "") or ""
            article_title = r.get("title", "") or ""
            fetched = bool(article_text)
        except Exception as e:  # noqa: BLE001 — review must never crash on a fetch
            print(f"[review] fetch failed for {url}: {e}", file=sys.stderr)
    if not fetched:
        article_text = (
            "[Could not fetch the live article — showing the quoted excerpt as a "
            "stand-in. Open the source link to read the full piece.]\n\n"
            + coerce_excerpt(s.get("source_excerpt"))
        )
    return {
        "headline": s.get("headline", ""),
        "track": s.get("track", ""),
        "summary": s.get("summary", ""),
        "implications": s.get("implications", []) or [],
        "korean_takeaway": s.get("korean_takeaway") or [],
        "source_excerpt": coerce_excerpt(s.get("source_excerpt")),
        "source_url": url,
        "hero_image_url": s.get("hero_image_url") or "",
        "article_title": article_title,
        "article_text": article_text,
        "fetched": fetched,
    }


# ---------------------------------------------------------------- interactive layer (v2)
# Plain-string constants (NOT f-strings) so their { } braces stay literal when
# interpolated into the page. Feedback is captured on LEFT-pane components only
# (our draft) — the right-pane article is read-only reference. Click-to-INSTRUCT,
# not click-to-edit: we capture what should change, we don't edit inline.

REVIEW_CSS_EXTRA = """
  [data-component]{ cursor:pointer; border-radius:4px; transition:background .1s; }
  [data-component].hovering{ outline:2px solid var(--accent); outline-offset:1px; }
  [data-component].annotated{ box-shadow:inset 3px 0 0 var(--mark); background:rgba(255,224,138,.06); }
  .article[data-component]{ cursor:auto; }
  #fbbar{ position:fixed; right:18px; bottom:18px; z-index:20; }
  #fbbar button{ background:var(--accent); color:#0b1020; border:0; border-radius:8px;
    padding:10px 14px; font-weight:700; cursor:pointer; box-shadow:0 4px 16px rgba(0,0,0,.4); }
  #fbpanel{ position:fixed; right:18px; bottom:70px; width:340px; background:var(--card);
    border:1px solid var(--line); border-radius:12px; padding:14px; z-index:21;
    box-shadow:0 12px 40px rgba(0,0,0,.5); display:none; }
  #fbpanel.open{ display:block; }
  #fbpanel .who{ font:11px/1.4 ui-monospace,monospace; color:var(--accent); word-break:break-all; margin-bottom:8px; }
  #fbtags{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }
  #fbtags button{ font-size:11px; padding:4px 8px; border-radius:999px; cursor:pointer;
    background:transparent; color:var(--mut); border:1px solid var(--line); }
  #fbtags button.sel{ background:var(--mark); color:#111; border-color:var(--mark); font-weight:700; }
  #fbtext{ width:100%; height:70px; background:var(--bg); color:var(--ink); border:1px solid var(--line);
    border-radius:8px; padding:8px; font:13px/1.4 inherit; resize:vertical; }
  #fbpanel .row{ display:flex; gap:8px; margin-top:8px; }
  #fbpanel .row button{ flex:1; padding:8px; border-radius:8px; border:0; cursor:pointer; font-weight:700; }
  .btn-save{ background:var(--accent); color:#0b1020; }
  .btn-del{ background:transparent!important; color:#ff7a7a; border:1px solid var(--line)!important; }
"""

FEEDBACK_HTML = """
<div id="fbpanel">
  <div class="who" id="fbwho"></div>
  <div id="fbtags"></div>
  <textarea id="fbtext" placeholder="What should change? e.g. 'tighten this', 'claim not in source', 'wrong audience'"></textarea>
  <div class="row"><button class="btn-save" id="fbsave">Save</button><button class="btn-del" id="fbdel">Delete</button></div>
</div>
<div id="fbbar"><button id="fbexport">Export feedback (<span id="fbcount">0</span>)</button></div>
"""

REVIEW_JS = """
(function(){
  var TAGS=['fabricated','stale','wrong-audience','too-long','unclear','good'];
  var KEY='review_fb_'+document.title;
  var store={}; try{ store=JSON.parse(localStorage.getItem(KEY)||'{}'); }catch(e){}
  var panel=document.getElementById('fbpanel'), who=document.getElementById('fbwho');
  var text=document.getElementById('fbtext'), tagsEl=document.getElementById('fbtags');
  var countEl=document.getElementById('fbcount'), current=null, curTag=null;

  function isTarget(el){ return el && !el.dataset.component.endsWith('.article'); }
  TAGS.forEach(function(t){ var b=document.createElement('button'); b.textContent=t;
    b.onclick=function(){ curTag=(curTag===t?null:t); paintTags(); }; tagsEl.appendChild(b); });
  function paintTags(){ Array.prototype.forEach.call(tagsEl.children, function(b){
    b.classList.toggle('sel', b.textContent===curTag); }); }
  function refresh(){ document.querySelectorAll('[data-component]').forEach(function(el){
    el.classList.toggle('annotated', !!store[el.dataset.component]); });
    countEl.textContent=Object.keys(store).length; }

  var hovered=null;
  document.addEventListener('mousemove', function(e){ var el=e.target.closest('[data-component]');
    if(!isTarget(el)) el=null;
    if(el!==hovered){ if(hovered)hovered.classList.remove('hovering'); hovered=el;
      if(hovered)hovered.classList.add('hovering'); } });

  document.addEventListener('click', function(e){
    if(e.target.closest('#fbpanel')||e.target.closest('#fbbar')) return;
    var el=e.target.closest('[data-component]');
    if(!isTarget(el)){ panel.classList.remove('open'); return; }
    current=el.dataset.component; var ex=store[current]||{};
    text.value=ex.instruction||''; curTag=ex.tag||null; paintTags();
    who.textContent=current; panel.classList.add('open'); text.focus();
  });

  document.getElementById('fbsave').onclick=function(){
    if(!current) return;
    if(!text.value.trim() && !curTag){ delete store[current]; }
    else { store[current]={tag:curTag, instruction:text.value.trim(), ts:Date.now()}; }
    localStorage.setItem(KEY, JSON.stringify(store)); refresh(); panel.classList.remove('open');
  };
  document.getElementById('fbdel').onclick=function(){
    if(current){ delete store[current]; localStorage.setItem(KEY, JSON.stringify(store)); }
    refresh(); panel.classList.remove('open');
  };
  document.getElementById('fbexport').onclick=function(){
    var rows=Object.keys(store).map(function(c){ var v=store[c]; return {component:c, tag:v.tag, instruction:v.instruction, ts:v.ts}; });
    var blob=new Blob([JSON.stringify({title:document.title, exported:Date.now(), feedback:rows}, null, 2)], {type:'application/json'});
    var a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='review-feedback.json'; a.click();
  };
  refresh();
})();
"""


# ---------------------------------------------------------------- HTML render

def _highlight_excerpt(article_text: str, excerpt: str) -> str:
    """Escape the article, then highlight the first source_excerpt sentence
    where it appears so David can spot the quoted passage in context."""
    art = escape(article_text)
    # Use the longest excerpt sentence as the anchor (robust to minor edits).
    anchor = max((p.strip() for p in excerpt.split(".")), key=len, default="").strip()
    if anchor and len(anchor) > 20:
        esc_anchor = escape(anchor)
        if esc_anchor in art:
            art = art.replace(esc_anchor, f'<mark>{esc_anchor}</mark>', 1)
    return art.replace("\n", "<br>")


def _srcurl_html(source_url) -> str:
    """A source link the reviewer can open — but gated to http(s). source_url
    comes from the scraping agent, so a javascript:/data: URL would be a live
    click in the console. Show the raw URL as text either way; link only if safe."""
    disp = escape(source_url or "")
    safe = _safe_url(source_url)
    if safe:
        return f'<a href="{escape(safe)}" target="_blank" rel="noopener">{disp}</a>'
    return f'<span class="unsafe-url" title="non-http(s) URL — not linked">{disp}</span>'


def _render_story_card(prefix: str, i: int, s: dict, ce: str) -> str:
    """Full-story review card (Top stories AND bench reserves share this shape).
    `prefix` drives the data-component ids (story-N / bench-N) the JS + approve
    logic key off of."""
    impl = "".join(
        f'<li data-component="{prefix}-{i}.implication-{j}"{ce}>{escape(x)}</li>'
        for j, x in enumerate(s.get("implications") or [], 1)
    )
    # Korean takeaway — a mirror of the implications for Korean-native readers. It
    # ships in the email, so it must be reviewable + editable here (was invisible
    # before). Only shown for audiences with wants_korean enabled.
    kt_raw = s.get("korean_takeaway") or []
    # korean_takeaway is usually a single (often multi-line) string; sometimes a
    # list of lines. Normalize to a list of lines — iterating a bare string here
    # rendered one <li> per CHARACTER (the console-only Korean garble, fixed
    # 2026-07-07). The email renderer (_render_korean_takeaway) normalizes the same way.
    kt = (
        [ln.strip() for ln in kt_raw.splitlines() if ln.strip()]
        if isinstance(kt_raw, str)
        else [str(x) for x in kt_raw if str(x).strip()]
    )
    korean_block = ""
    if kt:
        klines = "".join(
            f'<li data-component="{prefix}-{i}.korean-{j}"{ce}>{escape(x)}</li>'
            for j, x in enumerate(kt, 1)
        )
        korean_block = (
            '<div class="label">Korean takeaway · 한국어 (ships in the email)</div>'
            f'<ul class="impl korean">{klines}</ul>'
        )
    fetch_note = "" if s.get("fetched") else (
        '<div class="warn">⚠ live article not fetched — excerpt shown as stand-in</div>'
    )
    article_html = _highlight_excerpt(s.get("article_text", ""), s.get("source_excerpt", ""))
    # Hero preview — only the Big Signal (Story 01) is illustrated, matching the
    # email. Show its image so it can be reviewed; call out a missing/rejected one.
    # Lower top stories, bench, and Other News are text-only (no hero block).
    hero_html = ""
    if prefix == "story" and i == 1:
        hero = _safe_url(s.get("hero_image_url"))
        hero_html = (f'<img class="hero" src="{escape(hero)}" alt="" loading="lazy">'
                     if hero else '<div class="nohero">no hero image</div>')
    return f"""
<section class="card" data-component="{prefix}-{i}">
  <div class="cardhead"><span class="num">{i:02d}</span>
    <span class="track">{escape(s.get('track',''))}</span></div>
  <div class="cols">
    <div class="pane left">
      <div class="panetag">OUR DRAFT</div>
      {hero_html}
      <h2 data-component="{prefix}-{i}.headline"{ce}>{escape(s.get('headline',''))}</h2>
      <p class="summary" data-component="{prefix}-{i}.summary"{ce}>{escape(s.get('summary',''))}</p>
      <div class="label">Implications</div>
      <ul class="impl">{impl}</ul>
      {korean_block}
      <div class="label">Quoted source excerpt</div>
      <blockquote data-component="{prefix}-{i}.excerpt"{ce}>{escape(s.get('source_excerpt',''))}</blockquote>
    </div>
    <div class="pane right">
      <div class="panetag">ORIGINAL ARTICLE</div>
      {fetch_note}
      <div class="srcurl">{_srcurl_html(s.get('source_url'))}</div>
      <div class="article" data-component="{prefix}-{i}.article">{article_html}</div>
    </div>
  </div>
</section>"""


def _render_other_card(i: int, o: dict, ce: str) -> str:
    """Lightweight Other-News review card — headline + one-line subtitle + source.
    No article pane, implications, or excerpt (Other News is scan-only)."""
    return f"""
<section class="card othercard" data-component="other-{i}">
  <div class="cardhead"><span class="num">O{i:02d}</span>
    <span class="track">{escape(o.get('track',''))}</span></div>
  <div class="pane left onepane">
    <div class="panetag">OTHER NEWS</div>
    <h2 data-component="other-{i}.headline"{ce}>{escape(o.get('headline',''))}</h2>
    <p class="summary" data-component="other-{i}.subtitle"{ce}>{escape(o.get('subtitle',''))}</p>
    <div class="srcurl">{_srcurl_html(o.get('source_url'))}</div>
  </div>
</section>"""


def render_review_html(bundle: dict, editable: bool = False) -> str:
    stories = bundle.get("stories", [])
    issue = bundle.get("issue_number")
    nl = bundle.get("newsletter", "")
    title = f"Review — {nl or 'newsletter'}" + (f" #{issue:03d}" if isinstance(issue, int) else "")
    # In edit mode, the left-pane draft components become directly editable;
    # the instruct/feedback layer is omitted (you edit, you don't annotate).
    ce = ' contenteditable="true" spellcheck="true"' if editable else ''

    # Operational notes, split into "needs a decision" vs "provenance". Showing
    # all of them under one ⚠ banner trained the reviewer to skim (Ledger № 007:
    # 7 notes, 6 needing no action) — and a gate that gets skimmed stops working.
    # Nothing is hidden: the provenance list is still rendered, just quietly.
    decides, notes = anomaly_rank.split(bundle.get("anomalies"))
    # BLOCKING findings get a checkbox each. They are a strict subset of
    # `decides`, so render them there and drop them from the plain list — the
    # send refuses to run while any is unticked (approved_artifact.verify), so
    # this checkbox is the only way past a guard, and ticking it is recorded
    # into the signed artifact.
    _blocking = guard_findings.blocking(bundle.get("anomalies"))
    _block_msgs = {b["message"] for b in _blocking}
    decides = [d for d in decides if d not in _block_msgs]

    anomalies_html = ""
    if _blocking:
        _b = "".join(
            f'<li><label><input type="checkbox" class="ackbox" data-ack-id="{escape(b["id"])}"> '
            f'<span class="ackcode">{escape(b["code"])}</span> {escape(b["message"])}</label></li>'
            for b in _blocking)
        anomalies_html += (
            f'<div class="anomalies blocking" role="alert">'
            f'<div class="anomalies-h">&#9940; {len(_blocking)} blocking finding'
            f'{"" if len(_blocking) == 1 else "s"} — the send will refuse until you '
            f'acknowledge {"it" if len(_blocking) == 1 else "each one"}</div>'
            f'<ul>{_b}</ul></div>'
        )
    if decides:
        _items = "".join(f"<li>{escape(a)}</li>" for a in decides)
        anomalies_html += (
            f'<div class="anomalies" role="alert">'
            f'<div class="anomalies-h">&#9888; {escape(anomaly_rank.banner(decides, notes))}</div>'
            f'<ul>{_items}</ul></div>'
        )
    if notes:
        _n = "".join(f"<li>{escape(a)}</li>" for a in notes)
        _summary = (anomaly_rank.banner(decides, notes) if not decides
                    else f"{len(notes)} note{'' if len(notes) == 1 else 's'} — no action needed")
        anomalies_html += (
            f'<details class="provenance"><summary>{escape(_summary)}</summary>'
            f'<ul>{_n}</ul></details>'
        )

    cards = [_render_story_card("story", i, s, ce) for i, s in enumerate(stories, 1)]
    bench = bundle.get("bench") or []
    if bench:
        cards.append('<div class="sectionhead">Bench &mdash; reserves '
                     '(auto-promoted if you drop a Top story; edit or drop them here too)</div>')
        cards += [_render_story_card("bench", i, s, ce) for i, s in enumerate(bench, 1)]
    other = bundle.get("other_news") or []
    if other:
        cards.append('<div class="sectionhead">Other News &mdash; scan items (edit or drop)</div>')
        cards += [_render_other_card(i, o, ce) for i, o in enumerate(other, 1)]

    edit_css = (
        "\n  .pane.left [data-component]{outline:1px dashed transparent; border-radius:3px; transition:outline-color .1s;}"
        "\n  .pane.left [data-component]:hover{outline-color:#3a3f4d;}"
        "\n  .pane.left [data-component]:focus{outline:1px solid var(--accent); background:rgba(124,156,255,.06);}"
    ) if editable else ""
    sub_html = (
        f"Side-by-side review &middot; {len(stories)} stories &middot; <b>edit any part of the draft (left) directly</b>, "
        "then approve &mdash; your edits are what gets sent."
        if editable else
        "Side-by-side review &middot; our draft (left) vs. original article (right). "
        "Highlighted = the passage we quoted &middot; <b>click any part of our draft to leave an instruction</b>."
    )
    interactive = "" if editable else f"{FEEDBACK_HTML}\n<script>{REVIEW_JS}</script>"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
<style>
  :root {{ --bg:#0f1117; --card:#1a1d27; --line:#2b2f3a; --ink:#e7e9ee; --mut:#9aa3b2;
           --accent:#7c9cff; --mark:#ffe08a; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
          font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }}
  header {{ position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line);
            padding:14px 24px; z-index:5; }}
  header h1 {{ margin:0; font-size:16px; letter-spacing:.02em; }}
  header .sub {{ color:var(--mut); font-size:12px; margin-top:2px; }}
  main {{ max-width:1280px; margin:0 auto; padding:24px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           margin-bottom:22px; overflow:hidden; }}
  .cardhead {{ display:flex; align-items:center; gap:12px; padding:12px 18px;
               border-bottom:1px solid var(--line); }}
  .num {{ font-weight:800; color:var(--accent); font-variant-numeric:tabular-nums; }}
  .track {{ font-size:11px; text-transform:uppercase; letter-spacing:.12em; color:var(--mut); }}
  .cols {{ display:grid; grid-template-columns:1fr 1fr; }}
  .pane {{ padding:18px 20px; }}
  .pane.left {{ border-right:1px solid var(--line); }}
  .panetag {{ font-size:10px; font-weight:700; letter-spacing:.16em; color:var(--mut);
              text-transform:uppercase; margin-bottom:10px; }}
  .pane h2 {{ margin:0 0 10px; font-size:20px; line-height:1.25; }}
  .summary {{ margin:0 0 14px; }}
  .label {{ font-size:10px; font-weight:700; letter-spacing:.14em; color:var(--accent);
            text-transform:uppercase; margin:14px 0 6px; }}
  .impl {{ margin:0 0 6px; padding-left:18px; }}
  .impl li {{ margin-bottom:5px; }}
  blockquote {{ margin:0; padding:10px 14px; border-left:3px solid var(--accent);
                background:rgba(124,156,255,.07); color:var(--ink); font-size:14px; }}
  .srcurl {{ font-size:12px; margin-bottom:10px; word-break:break-all; }}
  .srcurl a {{ color:var(--accent); }}
  .article {{ font-size:14px; color:#cfd4de; max-height:540px; overflow:auto;
              padding-right:8px; }}
  .article mark {{ background:var(--mark); color:#111; padding:0 2px; }}
  .warn {{ font-size:12px; color:#ffb454; margin-bottom:8px; }}
  .anomalies {{ background:rgba(255,120,80,.12); border:1px solid #ff7850; border-radius:8px;
                padding:12px 16px; margin-bottom:20px; color:#ffcab0; font-size:13px; }}
  .anomalies-h {{ font-weight:700; margin-bottom:6px; color:#ff9d7a; }}
  .anomalies ul {{ margin:0; padding-left:18px; }}
  /* Blocking: louder than a decision, because the send genuinely refuses. */
  .anomalies.blocking {{ background:rgba(255,60,60,.16); border:2px solid #ff4d4d; color:#ffd6d6; }}
  .anomalies.blocking .anomalies-h {{ color:#ff6b6b; }}
  .anomalies.blocking ul {{ list-style:none; padding-left:0; }}
  .anomalies.blocking li {{ margin:6px 0; }}
  .anomalies.blocking label {{ cursor:pointer; display:flex; gap:8px; align-items:flex-start; }}
  .ackcode {{ font:11px/1.6 ui-monospace,monospace; color:#ff9d9d; text-transform:uppercase;
              border:1px solid #ff6b6b; border-radius:4px; padding:0 5px; flex:none; }}
  /* Provenance: kept visible but deliberately quiet — these need no decision. */
  .provenance {{ background:rgba(255,255,255,.04); border:1px solid #333; border-radius:8px;
                 padding:10px 16px; margin-bottom:20px; color:#9aa0a6; font-size:12.5px; }}
  .provenance summary {{ cursor:pointer; font-weight:600; color:#b9bfc6; }}
  .provenance ul {{ margin:8px 0 0; padding-left:18px; }}
  .sectionhead {{ font-size:12px; font-weight:700; letter-spacing:.14em; text-transform:uppercase;
                  color:var(--mut); margin:8px 2px 14px; padding-top:6px; border-top:1px solid var(--line); }}
  .othercard .onepane {{ border-right:none; }}
  .hero {{ display:block; width:100%; max-width:100%; height:auto; border-radius:6px;
           margin-bottom:12px; background:#0b0d12; border:1px solid var(--line); }}
  .nohero {{ font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--mut);
             border:1px dashed var(--line); border-radius:6px; padding:14px; text-align:center; margin-bottom:12px; }}
  @media (max-width:900px) {{ .cols {{ grid-template-columns:1fr; }}
                              .pane.left {{ border-right:none; border-bottom:1px solid var(--line); }} }}
{REVIEW_CSS_EXTRA}{edit_css}
</style></head>
<body>
<header><h1>{escape(title)}</h1>
<div class="sub">{sub_html}</div></header>
<main>{anomalies_html}{''.join(cards)}</main>
{interactive}
</body></html>"""


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--bundle", help="render an existing review bundle JSON")
    g.add_argument("--from-input", help="build a bundle from a payload JSON (fetches articles)")
    ap.add_argument("--out", default="review.html")
    ap.add_argument("--save-bundle", help="when using --from-input, also write the built bundle here")
    args = ap.parse_args()

    if args.bundle:
        bundle = json.loads(Path(args.bundle).read_text())
    else:
        bundle = build_bundle_from_payload(Path(args.from_input))
        if args.save_bundle:
            Path(args.save_bundle).write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
            print(f"[review] wrote bundle → {args.save_bundle}")

    html = render_review_html(bundle)
    Path(args.out).write_text(html)
    print(f"[review] wrote {args.out}  ({len(bundle.get('stories', []))} stories)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
