"""Vision-check candidate hero images against the story headline.

Implements the SKILL.md Step 6 rule: reject generic stock, brand cards,
watermarked thumbnails, off-topic content, and any image larger than 1 MB.

Returns True (approve), False (reject), or None (skip — couldn't check).
"""

from __future__ import annotations

import base64
import os
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


def check_image_relevance(
    image_url: str,
    headline: str,
    *,
    track: str = "",
    image_size_bytes: Optional[int] = None,
) -> Optional[bool]:
    """Vision-check a hero candidate.

    Args:
        image_url: URL of the candidate image.
        headline: The story headline.
        track: Optional track tag (e.g. "Payments", "Apps") for context.
        image_size_bytes: Pre-fetched Content-Length if known. If >1MB or None
            and HEAD fails, the image is skipped (returns None).

    Returns:
        True if the image is relevant (approve).
        False if the image is off-topic / stock / brand card (reject).
        None if the image couldn't be checked (too large, fetch failed, etc.).
    """
    # Size pre-check — never attempt to read >1MB into vision context
    if image_size_bytes is not None:
        if image_size_bytes > MAX_IMAGE_BYTES:
            return None
    else:
        # Try HEAD to get size
        try:
            head = requests.head(image_url, headers=DEFAULT_HEADERS, timeout=8, allow_redirects=True)
            cl = head.headers.get("Content-Length")
            if cl and cl.isdigit():
                size = int(cl)
                if size > MAX_IMAGE_BYTES:
                    return None
            else:
                # No content-length — be cautious, skip
                return None
        except requests.RequestException:
            return None

    # Download
    try:
        resp = requests.get(image_url, headers=DEFAULT_HEADERS, timeout=15)
        if resp.status_code >= 400:
            return None
        image_bytes = resp.content
        if len(image_bytes) > MAX_IMAGE_BYTES:
            return None
        media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            return None
    except requests.RequestException:
        return None

    # Vision call
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
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
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
        # Unparseable verdict — treat as reject (conservative)
        return False
    except anthropic.APIError:
        return None
