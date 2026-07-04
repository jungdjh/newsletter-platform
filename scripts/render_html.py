"""Render a newsletter as Outlook-safe HTML + plaintext.

Takes structured story payload from the agent loop, applies the newsletter's
palette (cobalt/green for Ledger, teal/lime for Pulse, burgundy/magenta for
Download), and emits a 600px table-based email that survives strict
corporate Outlook (the v8-polish design we landed on).

Public API:
    render_newsletter(newsletter, content, meta) -> (html_str, plaintext_str)

Where:
    newsletter: 'ledger' | 'pulse' | 'download'
    content: {
        "top3": [Story, ...],         # exactly 2 or 3 entries
        "watchlist": [Signal, ...],   # 0-3 entries, omit section if empty
        "also_noted": [Item, ...],    # 0-5 entries
    }
    meta: {
        "issue_number": int,
        "date_dd_mm_yy": str,         # e.g. "19.05.26"
        "weekday_str": str,           # e.g. "Mon"
        "edition_label": str,         # e.g. "MORNING EDI." (always uppercase)
        "filed_time_ct": str,         # e.g. "07:00 CT"
        "min_read": int,
    }
"""

from __future__ import annotations

from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as _date_parser

_CENTRAL_TZ = ZoneInfo("America/Chicago")
_UTC_TZ = ZoneInfo("UTC")


def _safe_url(url: Any) -> str:
    """Return url only if it's an http(s) link, else "".

    source_url / hero_image_url come from an LLM agent scraping the open web, so
    a hostile or malformed page could yield a `javascript:` or `data:` URL that
    survives html.escape() and becomes a live href / img src in the email.
    Restrict to http(s); anything else (including empty / non-str) is dropped.
    """
    if not isinstance(url, str):
        return ""
    u = url.strip()
    if u.lower().startswith(("http://", "https://")):
        return u
    return ""


def coerce_excerpt(value: Any) -> str:
    """Flatten a possibly-malformed source_excerpt into one clean string.

    The model occasionally double-serializes the submit_newsletter payload as a
    JSON *string* with unescaped quotes inside source_excerpt (verbatim article
    quotes). The json-repair fallback in agent_loop then either splits the
    excerpt across stray object keys or hands it back as a list/dict fragment.
    Every consumer (Sr. Editor claim-verification, the build_review blockquote)
    expects a single continuous string, so coerce any of those shapes — str /
    list / dict / None — back into one. Degrades gracefully instead of breaking
    layout or tripping the editor's "excerpt split across multiple keys" flag.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(p for p in (coerce_excerpt(v) for v in value) if p)
    if isinstance(value, dict):
        # Fragmented shape: both keys and values are pieces of the original text.
        parts: list[str] = []
        for k, v in value.items():
            parts.append(str(k).strip())
            parts.append(coerce_excerpt(v))
        return " ".join(p for p in parts if p)
    return str(value).strip()


def _format_filed_from_published(published_at_iso: str | None) -> str:
    """Convert an article's ISO 8601 publish timestamp to 'HH:MM CT'.

    Returns empty string if missing or unparseable — caller skips the
    'Filed' portion of the meta strip when this is empty.

    Handles common quirks:
      - Naive timestamps (no tzinfo) — assumed UTC (common WordPress default)
      - Mixed formats: "2026-05-23T17:30:00Z", "2026-05-23T17:30:00+00:00",
        "2026-05-23 17:30:00", "Sat, 23 May 2026 17:30:00 GMT"
      - Garbage strings the publisher dumped into og:published_time
    """
    if not published_at_iso:
        return ""
    try:
        dt = _date_parser.parse(published_at_iso)
    except (ValueError, TypeError, OverflowError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC_TZ)
    try:
        return dt.astimezone(_CENTRAL_TZ).strftime("%H:%M CT")
    except (ValueError, OverflowError):
        return ""

# --- High-contrast chassis --------------------------------------------------
# Every newsletter shares ONE bulletproof structure — black canvas, white story
# cards, a near-black hero card — so contrast + dark-mode survival are constant.
# Per-audience identity is a single ACCENT color (from the brand), plus a light
# TINT of it for pills/implications on white cards. The model David approved:
# lock the chassis, swap the accent.
def _chassis(accent: str, *, tint: str, tint_ink: str) -> dict[str, str]:
    return {
        "outer_bg": "#0A0A0A",
        "card_bg": "#0A0A0A",            # the "paper"/gutter is the black canvas
        "white": "#FFFFFF",              # white story-card surface
        "big_signal_bg": "#141414",      # near-black hero card (lifted off the canvas)
        "big_signal_bg_gradient_end": "#141414",  # flat — no gradient to get mangled
        "big_signal_accent": accent,     # hero numeral, pills, CTA, headline
        "primary": accent,               # numerals/links/top-bar on white cards
        "primary_tint": tint,
        "ink": "#0A0A0A",                # black text on white cards
        "ink_muted": "#6B6B6B",
        "ink_hairline": "#E8E8E8",       # hairline between items on the white Other-news card
        "story_pill_bg": "#0A0A0A",
        "story_pill_fg": "#FFFFFF",
        "track_tag_bg": tint,            # light accent tint — pill on white cards + on black
        "track_tag_fg": tint_ink,        # readable accent text on the tint
        "card_border": "#1A1A1A",
        "implications_bg": tint,
        "implications_border": accent,
        "big_signal_meta_color": "#9A9A9A",
        "big_signal_meta_pipe": "#444444",
        "big_signal_body": "#ECECEC",    # near-white body on the dark hero
        "big_signal_pill_border": accent,
        "footer_brand_main": accent,
        "footer_brand_sub": "#FFFFFF",
        "footer_pipe": "#444444",
        "wordmark_period": accent,
        "footer_wordmark_period": accent,
    }


# --- Palettes ---------------------------------------------------------------
# The original newsletters' bespoke branding lives in the audience config
# packs (config/audiences/<pack>/palettes.json). Each entry carries branding
# text plus a `chassis` block (accent + tint + tint_ink) that we expand onto
# the shared high-contrast chassis here. Empty when no pack is present.

def _load_palettes() -> dict[str, dict[str, str]]:
    from scripts import audience_config
    palettes: dict[str, dict[str, str]] = {}
    for key, entry in audience_config.load_palettes().items():
        entry = dict(entry)
        chassis = entry.pop("chassis")
        palettes[key] = {
            **entry,
            **_chassis(chassis["accent"], tint=chassis["tint"], tint_ink=chassis["tint_ink"]),
        }
    return palettes


PALETTES: dict[str, dict[str, str]] = _load_palettes()


def _load_feedback_email() -> str:
    from scripts import audience_config
    return audience_config.feedback_email() or "feedback@example.com"


# Reply-with-feedback address in the footer — operator setting from the
# audience config pack, with a placeholder default for pack-less checkouts.
FEEDBACK_EMAIL: str = _load_feedback_email()

# --- Per-audience theming ---------------------------------------------------
# A platform audience's brief spec carries a small `theme` (3 base colors).
# The full ~25-key palette is DERIVED from it, so every audience gets its own
# consistent visual identity — generated once per audience, reused every issue
# (a stable brand, not a per-run design). No pack palette involved.

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]

def _rgb_to_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c)))) for c in rgb)

def _mix(h1: str, h2: str, t: float) -> str:
    a, b = _hex_to_rgb(h1), _hex_to_rgb(h2)
    return _rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))

def _tint(h: str, t: float) -> str:   # toward white
    return _mix(h, "#FFFFFF", t)


def _palette_from_theme(theme: dict) -> dict[str, str]:
    """Platform audiences ride the same high-contrast chassis; their identity is
    a single accent pulled from the spec's `theme` (primary preferred, else
    accent). The light tint + its ink are derived for readable pills on white."""
    accent = theme.get("primary") or theme.get("accent") or "#3B5BFF"
    return _chassis(accent, tint=_tint(accent, 0.88), tint_ink=_mix(accent, "#000000", 0.30))


def _effective_palette(newsletter: str) -> dict[str, Any]:
    """Palette for a newsletter. Pack newsletters have bespoke palettes
    (config/audiences/). ANY other name is a platform audience: colors come
    from the spec's `theme` (derived), and ALL branding TEXT comes from the
    spec — nothing pack-specific."""
    if newsletter in PALETTES:
        return PALETTES[newsletter]
    import json as _json
    from pathlib import Path as _Path
    spec_path = _Path(__file__).resolve().parent.parent / "briefs" / f"{newsletter}.json"
    spec = _json.loads(spec_path.read_text()) if spec_path.exists() else {}
    theme = spec.get("theme")
    if theme:
        pal = _palette_from_theme(theme)
    else:
        # Theme-less spec: the pack can nominate one of its palettes as the
        # fallback brand; without one, derive a neutral default.
        from scripts import audience_config
        fallback = audience_config.fallback_palette()
        pal = dict(PALETTES[fallback]) if fallback in PALETTES else _palette_from_theme({})
    name = spec.get("display_name", newsletter)
    pal["name"] = name
    pal["category_main"] = spec.get("category_main") or name
    pal["category_sub"] = spec.get("category_sub", "Intel")
    pal["subtitle"] = spec.get("subtitle", pal.get("subtitle", ""))
    pal["implications_label"] = spec.get("implications_label", "For readers")
    pal["footer_tagline"] = spec.get("footer_tagline", f"Curated for {name} readers.")
    pal["wants_korean"] = spec.get("wants_korean", False)
    pal["feedback_subject_token"] = name
    return pal


# Watchlist tag colors (semantic, shared across newsletters)
WATCHLIST_COLORS = {
    "Filing":  "#6D28D9",
    "Rumor":   "#D97706",
    "Signal":  "#475569",
    "Leak":    "#DC2626",
    "Beta":    "#0D9488",
}

# Common font stacks
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"
SERIF = "Georgia, 'Times New Roman', serif"
ARIAL_BLACK = "'Arial Black','Helvetica Neue',Arial,sans-serif"


# --- Public API -------------------------------------------------------------

def render_newsletter(
    newsletter: str,
    content: dict[str, Any],
    meta: dict[str, Any],
    *,
    editor_concerns: dict[str, Any] | None = None,
    include_reply_footer: bool = True,
) -> tuple[str, str]:
    """Render the newsletter to HTML and plaintext.

    Shape (as of 2026-05-22, Download-only, 2x/week cadence):
      content = {
        "top_stories": [{...story dict...}, ...]  # 1 to 3 stories.
                                                  # First is Big Signal.
        "other_news":  [{...item dict...}, ...]   # 0 to 5 items
      }

    editor_concerns (optional): {
        "verdict": "PASS" | "FAIL",
        "must_fix": [str, ...],
        "notes": str,
    }
    If provided AND must_fix is non-empty, renders a yellow advisory
    banner at the very top of the email body so David sees it first
    during morning review.
    """
    # Pack newsletters use bespoke palettes; platform audiences get a derived
    # palette with branding text pulled from briefs/<name>.json.
    palette = _effective_palette(newsletter)
    top_stories = _enforce_top_stories_caps(content.get("top_stories") or [])
    other_news = _trim_other_news(content.get("other_news") or [])

    html = _build_html(palette, top_stories, other_news, meta, editor_concerns,
                       include_reply_footer=include_reply_footer)
    plaintext = _build_plaintext(palette, top_stories, other_news, meta, editor_concerns,
                                 include_reply_footer=include_reply_footer)
    return html, plaintext


# --- Deterministic length-cap helpers ---------------------------------------

def _truncate_words(text: str, max_words: int) -> str:
    """Truncate to N words, appending ellipsis if cut. Preserves trailing period."""
    if not isinstance(text, str):
        return text
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = " ".join(words[:max_words]).rstrip(",;:")
    if not truncated.endswith("."):
        truncated = truncated.rstrip(".") + "…"
    return truncated


def _truncate_chars(text: str, max_chars: int) -> str:
    if not isinstance(text, str) or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip(" ,;:.") + "…"


def _truncate_chars_word(text: str, max_chars: int) -> str:
    """Like _truncate_chars but clips at the last whole word inside max_chars,
    so the ellipsis never lands mid-word (e.g. 'undercuts ACH…' not 'ACH o…')."""
    if not isinstance(text, str) or len(text) <= max_chars:
        return text
    head = text[:max_chars]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:.—-") + "…"


def _enforce_top_stories_caps(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim each top story's summary (≤35 words) and implications bullets (≤14 words). Cap to 3."""
    out = []
    for story in stories[:3]:
        if not story:
            continue
        s = dict(story)
        if s.get("summary"):
            s["summary"] = _truncate_words(s["summary"], 35)
        if isinstance(s.get("implications"), list):
            s["implications"] = [
                _truncate_words(b, 14) if isinstance(b, str) else b
                for b in s["implications"]
            ]
        out.append(s)
    return out


def _trim_other_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim Other News fields: headline (≤80 chars), subtitle (≤14 words, ≤72 chars).
    NO summary in Other News — it's a scan-only block. Track + Headline +
    Subtitle + Source URL only. The subtitle char cap (~72) keeps it to a
    single rendered line at the ~500px content width / 13px font — the CSS
    one-line clamp can't be relied on in email tables, so it hard-cuts."""
    out = []
    for item in items[:5]:
        clean = dict(item)
        if clean.get("headline"):
            clean["headline"] = _truncate_chars(clean["headline"], 80)
        if clean.get("subtitle"):
            clean["subtitle"] = _truncate_chars_word(_truncate_words(clean["subtitle"], 14), 72)
        # Strip summary if agent sent one (no longer rendered)
        clean.pop("summary", None)
        out.append(clean)
    return out


# --- HTML builders ----------------------------------------------------------

def _build_html(palette, top_stories, other_news, meta, editor_concerns=None, *, include_reply_footer=True) -> str:
    issue_str = f"{meta['issue_number']:03d}"
    parts: list[str] = []
    parts.append(_html_head(palette, meta, top_stories))
    parts.append(_html_open_outer(palette))
    # Editor concerns banner — shown FIRST so David sees it during AM review.
    # It's a sending-mode artifact (the banner itself says "delete this block
    # before sending"), so the demo/public render omits it for a clean look.
    if include_reply_footer and editor_concerns and editor_concerns.get("must_fix"):
        parts.append(_html_editor_concerns(palette, editor_concerns))
    parts.append(_html_top_accent(palette))
    parts.append(_html_masthead(palette, issue_str, meta))
    parts.append(_html_wordmark(palette))
    parts.append(_html_tagline(palette))
    parts.append(_html_divider_row(palette))
    parts.append(_html_edition_strip(palette, meta))
    parts.append(_html_toc(palette, top_stories, other_news))
    for idx, story in enumerate(top_stories):
        if idx == 0:
            parts.append(_html_big_signal_card(palette, story))
        else:
            parts.append(_html_standard_card(palette, story, idx))
    if other_news:
        parts.append(_html_other_news(palette, other_news))
    parts.append(_html_footer(palette, issue_str, meta, include_reply_footer=include_reply_footer))
    parts.append(_html_close_outer())
    parts.append("</body></html>")
    return "".join(parts)


def _html_head(palette, meta, top_stories) -> str:
    issue_str = f"{meta['issue_number']:03d}"
    preheader_parts = [escape(s.get("headline", "")) for s in top_stories]
    preheader = " &middot; ".join(preheader_parts)
    return f"""<!doctype html><html lang="en"><head>\
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">\
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark">\
<title>{escape(palette['name'])} &middot; &#8470; {issue_str} &mdash; {escape(meta['date_dd_mm_yy'])}</title>\
<style>:root{{color-scheme:dark;supported-color-schemes:dark;}}</style>\
</head><body bgcolor="{palette['outer_bg']}" style="margin:0; padding:0; background:{palette['outer_bg']}; -webkit-font-smoothing:antialiased;">\
<div style="display:none; max-height:0; overflow:hidden; opacity:0; color:transparent;">{preheader}</div>"""


def _html_open_outer(palette) -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'bgcolor="{palette["outer_bg"]}" style="background:{palette["outer_bg"]};">'
        f'<tr><td align="center" style="padding:32px 12px 48px 12px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" '
        f'style="width:600px; max-width:600px; background:{palette["card_bg"]};">'
    )


def _html_close_outer() -> str:
    return "</table></td></tr></table>"


def _html_top_accent(palette) -> str:
    return (
        f'<tr><td bgcolor="{palette["primary"]}" height="6" '
        f'style="height:6px; line-height:6px; font-size:0; background:{palette["primary"]};">&nbsp;</td></tr>'
    )


def _html_editor_concerns(palette, concerns: dict[str, Any]) -> str:
    """Render the Sr. Editor advisory banner at the top of the email.

    Visible only to David during review — this block should be DELETED
    from the draft before sending to subscribers. We mark it visually so
    it's obvious it's review-only.
    """
    p = palette
    must_fix = concerns.get("must_fix", []) or []
    notes = concerns.get("notes", "") or ""
    verdict = concerns.get("verdict", "")

    items_html = "".join(
        f'<tr><td style="padding:4px 0 0 0; font-family:{SANS}; font-size:13px; line-height:1.5; color:#5C4400;">'
        f'<span style="color:#996F00; font-weight:700;">&bull;</span> {escape(item)}</td></tr>'
        for item in must_fix
    )

    notes_html = ""
    if notes:
        notes_html = (
            f'<tr><td style="padding:10px 0 0 0; font-family:{SANS}; font-size:12px; line-height:1.5; color:#7A5D00; font-style:italic;">'
            f'{escape(notes)}</td></tr>'
        )

    return (
        f'<tr><td bgcolor="#FFF4CC" style="background:#FFF4CC; border-left:4px solid #E6B800; padding:14px 18px 14px 22px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        f'<tr><td style="font-family:{MONO}; font-size:10px; font-weight:700; letter-spacing:0.14em; text-transform:uppercase; color:#996F00; line-height:1; padding-bottom:6px;">'
        f'&#9888;&nbsp;Sr.&nbsp;Editor&nbsp;advisory &middot; verdict: {escape(verdict)} &middot; <em style="font-style:italic; font-weight:500;">delete this block before sending</em>'
        f'</td></tr>'
        f'<tr><td style="font-family:{SANS}; font-size:13px; font-weight:600; line-height:1.4; color:#5C4400; padding-bottom:4px;">'
        f'{len(must_fix)} concern{"s" if len(must_fix) != 1 else ""} to review:'
        f'</td></tr>'
        + items_html
        + notes_html
        + "</table></td></tr>"
    )


def _html_masthead(palette, issue_str, meta) -> str:
    return f"""<tr><td bgcolor="{palette['card_bg']}" style="background:{palette['card_bg']}; padding:26px 28px 0 28px;">\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>\
<td valign="middle" style="vertical-align:middle; font-family:{SANS}; font-size:12px; font-weight:700; letter-spacing:0.12em; text-transform:uppercase; color:#B0B0B0; line-height:1;">\
{escape(palette['category_main'])} <span style="color:#444444; padding:0 6px;">/</span><span style="color:#8C8C8C;">{escape(palette['category_sub'])}</span>\
</td>\
<td align="right" valign="middle" style="font-family:{MONO}; font-size:11px; font-weight:700; letter-spacing:0.04em; color:{palette['primary']}; line-height:1; white-space:nowrap;">No.&nbsp;{issue_str}&nbsp;&nbsp;&middot;&nbsp;&nbsp;{escape(meta['date_dd_mm_yy'])}</td>\
</tr></table></td></tr>"""


def _html_wordmark(palette) -> str:
    name = palette["name"]
    return f"""<tr><td bgcolor="{palette['card_bg']}" style="background:{palette['card_bg']}; padding:16px 28px 0 28px;">\
<div style="font-family:{SANS}; font-size:50px; font-weight:900; letter-spacing:-0.03em; line-height:0.96; text-transform:uppercase; color:#FFFFFF;">\
{escape(name)}<span style="color:{palette['wordmark_period']};">.</span></div></td></tr>"""


def _html_tagline(palette) -> str:
    return f"""<tr><td bgcolor="{palette['card_bg']}" style="background:{palette['card_bg']}; padding:12px 28px 0 28px;">\
<div style="font-family:{SANS}; font-size:14px; font-weight:500; line-height:1.5; color:#9A9A9A;">{palette['subtitle']}</div></td></tr>"""


def _html_divider_row(palette) -> str:
    return f"""<tr><td bgcolor="{palette['card_bg']}" style="background:{palette['card_bg']}; padding:18px 28px 0 28px;">\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">\
<tr><td bgcolor="{palette['primary']}" height="2" style="height:2px; line-height:2px; font-size:0; background:{palette['primary']};">&nbsp;</td></tr></table></td></tr>"""


def _html_edition_strip(palette, meta) -> str:
    weekday = meta.get("weekday_str", "")
    date = meta["date_dd_mm_yy"]
    min_read = meta.get("min_read", 6)
    # Day + date together. No edition label, no filed time — generation/review
    # happen at different times so those got noisy. Just date + read estimate.
    day_label = f"{weekday}&nbsp;{date.replace('.','&nbsp;')}" if weekday else date
    return f"""<tr><td bgcolor="{palette['card_bg']}" style="background:{palette['card_bg']}; padding:12px 28px 22px 28px;">\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>\
<td valign="middle" style="font-family:{MONO}; font-size:11px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; color:#B0B0B0; line-height:1;">\
{day_label}</td>\
<td align="right" valign="middle" style="font-family:{MONO}; font-size:11px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; color:#8C8C8C; line-height:1; white-space:nowrap;">\
<span style="color:#FFFFFF;">{min_read}</span>&nbsp;min&nbsp;read</td></tr></table></td></tr>"""


def _html_toc(palette, top_stories, other_news) -> str:
    rows: list[str] = []
    rows.append(
        f'<tr><td style="font-family:{MONO}; font-size:10px; font-weight:700; letter-spacing:0.18em; text-transform:uppercase; color:{palette["big_signal_accent"]}; line-height:1; padding-bottom:14px;">In&nbsp;this&nbsp;issue</td></tr>'
    )
    inner_rows: list[str] = []
    for idx, story in enumerate(top_stories):
        num_color = palette["big_signal_accent"] if idx == 0 else "#A0A0A0"
        big_signal_tag = (
            f' <span style="font-family:{MONO}; font-size:10px; font-weight:600; letter-spacing:0.08em; color:{palette["big_signal_accent"]}; padding-left:8px; text-transform:uppercase;">Big&nbsp;signal</span>'
            if idx == 0
            else ""
        )
        inner_rows.append(
            f'<tr><td valign="top" width="40" style="width:40px; font-family:{MONO}; font-size:11px; font-weight:700; color:{num_color}; letter-spacing:0.04em; padding:6px 12px 6px 0; line-height:1.4;">{idx+1:02d}</td>'
            f'<td valign="top" style="font-family:{SANS}; font-size:15px; font-weight:600; color:#FFFFFF; line-height:1.4; padding:6px 0;">{escape(story.get("headline",""))}{big_signal_tag}</td></tr>'
        )
        inner_rows.append('<tr><td colspan="2" style="border-top:1px solid #31222C; font-size:0; line-height:0;">&nbsp;</td></tr>')
    n = len(other_news)
    if n:
        plural = "stories" if n != 1 else "story"
        inner_rows.append(
            f'<tr><td valign="top" width="40" style="width:40px; font-family:{MONO}; font-size:11px; font-weight:700; color:#A0A0A0; letter-spacing:0.04em; padding:6px 12px 6px 0; line-height:1.4;">+</td>'
            f'<td valign="top" style="font-family:{SANS}; font-size:14px; font-weight:500; color:#BAB6B8; line-height:1.4; padding:6px 0;">{n}&nbsp;other&nbsp;news&nbsp;{plural}</td></tr>'
        )
    return (
        f'<tr><td bgcolor="{palette["ink"]}" style="background:{palette["ink"]}; padding:22px 28px 22px 28px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        + "".join(rows)
        + '<tr><td><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        + "".join(inner_rows)
        + "</table></td></tr></table></td></tr>"
    )


def _html_big_signal_card(palette, story) -> str:
    p = palette
    num_str = "01"
    headline = escape(story.get("headline", ""))
    summary = escape(story.get("summary", ""))
    track = escape(story.get("track", ""))
    filed_time = escape(
        _format_filed_from_published(story.get("published_at"))
        or story.get("filed_time", "")
    )
    dateline = escape(story.get("dateline", ""))
    hero_url = _safe_url(story.get("hero_image_url"))
    hero_html = ""
    if hero_url:
        hero_html = (
            f'<tr><td style="padding:18px 24px 0 24px;">'
            f'<a href="{escape(_safe_url(story.get("source_url")))}" style="text-decoration:none;">'
            f'<img src="{escape(hero_url)}" width="520" height="293" alt="{headline}" style="display:block; width:100%; max-width:520px; height:auto; border:0; border-radius:4px;"></a></td></tr>'
        )
    implications = story.get("implications") or []
    bullets_html = _render_implications_bullets(implications, p, on_dark_card=True)
    # Implications inset — only when the story has implications. A promoted/thin
    # bench story with none must not render an empty "IMPLICATIONS / ..." box.
    implications_html = (
        f'<tr><td style="padding:22px 24px 0 24px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="{p["white"]}" style="background:{p["white"]}; border-radius:6px;">'
        f'<tr><td style="padding:16px 22px 4px 22px;">'
        f'<div style="font-family:{MONO}; font-size:10px; font-weight:700; letter-spacing:0.16em; text-transform:uppercase; color:{p["primary"]}; line-height:1;">Implications&nbsp;/&nbsp;<span style="color:{p["ink"]};">{escape(p.get("implications_label", "For readers")).replace(" ", "&nbsp;")}</span></div></td></tr>'
        f'<tr><td style="padding:10px 22px 18px 22px;">{bullets_html}</td></tr></table></td></tr>'
    ) if implications else ""
    korean_takeaway_html = _render_korean_takeaway(story.get("korean_takeaway"), p, on_dark_card=True) if p.get("wants_korean") else ""
    # Meta strip: dateline only (the "Filed HH:MM CT" stamp was dropped — it was
    # inconsistent across stories and added noise).
    meta_inner = dateline
    # Gradient styles: gmail/apple mail render the linear-gradient; outlook
    # desktop strips it and falls back to the solid bgcolor attribute. Inner
    # cells that set their own bgcolor will overlay the gradient — that's
    # intentional for distinct elements (pills, numeral cell).
    gradient_end = p.get("big_signal_bg_gradient_end", p["big_signal_bg"])
    big_signal_bg_style = (
        f'background-color:{p["big_signal_bg"]}; '
        f'background-image:linear-gradient(135deg, {p["big_signal_bg"]} 0%, {gradient_end} 100%); '
        f'background:linear-gradient(135deg, {p["big_signal_bg"]} 0%, {gradient_end} 100%) {p["big_signal_bg"]};'
    )
    return (
        f'<tr><td bgcolor="{p["card_bg"]}" style="background:{p["card_bg"]}; padding:24px 16px 0 16px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="{p["big_signal_bg"]}" style="{big_signal_bg_style} border-radius:6px;">'
        # Numeral + tags row
        f'<tr><td style="padding:24px 24px 0 24px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
        f'<td valign="middle" width="90" align="left" style="padding:0; font-family:{ARIAL_BLACK}; font-size:46px; font-weight:900; color:{p["big_signal_accent"]}; line-height:1; letter-spacing:-0.02em;">{num_str}</td>'
        f'<td align="right" valign="middle"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td bgcolor="{p["big_signal_accent"]}" style="background:{p["big_signal_accent"]}; padding:6px 11px; border-radius:999px; font-family:{SANS}; font-size:10px; font-weight:800; letter-spacing:0.12em; text-transform:uppercase; color:{p["big_signal_bg"]}; line-height:1;">Big&nbsp;Signal</td>'
        f'<td bgcolor="{p["big_signal_bg"]}" style="width:6px; font-size:0; line-height:0; background:{p["big_signal_bg"]};">&nbsp;</td>'
        f'<td bgcolor="{p["big_signal_bg"]}" style="padding:6px 11px; border-radius:999px; border:1px solid {p["big_signal_pill_border"]}; background:{p["big_signal_bg"]}; font-family:{SANS}; font-size:10px; font-weight:700; letter-spacing:0.12em; text-transform:uppercase; color:#FFFFFF; line-height:1;">{track}</td>'
        f'</tr></table></td></tr></table></td></tr>'
        # Meta
        f'<tr><td style="padding:14px 24px 0 24px;">'
        f'<div style="font-family:{MONO}; font-size:10px; font-weight:600; letter-spacing:0.10em; text-transform:uppercase; color:{p["big_signal_meta_color"]}; line-height:1;">{meta_inner}</div></td></tr>'
        + hero_html
        # Headline — bold condensed display in the accent color (Half Baked DNA)
        + f'<tr><td style="padding:20px 24px 0 24px; font-family:{ARIAL_BLACK}; font-size:32px; font-weight:900; line-height:1.05; letter-spacing:-0.02em; text-transform:uppercase; color:{p["big_signal_accent"]};">{headline}</td></tr>'
        # Summary
        + f'<tr><td style="padding:14px 24px 0 24px; font-family:{SANS}; font-size:15px; line-height:1.6; color:{p["big_signal_body"]};">{summary}</td></tr>'
        + korean_takeaway_html
        # Implications white inset — only when present (see implications_html)
        + implications_html
        # Read CTA
        + f'<tr><td style="padding:20px 24px 24px 24px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        + f'<td style="padding-right:10px; font-family:{SANS}; font-size:13px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; line-height:1;">'
        + f'<a href="{escape(_safe_url(story.get("source_url")))}" style="color:{p["big_signal_accent"]}; text-decoration:none;">Read&nbsp;the&nbsp;full&nbsp;story</a></td>'
        + f'<td style="font-family:{MONO}; font-size:13px; font-weight:700; color:{p["big_signal_accent"]}; line-height:1;">&mdash;&mdash;&mdash;&nbsp;&rarr;</td>'
        + "</tr></table></td></tr></table></td></tr>"
    )


def _html_standard_card(palette, story, idx) -> str:
    """Standard card for Stories 02 + 03 — white background, magenta numeral,
    sans headline, pink-tint Implications block. Same shape as Big Signal
    but lighter visual weight."""
    p = palette
    num_str = f"{idx+1:02d}"
    headline = escape(story.get("headline", ""))
    summary = escape(story.get("summary", ""))
    track = escape(story.get("track", ""))
    filed_time = escape(
        _format_filed_from_published(story.get("published_at"))
        or story.get("filed_time", "")
    )
    dateline = escape(story.get("dateline", ""))
    # Standard stories (02, 03) are text-only by design — only the Big Signal
    # (Story 01) is illustrated, so the issue reads consistently instead of a
    # hero appearing on some lower stories but not others.
    hero_html = ""
    implications = story.get("implications") or []
    bullets_html = _render_implications_bullets(implications, p, on_dark_card=False)
    # Implications inset — only when the story has implications (see big-signal card).
    implications_html = (
        f'<tr><td style="padding:22px 24px 0 24px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="{p["implications_bg"]}" style="background:{p["implications_bg"]}; border-left:3px solid {p["implications_border"]}; border-radius:4px;">'
        f'<tr><td style="padding:14px 18px 6px 18px;">'
        f'<div style="font-family:{MONO}; font-size:10px; font-weight:700; letter-spacing:0.16em; text-transform:uppercase; color:{p["primary"]}; line-height:1;">Implications&nbsp;/&nbsp;<span style="color:{p["ink"]};">{escape(p.get("implications_label", "For readers")).replace(" ", "&nbsp;")}</span></div></td></tr>'
        f'<tr><td style="padding:6px 18px 16px 18px;">{bullets_html}</td></tr></table></td></tr>'
    ) if implications else ""
    korean_takeaway_html = _render_korean_takeaway(story.get("korean_takeaway"), p, on_dark_card=False) if p.get("wants_korean") else ""
    # Meta strip: dateline only (Filed timestamp dropped).
    meta_inner = f'<span style="color:{p["ink_muted"]};">{dateline}</span>'
    return (
        f'<tr><td bgcolor="{p["card_bg"]}" style="background:{p["card_bg"]}; padding:24px 16px 0 16px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="{p["white"]}" style="background:{p["white"]}; border:1px solid {p["card_border"]}; border-radius:6px;">'
        f'<tr><td style="padding:22px 24px 0 24px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
        f'<td valign="middle" bgcolor="{p["white"]}" width="90" align="left" style="background:{p["white"]}; padding:0; font-family:{ARIAL_BLACK}; font-size:46px; font-weight:900; color:{p["primary"]}; line-height:1; letter-spacing:-0.02em;">{num_str}</td>'
        f'<td align="right" valign="middle"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td bgcolor="{p["story_pill_bg"]}" style="background:{p["story_pill_bg"]}; padding:6px 11px; border-radius:999px; font-family:{SANS}; font-size:10px; font-weight:800; letter-spacing:0.12em; text-transform:uppercase; color:{p["story_pill_fg"]}; line-height:1;">Story</td>'
        f'<td bgcolor="{p["white"]}" style="width:6px; font-size:0; line-height:0; background:{p["white"]};">&nbsp;</td>'
        f'<td bgcolor="{p["track_tag_bg"]}" style="background:{p["track_tag_bg"]}; padding:6px 11px; border-radius:999px; font-family:{SANS}; font-size:10px; font-weight:800; letter-spacing:0.12em; text-transform:uppercase; color:{p["track_tag_fg"]}; line-height:1;">{track}</td>'
        f'</tr></table></td></tr></table></td></tr>'
        f'<tr><td style="padding:12px 24px 0 24px;"><div style="font-family:{MONO}; font-size:10px; font-weight:600; letter-spacing:0.10em; text-transform:uppercase; color:{p["ink_muted"]}; line-height:1;">{meta_inner}</div></td></tr>'
        + hero_html
        + f'<tr><td style="padding:20px 24px 0 24px; font-family:{ARIAL_BLACK}; font-size:26px; font-weight:900; letter-spacing:-0.02em; line-height:1.08; text-transform:uppercase; color:{p["ink"]};">{headline}</td></tr>'
        + f'<tr><td style="padding:12px 24px 0 24px; font-family:{SANS}; font-size:15px; line-height:1.6; color:{p["ink"]};">{summary}</td></tr>'
        + korean_takeaway_html
        + implications_html
        + f'<tr><td style="padding:18px 24px 24px 24px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        + f'<td style="padding-right:10px; font-family:{SANS}; font-size:13px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; line-height:1;">'
        + f'<a href="{escape(_safe_url(story.get("source_url")))}" style="color:{p["primary"]}; text-decoration:none;">Read&nbsp;the&nbsp;full&nbsp;story</a></td>'
        + f'<td style="font-family:{MONO}; font-size:13px; font-weight:700; color:{p["primary"]}; line-height:1;">&mdash;&mdash;&mdash;&nbsp;&rarr;</td>'
        + "</tr></table></td></tr></table></td></tr>"
    )


def _render_implications_bullets(implications, palette, *, on_dark_card: bool) -> str:
    """Render the 2 EN (and optional 1 KO) bullets."""
    p = palette
    rows: list[str] = []
    text_color = p["ink"]
    for bullet in implications:
        if not isinstance(bullet, str):
            continue
        is_korean = any(0xAC00 <= ord(ch) <= 0xD7A3 for ch in bullet)
        marker = "→"
        bullet_color = p["primary"]
        rows.append(
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
            f'<td valign="top" width="22" style="width:22px; padding:8px 8px 0 0; font-family:{MONO}; font-size:11px; font-weight:700; color:{bullet_color}; line-height:1.4;">{marker}</td>'
            f'<td valign="top" style="padding:6px 0 0 0; font-family:{SANS}; font-size:14px; font-weight:500; line-height:1.55; color:{text_color};">{escape(bullet)}</td>'
            f'</tr></table>'
        )
    return "".join(rows)


def _render_korean_takeaway(takeaway: str | None, palette, *, on_dark_card: bool) -> str:
    """Render the bilingual Korean 핵심 요약 block (Ledger + Pulse only).

    on_dark_card=True  → Big Signal context, dark bg → use light body color
    on_dark_card=False → standard card on white → use dark ink color
    """
    if not takeaway:
        return ""
    p = palette
    # Body color must contrast with the card it sits on. On Big Signal we use
    # pure white; on white standard cards we use ink. Earlier attempts at a
    # Korean-specific font stack (Apple SD Gothic Neo / Malgun Gothic / Nanum
    # Gothic) broke because double-quoted font names inside double-quoted
    # style attributes terminated the style early — color never applied,
    # appearing as low-contrast inherited text. Reverted to system SANS;
    # OS-default Korean font picks up via fallback (same as the rest of the
    # email's Korean handling).
    body_color = "#FFFFFF" if on_dark_card else p["ink"]
    return (
        f'<tr><td style="padding:14px 24px 0 24px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-left:3px solid {p["big_signal_accent"]};">'
        f'<tr><td style="padding:8px 0 8px 14px;">'
        f'<div style="font-family:{MONO}; font-size:10px; font-weight:700; letter-spacing:0.16em; text-transform:uppercase; color:{p["big_signal_accent"]}; line-height:1; padding-bottom:6px;">핵심&nbsp;요약 / Key Takeaway</div>'
        f'<div style="font-family:{SANS}; font-size:14px; font-weight:500; line-height:1.55; color:{body_color};">{escape(takeaway)}</div>'
        f'</td></tr></table></td></tr>'
    )


def _html_other_news(palette, items: list[dict[str, Any]]) -> str:
    """Render the 'Other news' section: up to 5 lightweight cards with
    track tag + headline + subtitle + summary. Each item is a small block
    inside one white card, separated by hairlines."""
    p = palette
    rows: list[str] = ['<tr><td style="border-top:1px solid '+p["ink_hairline"]+'; font-size:0; line-height:0;">&nbsp;</td></tr>']
    for idx, item in enumerate(items):
        num_str = f"{idx+1:02d}"
        track = escape(item.get("track", ""))
        headline = escape(item.get("headline", ""))
        subtitle = escape(item.get("subtitle", ""))
        summary = escape(item.get("summary", ""))
        source_url = escape(_safe_url(item.get("source_url")))
        headline_html = (
            f'<a href="{source_url}" style="color:{p["ink"]}; text-decoration:none;">{headline}</a>'
            if source_url else headline
        )
        rows.append(
            # Item row — number / track-pill stack on left, content on right
            f'<tr><td style="padding:14px 0 14px 0;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
            # Left column: number
            f'<td valign="top" width="40" style="width:40px; padding-right:12px; font-family:{MONO}; font-size:11px; font-weight:700; color:{p["primary"]}; letter-spacing:0.04em; line-height:1.4; padding-top:4px;">{num_str}</td>'
            # Right column: track pill + headline + subtitle stacked (no summary)
            f'<td valign="top" style="font-family:{SANS}; color:{p["ink"]};">'
            # Track pill
            f'<div style="margin-bottom:6px;"><span style="display:inline-block; background:{p["track_tag_bg"]}; color:{p["track_tag_fg"]}; font-family:{SANS}; font-size:10px; font-weight:800; letter-spacing:0.12em; text-transform:uppercase; padding:5px 10px; border-radius:999px; line-height:1;">{track}</span></div>'
            # Headline (sans, bold, link) — clamped to ONE line (ellipsis if long)
            f'<div style="font-family:{SANS}; font-size:17px; font-weight:800; letter-spacing:-0.015em; line-height:1.25; color:{p["ink"]}; margin-bottom:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{headline_html}</div>'
            # Subtitle (sans, medium, muted) — clamped to ONE line (ellipsis if long)
            f'<div style="font-family:{SANS}; font-size:13px; font-weight:500; line-height:1.4; color:{p["ink_muted"]}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{subtitle}</div>'
            f'</td></tr></table></td></tr>'
            # Hairline separator
            f'<tr><td style="border-top:1px solid {p["ink_hairline"]}; font-size:0; line-height:0;">&nbsp;</td></tr>'
        )
    return (
        f'<tr><td bgcolor="{p["card_bg"]}" style="background:{p["card_bg"]}; padding:24px 16px 0 16px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="{p["white"]}" style="background:{p["white"]}; border:1px solid {p["card_border"]}; border-radius:6px;">'
        # Section header
        f'<tr><td style="padding:22px 24px 8px 24px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
        f'<td valign="middle" style="font-family:{SANS}; font-size:20px; font-weight:900; letter-spacing:-0.02em; color:{p["ink"]}; line-height:1;">Other news</td>'
        f'<td align="right" valign="middle" style="font-family:{MONO}; font-size:10px; font-weight:600; letter-spacing:0.10em; text-transform:uppercase; color:{p["ink_muted"]}; line-height:1; white-space:nowrap;">{len(items)}&nbsp;{"story" if len(items) == 1 else "stories"}</td>'
        f'</tr></table></td></tr>'
        # Item rows
        f'<tr><td style="padding:0 24px 12px 24px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        + "".join(rows) +
        "</table></td></tr></table></td></tr>"
    )


def _html_footer(palette, issue_str, meta, *, include_reply_footer=True) -> str:
    p = palette
    feedback_subject = f"Feedback%20%E2%80%94%20{p['feedback_subject_token']}%20%E2%84%96%20{issue_str}"
    tagline = p.get("footer_tagline", f"Curated for the {p['category_main']} team.")
    # include_reply_footer == "sending mode": show the reply line + the
    # "internal use only" notice. Demo/public (GitHub) render omits both.
    reply_row = (
        f'<tr><td style="padding:18px 24px 0 24px; font-family:{SANS}; font-size:13px; font-weight:500; line-height:1.55; color:#D4CACD;">Reply to this email with feedback &mdash; <a href="mailto:{FEEDBACK_EMAIL}?subject={feedback_subject}" style="color:{p["footer_brand_main"]}; text-decoration:underline;">{FEEDBACK_EMAIL}</a></td></tr>'
        if include_reply_footer else ""
    )
    internal_label = "Internal&nbsp;use&nbsp;only&nbsp;&middot;&nbsp;Not&nbsp;for&nbsp;redistribution" if include_reply_footer else ""
    gradient_end = p.get("big_signal_bg_gradient_end", p["big_signal_bg"])
    footer_bg_style = (
        f'background-color:{p["big_signal_bg"]}; '
        f'background-image:linear-gradient(135deg, {p["big_signal_bg"]} 0%, {gradient_end} 100%); '
        f'background:linear-gradient(135deg, {p["big_signal_bg"]} 0%, {gradient_end} 100%) {p["big_signal_bg"]};'
    )
    return (
        f'<tr><td bgcolor="{p["card_bg"]}" style="background:{p["card_bg"]}; padding:24px 16px 24px 16px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="{p["big_signal_bg"]}" style="{footer_bg_style} border-radius:6px;">'
        f'<tr><td style="padding:28px 24px 12px 24px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
        f'<td valign="middle" style="vertical-align:middle; font-family:{SANS}; font-size:13px; font-weight:700; letter-spacing:0.12em; text-transform:uppercase; color:{p["footer_brand_main"]}; line-height:1;">{escape(p["category_main"])}&nbsp;<span style="color:{p["footer_pipe"]};">/</span>&nbsp;<span style="color:{p["footer_brand_sub"]};">{escape(p["category_sub"])}</span></td>'
        f'<td align="right" valign="middle" style="font-family:{MONO}; font-size:10px; font-weight:600; letter-spacing:0.06em; text-transform:uppercase; color:#A89398; line-height:1; white-space:nowrap;">No.&nbsp;{issue_str}</td>'
        f'</tr></table></td></tr>'
        f'<tr><td style="padding:18px 24px 0 24px;"><div style="font-family:{SANS}; font-size:34px; font-weight:900; letter-spacing:-0.03em; line-height:1; color:#FFFFFF;">{escape(p["name"])}<span style="color:{p["footer_wordmark_period"]};">.</span></div></td></tr>'
        f'<tr><td style="padding:14px 24px 0 24px; font-family:{SANS}; font-size:14px; font-weight:500; line-height:1.55; color:#D4CACD;">{escape(tagline)}</td></tr>'
        f'{reply_row}'
        f'<tr><td style="padding:14px 24px 24px 24px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
        f'<td valign="middle" style="font-family:{MONO}; font-size:10px; font-weight:600; letter-spacing:0.08em; text-transform:uppercase; color:#A89398; line-height:1.5;">{internal_label}</td>'
        f'<td align="right" valign="middle" style="font-family:{MONO}; font-size:10px; font-weight:600; letter-spacing:0.08em; text-transform:uppercase; color:#A89398; line-height:1.5;">{escape(meta["date_dd_mm_yy"])}</td>'
        f'</tr></table></td></tr></table></td></tr>'
    )


# --- Plaintext fallback ----------------------------------------------------

def _build_plaintext(palette, top_stories, other_news, meta, editor_concerns=None, *, include_reply_footer=True) -> str:
    issue_str = f"{meta['issue_number']:03d}"
    out: list[str] = []
    # Sending-mode artifact only — omitted from the demo/public render.
    if include_reply_footer and editor_concerns and editor_concerns.get("must_fix"):
        out.append("⚠ SR. EDITOR ADVISORY — DELETE BEFORE SENDING ⚠")
        out.append(f"Verdict: {editor_concerns.get('verdict','')}")
        out.append(f"Concerns ({len(editor_concerns['must_fix'])}):")
        for item in editor_concerns["must_fix"]:
            out.append(f"  • {item}")
        if editor_concerns.get("notes"):
            out.append(f"Notes: {editor_concerns['notes']}")
        out.append("")
        out.append("─" * 60)
        out.append("")
    out.append(f"{palette['name']} · № {issue_str} · {meta['date_dd_mm_yy']}")
    out.append(f"{palette['category_main']} / {palette.get('category_sub', 'Intel')}")
    out.append("")
    for idx, story in enumerate(top_stories):
        label = " [BIG SIGNAL]" if idx == 0 else ""
        out.append(f"{idx+1:02d}{label}  {story.get('headline','')}")
        _meta_bits = [story.get("track", "")]
        if story.get("dateline"):
            _meta_bits.append(story["dateline"])
        out.append(f"     {' · '.join(b for b in _meta_bits if b)}")
        out.append(f"     {story.get('summary','')}")
        # `or []` (not a default arg): a story with implications=null must not
        # crash the plaintext build — matches the HTML cards. A promoted thin
        # bench story legitimately can carry no implications.
        for b in story.get("implications") or []:
            out.append(f"     → {b}")
        _surl = _safe_url(story.get("source_url"))
        if _surl:
            out.append(f"     ↳ {_surl}")
        out.append("")
    if other_news:
        out.append("OTHER NEWS")
        for idx, item in enumerate(other_news):
            out.append(f"  {idx+1:02d}  [{item.get('track','')}]  {item.get('headline','')}")
            if item.get("subtitle"):
                out.append(f"        {item.get('subtitle','')}")
            _surl = _safe_url(item.get("source_url"))
            if _surl:
                out.append(f"        ↳ {_surl}")
            out.append("")
    if include_reply_footer:
        out.append(f"Reply to this email with feedback — {FEEDBACK_EMAIL}")
        out.append(f"Internal use only · Not for redistribution · {meta['date_dd_mm_yy']}")
    else:
        out.append(meta['date_dd_mm_yy'])
    return "\n".join(out)
