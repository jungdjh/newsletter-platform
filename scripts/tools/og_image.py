"""Vision-check candidate hero images against the story headline.

Two entry points share one bounded download:
  - check_image_relevance() -> True/False/None : the boolean relevance gate the
    agent's `og_image_check` tool uses during generation (unchanged contract).
  - grade_image() -> structured dict : the Card News image router's vetting —
    {relevant, audience_ok, grade (hero|card|reject), reason, width, height},
    with a deterministic pixel-size floor/cap applied before the vision call.

The download is streamed and capped at MAX_IMAGE_BYTES, and — the Phase-1 yield
unlock — a MISSING Content-Length no longer auto-skips the candidate (many
publisher CDNs omit it; that skip was the single biggest reason platform issues
shipped imageless). The bounded read still can't overflow memory.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from typing import Optional

import anthropic
import requests

MAX_IMAGE_BYTES = 1_000_000  # 1 MB hard cap to avoid context overflow
VISION_MODEL = "claude-sonnet-4-5"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

# Deterministic dimension floors (Card News, ticket Step 2). Hero needs a wide
# landscape frame; card is the smaller 240px-band floor; below card = reject.
HERO_MIN_WIDTH = 1200
HERO_MIN_ASPECT = 1.3
CARD_MIN_WIDTH = 800
CARD_MIN_HEIGHT = 480
_GRADE_RANK = {"reject": 0, "card": 1, "hero": 2}

VISION_PROMPT_TEMPLATE = """\
You are vetting a hero image for a market-intelligence newsletter story.

Story headline: "{headline}"
Story track: {track}

Is this image directly and recognizably related to the headline?

APPROVE if the image clearly depicts:
- The specific company / product / person named in the headline
- A relevant UI screenshot, device photo, or branded scene
- A direct visual reference to the news event

REJECT if the image is:
- Generic stock photo (people in suits shaking hands, abstract tech imagery)
- A brand card / press kit logo card / floating-logo composition
- Off-topic incidental imagery (random office, abstract pattern)
- A watermarked publication thumbnail or stock-photo watermark
- A headshot of the article's author / journalist rather than the subject
- An "app icons floating in space" or generic device-lineup stock shot

Reply with EXACTLY one of:
APPROVE
REJECT
"""

GRADE_PROMPT_TEMPLATE = """\
You are vetting a candidate hero image for a newsletter story, for a specific
audience, and grading how it can be used.

Newsletter audience: {audience}
Story headline: "{headline}"
Story track: {track}
Image pixel size: {w}x{h} ({aspect}:1 aspect).

Judge three things:
1. relevant — does the image clearly relate to THIS story's headline/track? Not
   generic stock, not a logo / brand card, not the author's headshot, not off-topic.
2. audience_ok — appropriate for THIS audience? Reject graphic clinical/medical
   imagery (wounds, surgery, distressed patients), memes, watermarked stock, and
   anything off-tone for the audience described above.
3. grade:
   - "hero"  = a WIDE editorial scene, landscape, subject placed so there is clean
     headroom / empty space for a text overlay; NOT a tight close-up, NOT busy
     edge-to-edge, NOT text/logo-heavy.
   - "card"  = relevant and audience_ok, but a tighter, busier, or close-up shot
     that only works as a small photo band (not a full-bleed hero).
   - "reject" = irrelevant, off-audience, low quality, watermarked, or a logo/graphic.

Reply with ONLY a JSON object, no prose, no code fence:
{{"relevant": true, "audience_ok": true, "grade": "hero", "reason": "<=12 words"}}
"""


def _download_image_capped(
    image_url: str,
    image_size_bytes: Optional[int] = None,
    *,
    allowed_types=("image/jpeg", "image/png", "image/gif", "image/webp"),
):
    """Fetch an image with a hard byte cap. Returns (bytes, media_type) or None.

    A missing Content-Length no longer skips — we stream and stop at the cap, so
    CL-less candidates still get vetted while an oversized/uncapped body can't
    overflow memory. (Salvaged from PR #45's size-gate work.)"""
    # Size pre-check — never pull a known-oversized image.
    if image_size_bytes is not None:
        if image_size_bytes > MAX_IMAGE_BYTES:
            return None
    else:
        try:
            head = requests.head(image_url, headers=DEFAULT_HEADERS, timeout=8, allow_redirects=True)
            cl = head.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > MAX_IMAGE_BYTES:
                return None
            # No Content-Length: DON'T skip — fall through to the bounded GET.
        except requests.RequestException:
            return None

    try:
        with requests.get(image_url, headers=DEFAULT_HEADERS, timeout=15, stream=True) as resp:
            if resp.status_code >= 400:
                return None
            media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            if media_type not in allowed_types:
                return None
            buf = b""
            for chunk in resp.iter_content(8192):
                if not chunk:
                    continue
                buf += chunk
                if len(buf) > MAX_IMAGE_BYTES:
                    return None  # over the cap — skip
            if not buf:
                return None
            return buf, media_type
    except requests.RequestException:
        return None


def check_image_relevance(
    image_url: str,
    headline: str,
    *,
    track: str = "",
    image_size_bytes: Optional[int] = None,
) -> Optional[bool]:
    """Vision-check a hero candidate. Returns True (approve) / False (reject) /
    None (couldn't check). Contract unchanged — used by the agent's og_image_check
    tool; now benefits from the bounded download (no more CL-missing auto-skip)."""
    got = _download_image_capped(image_url, image_size_bytes)
    if got is None:
        return None
    image_bytes, media_type = got

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=6)
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    prompt = VISION_PROMPT_TEMPLATE.format(headline=headline, track=track or "(unspecified)")

    try:
        response = client.messages.create(
            model=VISION_MODEL,
            max_tokens=10,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        verdict = (response.content[0].text or "").strip().upper()
        if verdict.startswith("APPROVE"):
            return True
        if verdict.startswith("REJECT"):
            return False
        return False  # unparseable — conservative
    except anthropic.APIError:
        return None


def _image_dimensions(image_bytes: bytes) -> Optional[tuple]:
    """(width, height) via Pillow, or None if the bytes aren't a decodable image."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None


def _dimension_grade(w: int, h: int) -> str:
    """The best grade the PIXELS allow (a hard cap on the vision opinion)."""
    if w >= HERO_MIN_WIDTH and h > 0 and (w / h) >= HERO_MIN_ASPECT:
        return "hero"
    if w >= CARD_MIN_WIDTH and h >= CARD_MIN_HEIGHT:
        return "card"
    return "reject"


def _parse_grade_json(raw: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _reject(reason: str, w: int = 0, h: int = 0) -> dict:
    return {"relevant": False, "audience_ok": False, "grade": "reject",
            "reason": reason, "width": w, "height": h}


def grade_image(
    image_url: str,
    headline: str,
    *,
    track: str = "",
    audience: str = "",
    image_size_bytes: Optional[int] = None,
) -> dict:
    """Structured verdict for the Card News image router:
        {relevant, audience_ok, grade (hero|card|reject), reason, width, height}

    Deterministic pixel gate first (a too-small image can't be a hero regardless
    of what the model thinks; below the card floor is rejected without a vision
    call). Then one structured Sonnet call for relevance / audience / composition.
    The final grade is the more conservative of the pixel cap and the vision
    opinion. Fail-CLOSED to reject on any error, so the caller falls back to the
    curated library — never raises, never returns a half-verified 'use it'."""
    got = _download_image_capped(image_url, image_size_bytes,
                                 allowed_types=("image/jpeg", "image/png", "image/webp"))
    if got is None:
        return _reject("unfetchable / too large / unsupported type")
    image_bytes, media_type = got

    dims = _image_dimensions(image_bytes)
    if dims is None:
        return _reject("undecodable image bytes")
    w, h = dims

    dim_grade = _dimension_grade(w, h)
    if dim_grade == "reject":
        return _reject(f"below card floor ({w}x{h})", w, h)

    # Vision needs a key; without one, skip (caller falls back to the library).
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _reject("no ANTHROPIC_API_KEY for vision grading", w, h)

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=6)
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        aspect = round(w / h, 2) if h else 0
        prompt = GRADE_PROMPT_TEMPLATE.format(
            audience=audience or "(general audience)", headline=headline,
            track=track or "(unspecified)", w=w, h=h, aspect=aspect)
        response = client.messages.create(
            model=VISION_MODEL,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        verdict = _parse_grade_json((response.content[0].text or "").strip())
    except Exception:
        # Broad on purpose: grade_image promises it never raises (an empty/odd
        # response.content would IndexError past a narrow APIError catch). Any
        # vision failure fails CLOSED to reject → the router uses the library.
        return _reject("vision call failed", w, h)

    if verdict is None:
        return _reject("unparseable vision verdict", w, h)

    relevant = bool(verdict.get("relevant"))
    audience_ok = bool(verdict.get("audience_ok"))
    vgrade = verdict.get("grade") if verdict.get("grade") in ("hero", "card", "reject") else "reject"
    reason = str(verdict.get("reason", ""))[:120]

    if not relevant or not audience_ok:
        final = "reject"
    else:
        # Conservative: the lower of what the pixels allow and what the model saw.
        final = vgrade if _GRADE_RANK[vgrade] <= _GRADE_RANK[dim_grade] else dim_grade

    return {"relevant": relevant, "audience_ok": audience_ok, "grade": final,
            "reason": reason or f"{final} ({w}x{h})", "width": w, "height": h}
