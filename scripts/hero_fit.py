"""Move 1 of the image-quality fix — a fit-check for curated-library hero images.

Tier-1 publisher images already run through a vision grader (og_image.grade_image)
before they may sit on a story. Tier-2 curated-library images did NOT: they were
applied on a topic-tag match alone, with nothing checking the photo fit THIS
story. That put a U.S. Navy public-domain photo of Indonesian nursing students in
Banda Aceh onto a story about *California* phlebotomy certification.

This module closes the hole. A library pick must clear two checks before it is
allowed onto a story; failing either drops the image and the story renders
text-only ("a wrong photo is worse than no photo"):

  1. PLACE (deterministic, always on, no API call): a geographically specific
     newsletter must not carry a photo taken in a *different, specific* country.
     Derived from the image's manifest `place` vs the newsletter's `geography`.
     This alone rejects the Indonesian (and Australian / U.K.) library photos for
     a Bay Area newsletter. Fail-OPEN: an unknown place on either side never
     triggers a rejection, so we only ever drop a photo we can prove is foreign.

  2. SUBJECT / AUDIENCE (vision, only when ANTHROPIC_API_KEY is set): the same
     relevance/audience grader publisher images go through, run against the
     specific story headline + track. Fail-OPEN to pass when there's no key (the
     place guard still protects); the grader itself fails closed on any error.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

# We only need to know two things about a place string: (a) is it the U.S., and
# (b) if not, is it some *specific* foreign country. Anything we can't pin down is
# treated as "makes no national claim" and never triggers a place rejection.
_US_HINTS = (
    "united states", "u.s.a", "usa", "u.s.", "america",
    "california", "bay area", "san francisco", "oakland", "san jose", "peninsula",
)
_UK_HINTS = ("united kingdom", "u.k.", "england", "scotland", "wales", "britain")
# A short roster of country names we might meet in a manifest `place` field.
_COUNTRIES = (
    "indonesia", "australia", "canada", "india", "philippines", "china",
    "japan", "korea", "germany", "france", "mexico", "brazil", "kenya",
    "nigeria", "vietnam", "thailand", "ireland", "spain", "italy",
)


def _country(text: Optional[str]) -> Optional[str]:
    """Normalize a place / geography string to a coarse country token, or None
    when it makes no specific national claim (generic / unspecified / unknown)."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip().lower()
    if t in ("", "unspecified", "generic", "n/a", "none", "unknown"):
        return None
    if any(h in t for h in _US_HINTS):
        return "united states"
    if any(h in t for h in _UK_HINTS):
        return "united kingdom"
    for c in _COUNTRIES:
        if c in t:
            return c
    return None  # unrecognized -> make no claim (fail-open)


def place_ok(image_place: Optional[str], newsletter_geo: Optional[str]) -> Tuple[bool, str]:
    """A geographically specific newsletter must not carry a photo from a
    *different, specific* country. Fail-open when either side is unknown."""
    img_c = _country(image_place)
    geo_c = _country(newsletter_geo)
    if img_c and geo_c and img_c != geo_c:
        return False, f"photo is from {img_c.title()}, newsletter covers {geo_c.title()}"
    return True, "place ok"


def hero_fit(story: dict, image_entry: dict, *, newsletter_geo: Optional[str] = None,
             audience_desc: str = "", resolved_url: Optional[str] = None,
             use_vision: bool = True) -> Tuple[bool, str]:
    """Return (ok, reason) for pairing image_entry with story.

    Place guard first (deterministic); then, when a key + a fetchable URL are
    present, the vision relevance/audience grader. Never raises."""
    ok, why = place_ok(image_entry.get("place"), newsletter_geo)
    if not ok:
        return False, why

    if use_vision and os.environ.get("ANTHROPIC_API_KEY") and resolved_url:
        try:
            from scripts.tools import og_image
            v = og_image.grade_image(
                resolved_url, story.get("headline", ""),
                track=story.get("track", ""), audience=audience_desc)
            if v.get("grade") == "reject" or not v.get("relevant", True) \
                    or not v.get("audience_ok", True):
                return False, f"vision: {v.get('reason', 'not a fit for this story')}"
        except Exception:
            pass  # fail-open — the place guard already passed

    return True, "fit ok"
