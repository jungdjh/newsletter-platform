"""Shared card engine — text/branding baked onto an image via Pillow, one code
path, layout selected by a profile. Two products reuse it:

- `newsletter-hero` (1200x1440): the Card News "A" treatment for Story 01.
  Mail clients can't render text-over-photo HTML reliably, so the A card ships
  as ONE baked image (design-spec-v7 `.a-hero`): cover-cropped photo → scrim
  gradient → kicker → highlight-box title → summary → 2 bullets → Korean box →
  pill CTA → source line. Canvas is @2x for the 600x720 email slot, exported as
  a progressive JPEG capped at 300 KB. The plaintext body and the <img alt>
  carry the content for accessibility.
  The '01 / BIG SIGNAL' numeral + chip are rendered as HTML by the card wrapper
  (render_html._cn_composed_hero_card), NOT baked here — so the story numbering
  survives even when the inbox blocks or fails to load this image.

- `social-card` (1536x1024): Footnote social framing (first cut) on a genmedia
  diary-doodle. The doodle already carries the joke — its recurring character +
  2-3 colored ink labels are baked BY THE IMAGE MODEL (see genmedia-style.md),
  NOT here; this profile only adds thin Footnote branding in the white margins.

Fonts (bundled, OFL — assets/fonts/): Arimo Bold (Arial-metric) is the
display/pill/wordmark face — it matches the HTML body's Helvetica/Arial stack so
the hero and the live-text cards read as one system in the inbox. Noto Sans KR
covers body Latin + Hangul (the Korean core-summary box needs real Hangul
glyphs).

Fail-open contract: compose_card()/compose_hero() raise on bad input — the
CALLER (run_newsletter._compose_lead_hero for the newsletter) wraps it and falls
back to the B split card.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = REPO_ROOT / "assets" / "fonts"

CANVAS_W, CANVAS_H = 1200, 1440   # @2x of the 600x720 slot
MAX_BYTES = 300_000

PAD_X = 56          # 28px @2x
PAD_BOTTOM = 52     # 26px @2x

# social-card canvas — match the locked genmedia generation size (1536x1024).
SOCIAL_W, SOCIAL_H = 1536, 1024
SOCIAL_PAD = 48
SOCIAL_MAX_BYTES = 5_000_000    # X's PNG post ceiling; doodles land far below

WHITE = (255, 255, 255)
BODY = (237, 237, 237)      # #EDEDED
GRAY = (185, 185, 185)      # srcline on photo
LABEL_GRAY = (154, 154, 154)
INK = (10, 10, 10)
SOCIAL_MARK = (90, 90, 90)  # recessive wordmark on the doodle's white

# Scrim stops (fraction-from-bottom, alpha) — design-spec-v7 .a-hero .scrim.
_SCRIM_STOPS = [(0.00, 0.95), (0.38, 0.80), (0.55, 0.35), (0.72, 0.02), (1.00, 0.25)]

_FONT_CACHE: dict = {}


@dataclass(frozen=True)
class LayoutProfile:
    """Canvas-level knobs for one baked-card layout. The fine-grained geometry
    of each layout (line heights, per-block gaps) lives inside its render
    function — it is that layout's private shape, not a shared parameter."""
    name: str
    canvas_w: int
    canvas_h: int
    max_bytes: int
    pad_x: int
    pad_bottom: int
    render: Callable
    export: str = "jpeg"            # "jpeg" (progressive, byte-capped) | "png"
    scrim_stops: tuple | list | None = None


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(str(FONT_DIR / name), size)
    return _FONT_CACHE[key]


def _hex_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _cover_crop(img: Image.Image, w: int, h: int, focus_y: float = 0.60) -> Image.Image:
    """Scale-to-cover then crop, biasing the vertical window toward focus_y
    (spec: object-position 50% 60%)."""
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * scale)), max(h, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = min(max(round(nh * focus_y - h / 2), 0), nh - h)
    return img.crop((left, top, left + w, top + h))


def _fit_on_white(img: Image.Image, w: int, h: int) -> Image.Image:
    """Contain-fit the doodle inside w×h centered on pure white — never crop:
    the doodle's character + labels must stay whole (unlike the newsletter's
    cover-crop, which is fed full-bleed photos)."""
    scale = min(w / img.width, h / img.height)
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), WHITE)
    canvas.paste(img, ((w - nw) // 2, (h - nh) // 2))
    return canvas


def _scrim_alpha(frac_from_bottom: float, stops) -> float:
    for (f0, a0), (f1, a1) in zip(stops, stops[1:]):
        if f0 <= frac_from_bottom <= f1:
            t = 0 if f1 == f0 else (frac_from_bottom - f0) / (f1 - f0)
            return a0 + (a1 - a0) * t
    return stops[-1][1]


def _apply_scrim(img: Image.Image, stops) -> Image.Image:
    mask = Image.new("L", (1, img.height))
    px = mask.load()
    for y in range(img.height):
        px[0, y] = round(255 * _scrim_alpha(1 - y / (img.height - 1), stops))
    mask = mask.resize(img.size)
    black = Image.new("RGB", img.size, (0, 0, 0))
    return Image.composite(black, img, mask)


def _draw_tracked(draw, xy, text: str, font, fill, tracking: int) -> int:
    """Letter-spaced text (Pillow has no native tracking). Returns end x."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return round(x)


def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> list:
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if draw.textlength(cand, font=font) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines - 1:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    leftover = len(words) > sum(len(l.split()) for l in lines)
    if leftover and lines:
        lines[-1] = lines[-1].rstrip(".,;") + "…"
    return lines


def _korean_lines(takeaway) -> list:
    if isinstance(takeaway, list):
        lines = [str(t).strip() for t in takeaway if str(t).strip()]
    else:
        lines = [l.strip() for l in str(takeaway or "").splitlines() if l.strip()]
    return lines[:2]


def _export(image: Image.Image, profile: "LayoutProfile") -> bytes:
    """Serialize under the profile's byte cap. `png` layouts (crisp line art)
    try lossless PNG first and fall back to a byte-capped progressive JPEG only
    if the PNG blows the cap; `jpeg` layouts run the progressive quality-loop."""
    if profile.export == "png":
        buf = io.BytesIO()
        image.save(buf, "PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= profile.max_bytes:
            return data
    for quality in range(82, 44, -6):
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=quality, progressive=True, optimize=True)
        data = buf.getvalue()
        if len(data) <= profile.max_bytes:
            break
    return data


def _render_newsletter_hero(
    profile: "LayoutProfile",
    image_bytes: bytes,
    story: dict,
    *,
    accent: str,
    accent_pale: str,
    source_label: str,
) -> Image.Image:
    """Bake the v7 'A' overlay onto a hero photo; returns the composed RGB image
    (compose_card handles the byte-capped export)."""
    CANVAS_W, CANVAS_H = profile.canvas_w, profile.canvas_h
    PAD_X, PAD_BOTTOM = profile.pad_x, profile.pad_bottom
    CONTENT_W = CANVAS_W - 2 * PAD_X

    teal = _hex_rgb(accent)
    teal_pale = _hex_rgb(accent_pale)

    photo = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    canvas = _apply_scrim(_cover_crop(photo, CANVAS_W, CANVAS_H), profile.scrim_stops)
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    f_display = _font("Arimo-Bold.ttf", 60)
    f_pill = _font("Arimo-Bold.ttf", 25)
    f_body = _font("NotoSansKR-Regular.otf", 28)
    f_bullet = _font("NotoSansKR-Regular.otf", 26)
    f_kr = _font("NotoSansKR-Regular.otf", 26)
    f_caps = _font("NotoSansKR-Bold.otf", 22)
    f_small = _font("NotoSansKR-Bold.otf", 21)

    headline = (story.get("headline") or "").strip()
    summary = (story.get("summary") or "").strip()
    bullets = [b.strip() for b in (story.get("implications") or []) if isinstance(b, str) and b.strip()][:2]
    kr_lines = _korean_lines(story.get("korean_takeaway"))
    kicker = (story.get("kicker") or story.get("track") or "").strip().upper()

    # ---- measure the bottom overlay stack (spec paddings x2) ----
    title_lines = _wrap(draw, headline, f_display, CONTENT_W - 40, 3)
    title_line_h, title_gap, title_pad_x, title_pad_y = 80, 10, 20, 8
    title_h = len(title_lines) * (title_line_h + title_gap) - title_gap

    sum_lines = _wrap(draw, summary, f_body, CONTENT_W, 3)
    sum_lh = 46
    sum_h = len(sum_lines) * sum_lh

    bullet_lh, bullet_gap = 40, 14
    bullets_h = (len(bullets) * (bullet_lh + bullet_gap) - bullet_gap) if bullets else 0

    kr_pad_y, kr_label_h, kr_label_gap, kr_lh = 24, 30, 12, 46
    kr_h = (kr_pad_y * 2 + kr_label_h + kr_label_gap + len(kr_lines) * kr_lh) if kr_lines else 0

    pill_h = 66
    src_h = 30 if source_label else 0

    g_kicker, g_title, g_sum, g_imps, g_kr, g_cta, g_src = 22, 28, 36, 28, 32, 40, 32
    kicker_h = 32 if kicker else 0

    total = (kicker_h + (g_kicker if kicker else 0) + title_h
             + (g_sum + sum_h if sum_lines else 0)
             + (g_imps + bullets_h if bullets else 0)
             + (g_kr + kr_h if kr_lines else 0)
             + g_cta + pill_h
             + (g_src + src_h if source_label else 0))

    y = CANVAS_H - PAD_BOTTOM - total

    # ---- kicker ----
    if kicker:
        _draw_tracked(draw, (PAD_X, y), kicker, f_caps, teal_pale + (255,), 4)
        y += kicker_h + g_kicker

    # ---- title: one teal highlight box per line (box-decoration-break: clone) ----
    for line in title_lines:
        tw = draw.textlength(line, font=f_display)
        draw.rectangle([PAD_X, y, PAD_X + tw + 2 * title_pad_x, y + title_line_h],
                       fill=teal + (255,))
        draw.text((PAD_X + title_pad_x, y + title_pad_y - 4), line, font=f_display, fill=WHITE + (255,))
        y += title_line_h + title_gap
    y -= title_gap

    # ---- summary ----
    if sum_lines:
        y += g_sum
        for line in sum_lines:
            draw.text((PAD_X, y + (sum_lh - 34) // 2), line, font=f_body, fill=BODY + (255,))
            y += sum_lh

    # ---- bullets ----
    if bullets:
        y += g_imps
        for i, b in enumerate(bullets):
            draw.text((PAD_X, y), "→", font=f_bullet, fill=teal + (255,))
            btxt = _wrap(draw, b, f_bullet, CONTENT_W - 44, 1)[0]
            draw.text((PAD_X + 44, y), btxt, font=f_bullet, fill=BODY + (255,))
            y += bullet_lh + (bullet_gap if i < len(bullets) - 1 else 0)

    # ---- Korean box (semi-transparent, teal left bar) ----
    if kr_lines:
        y += g_kr
        draw.rectangle([PAD_X, y, PAD_X + CONTENT_W, y + kr_h], fill=(10, 10, 10, 153))
        draw.rectangle([PAD_X, y, PAD_X + 6, y + kr_h], fill=teal + (255,))
        ky = y + kr_pad_y
        _draw_tracked(draw, (PAD_X + 32, ky), "핵심 요약 / KEY TAKEAWAY", f_small, LABEL_GRAY + (255,), 3)
        ky += kr_label_h + kr_label_gap
        for line in kr_lines:
            draw.text((PAD_X + 32, ky + (kr_lh - 38) // 2), line, font=f_kr, fill=teal_pale + (255,))
            ky += kr_lh
        y += kr_h

    # ---- pill CTA ----
    y += g_cta
    pill_text = "Read the full story  →"
    pw = draw.textlength(pill_text, font=f_pill) + 2 * 36
    draw.rounded_rectangle([PAD_X, y, PAD_X + pw, y + pill_h], radius=pill_h // 2, fill=WHITE + (255,))
    draw.text((PAD_X + 36, y + (pill_h - 34) // 2), pill_text, font=f_pill, fill=INK + (255,))
    y += pill_h

    # ---- source line ----
    if source_label:
        y += g_src
        _draw_tracked(draw, (PAD_X, y), f"SOURCE · {source_label}".upper(), f_small, GRAY + (255,), 2)

    # The '01 / BIG SIGNAL' numeral + chip are rendered as HTML by the card
    # wrapper (render_html._cn_composed_hero_card), so they survive image-blocking
    # — not baked here.

    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def _render_social_card(
    profile: "LayoutProfile",
    image_bytes: bytes,
    content: dict,
    *,
    accent: str,
    accent_pale: str,
    source_label: str,
) -> Image.Image:
    """Footnote social framing (FIRST CUT — design draft) on a genmedia
    diary-doodle. The doodle owns the joke (character + colored ink labels baked
    by the image model); this only adds thin branding in the white margins:
    a small @thefootnotery wordmark (bottom-right), an optional pack numbering
    chip (top-left, only in a 4-card pack) + an optional caption/kicker (top-left).
    accent/accent_pale/source_label are accepted for a uniform renderer signature
    but unused in the first cut (Footnote brand color TBD)."""
    W, H = profile.canvas_w, profile.canvas_h
    pad = profile.pad_x

    doodle = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    canvas = _fit_on_white(doodle, W, H)
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    f_mark = _font("Arimo-Bold.ttf", 30)
    f_caption = _font("Arimo-Bold.ttf", 26)

    # ---- pack numbering chip (top-left): "01 / 04" — the same unified numbering
    # language as the newsletter cards, set in the doodle's white top-left margin
    # (clear of the model-baked character + colored labels). The current beat is
    # crisp INK, the total recessive gray. Rendered ONLY inside a pack
    # (content["pack_number"] set, 1..pack_total); a standalone card omits it, so
    # the locked handle-only layout is byte-for-byte unchanged. ----
    cap_top = pad
    pack_number = content.get("pack_number")
    if pack_number:
        f_chip = _font("Arimo-Bold.ttf", 34)
        pack_total = int(content.get("pack_total") or 4)
        endx = _draw_tracked(draw, (pad, pad), f"{int(pack_number):02d}", f_chip, INK + (255,), 1)
        _draw_tracked(draw, (endx, pad), f" / {pack_total:02d}", f_chip, SOCIAL_MARK + (255,), 1)
        cap_top = pad + 48

    # ---- optional caption/kicker (top-left, tracked caps, ≤2 lines) ----
    caption = str(content.get("caption") or content.get("kicker") or "").strip().upper()
    if caption:
        cy, cap_lh = cap_top, 40
        for line in _wrap(draw, caption, f_caption, W - 2 * pad, 2):
            _draw_tracked(draw, (pad, cy), line, f_caption, INK + (255,), 2)
            cy += cap_lh

    # ---- @thefootnotery wordmark (bottom-right, recessive gray) ----
    handle = str(content.get("handle") or "@thefootnotery").strip()
    track = 2
    hw = sum(draw.textlength(c, font=f_mark) for c in handle) + track * (len(handle) - 1)
    hx = W - pad - hw
    hy = H - pad - 32
    _draw_tracked(draw, (hx, hy), handle, f_mark, SOCIAL_MARK + (255,), track)

    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


NEWSLETTER_HERO = LayoutProfile(
    name="newsletter-hero",
    canvas_w=CANVAS_W, canvas_h=CANVAS_H, max_bytes=MAX_BYTES,
    pad_x=PAD_X, pad_bottom=PAD_BOTTOM,
    scrim_stops=_SCRIM_STOPS, export="jpeg",
    render=_render_newsletter_hero,
)

SOCIAL_CARD = LayoutProfile(
    name="social-card",
    canvas_w=SOCIAL_W, canvas_h=SOCIAL_H, max_bytes=SOCIAL_MAX_BYTES,
    pad_x=SOCIAL_PAD, pad_bottom=SOCIAL_PAD,
    scrim_stops=None, export="png",
    render=_render_social_card,
)

PROFILES: dict = {p.name: p for p in (NEWSLETTER_HERO, SOCIAL_CARD)}


# --- branded field: the no-photo fallback -----------------------------------
# David's locked rule (2026-07-12) is that Story 01 always leads with a visual.
# But the place guard + fit-check correctly reject a lot of stock ("a wrong
# photo is worse than no photo"), which used to leave the lead text-only. A
# branded field is the third option: it always *belongs* — no geography to get
# wrong, no licence, no relevance to misjudge — and because it feeds the SAME
# newsletter-hero renderer, the typography and Arial-metric face match the HTML
# body for free. See Handoff/newsletter-image-strategy (Track B).

def brand_field_bytes(accent: str = "#0EA5A5", *, w: int = CANVAS_W, h: int = CANVAS_H) -> bytes:
    """A vertical accent→near-black gradient sized for the hero slot.

    Deliberately dark: the hero renderer lays its own scrim over this and then
    draws white text, so a light field would wash the headline out."""
    top = _hex_rgb(accent)
    # Deepen the accent so the scrim doesn't have to fight it, and so two
    # different audiences' accents stay visually distinct.
    top = tuple(max(0, int(c * 0.55)) for c in top)
    bottom = (10, 12, 14)
    grad = Image.new("RGB", (1, h))
    px = grad.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    buf = io.BytesIO()
    grad.resize((w, h)).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def compose_branded(
    story: dict,
    *,
    accent: str = "#0EA5A5",
    accent_pale: str = "#D6F5F5",
    source_label: str = "",
    out_path=None,
) -> bytes:
    """Bake the Story-01 card onto a branded field instead of a photograph.

    Same layout, same fonts, same byte cap as the photo hero — only the
    background differs, so hero and body still read as one system."""
    return compose_card(
        brand_field_bytes(accent), story,
        profile="newsletter-hero",
        accent=accent, accent_pale=accent_pale,
        source_label=source_label, out_path=out_path)


# Wide branded field for the STANDARD (non-Card-News) lead slot. That template
# renders the lead photo at width="560" over the spec's 240px cover band, so
# generate at @2x of that slot. Nothing is baked in: the standard template draws
# the headline in HTML, so text on the image would print it twice. This is the
# non-Card-News half of Track B (Handoff/newsletter-image-strategy) — it gives
# each audience a field in ITS OWN accent instead of the one shared
# assets/hero/default.jpg that every audience would otherwise repeat.
FIELD_W, FIELD_H = 1120, 480


def compose_brand_field(accent: str = "#0EA5A5", *, out_path=None,
                        w: int = FIELD_W, h: int = FIELD_H) -> bytes:
    """A text-free branded field sized for the standard lead slot.

    Unlike compose_branded (which bakes the whole Story-01 card for Card News),
    this is the bare accent field. Deterministic for a given accent, so a
    re-run writes byte-identical output and never churns the repo."""
    data = brand_field_bytes(accent, w=w, h=h)
    if out_path is not None:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(data)
    return data


def compose_card(
    image_bytes: bytes,
    content: dict,
    *,
    profile="newsletter-hero",
    accent: str = "#0EA5A5",
    accent_pale: str = "#D6F5F5",
    source_label: str = "",
    out_path=None,
) -> bytes:
    """Bake a card via the selected layout `profile` (name or LayoutProfile).
    Returns serialized bytes under the profile's cap; also writes them to
    out_path when given. Raises on bad input — the caller treats any raise as
    'no composed card' and falls back."""
    prof = profile if isinstance(profile, LayoutProfile) else PROFILES[profile]
    image = prof.render(prof, image_bytes, content,
                        accent=accent, accent_pale=accent_pale, source_label=source_label)
    data = _export(image, prof)
    if out_path is not None:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(data)
    return data


def compose_hero(
    image_bytes: bytes,
    story: dict,
    *,
    accent: str = "#0EA5A5",
    accent_pale: str = "#D6F5F5",
    source_label: str = "",
    out_path=None,
) -> bytes:
    """Back-compat wrapper for the newsletter caller: the `newsletter-hero`
    profile. Returns progressive-JPEG bytes (<= MAX_BYTES)."""
    return compose_card(
        image_bytes, story,
        profile="newsletter-hero",
        accent=accent, accent_pale=accent_pale,
        source_label=source_label, out_path=out_path,
    )
