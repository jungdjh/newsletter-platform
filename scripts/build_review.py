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
  python -m scripts.build_review --from-input review/ai-pms-ranked.json --out review.html
  python -m scripts.build_review --from-input review/ai-pms-ranked.json --out review.html
"""

from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path

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
    stories = []
    for s in content.get("top_stories", []):
        url = s.get("source_url", "")
        article_text, article_title, fetched = "", "", False
        if url:
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
                + s.get("source_excerpt", "")
            )
        stories.append({
            "headline": s.get("headline", ""),
            "track": s.get("track", ""),
            "summary": s.get("summary", ""),
            "implications": s.get("implications", []) or [],
            "source_excerpt": s.get("source_excerpt", ""),
            "source_url": url,
            "article_title": article_title,
            "article_text": article_text,
            "fetched": fetched,
        })
    return {
        "newsletter": raw.get("newsletter", meta.get("newsletter", "")),
        "issue_number": meta.get("issue_number"),
        "stories": stories,
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
    where it appears so the reviewer can spot the quoted passage in context."""
    art = escape(article_text)
    # Use the longest excerpt sentence as the anchor (robust to minor edits).
    anchor = max((p.strip() for p in excerpt.split(".")), key=len, default="").strip()
    if anchor and len(anchor) > 20:
        esc_anchor = escape(anchor)
        if esc_anchor in art:
            art = art.replace(esc_anchor, f'<mark>{esc_anchor}</mark>', 1)
    return art.replace("\n", "<br>")


def render_review_html(bundle: dict) -> str:
    stories = bundle.get("stories", [])
    issue = bundle.get("issue_number")
    nl = bundle.get("newsletter", "")
    title = f"Review — {nl or 'newsletter'}" + (f" #{issue:03d}" if isinstance(issue, int) else "")

    cards = []
    for i, s in enumerate(stories, 1):
        impl = "".join(
            f'<li data-component="story-{i}.implication-{j}">{escape(x)}</li>'
            for j, x in enumerate(s["implications"], 1)
        )
        fetch_note = "" if s.get("fetched") else (
            '<div class="warn">⚠ live article not fetched — excerpt shown as stand-in</div>'
        )
        article_html = _highlight_excerpt(s["article_text"], s["source_excerpt"])
        cards.append(f"""
<section class="card" data-component="story-{i}">
  <div class="cardhead"><span class="num">{i:02d}</span>
    <span class="track">{escape(s['track'])}</span></div>
  <div class="cols">
    <div class="pane left">
      <div class="panetag">OUR DRAFT</div>
      <h2 data-component="story-{i}.headline">{escape(s['headline'])}</h2>
      <p class="summary" data-component="story-{i}.summary">{escape(s['summary'])}</p>
      <div class="label">Implications</div>
      <ul class="impl">{impl}</ul>
      <div class="label">Quoted source excerpt</div>
      <blockquote data-component="story-{i}.excerpt">{escape(s['source_excerpt'])}</blockquote>
    </div>
    <div class="pane right">
      <div class="panetag">ORIGINAL ARTICLE</div>
      {fetch_note}
      <div class="srcurl"><a href="{escape(s['source_url'])}" target="_blank" rel="noopener">{escape(s['source_url'])}</a></div>
      <div class="article" data-component="story-{i}.article">{article_html}</div>
    </div>
  </div>
</section>""")

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
  @media (max-width:900px) {{ .cols {{ grid-template-columns:1fr; }}
                              .pane.left {{ border-right:none; border-bottom:1px solid var(--line); }} }}
{REVIEW_CSS_EXTRA}
</style></head>
<body>
<header><h1>{escape(title)}</h1>
<div class="sub">Side-by-side review · {len(stories)} stories · our draft (left) vs. original article (right). Highlighted = the passage we quoted · <b>click any part of our draft to leave an instruction</b>.</div></header>
<main>{''.join(cards)}</main>
{FEEDBACK_HTML}
<script>{REVIEW_JS}</script>
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
