"""Fetch an article URL and extract main content + og:image.

Used as an agent tool to read full articles for ranking and content generation.
Uses readability-lxml for content extraction and BeautifulSoup for og:image.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from readability import Document

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(RuntimeError):
    pass


def fetch(url: str, *, timeout: int = 20) -> dict[str, Any]:
    """Fetch an article and return structured content.

    Returns:
        {
          "url": str,
          "final_url": str,           # after redirects
          "title": str,
          "body_text": str,            # readable article body
          "body_html": str,            # readability summary HTML
          "og_image_url": str | None,
          "og_image_size_bytes": int | None,
          "site_name": str | None,
          "published_at": str | None,
        }

    Raises:
        FetchError on HTTP errors or unfetchable content.
    """
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        raise FetchError(f"Failed to fetch {url}: {e}") from e

    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code} for {url}")

    html = resp.text
    final_url = resp.url

    # Readability for main content
    try:
        doc = Document(html)
        title = doc.short_title() or doc.title() or ""
        body_html = doc.summary(html_partial=True)
        body_text = BeautifulSoup(body_html, "lxml").get_text(separator="\n", strip=True)
    except Exception:
        title = ""
        body_html = ""
        body_text = ""

    # Open Graph + meta extraction
    soup = BeautifulSoup(html, "lxml")
    og_image_url = _first_meta(soup, ["og:image", "twitter:image", "twitter:image:src"])
    if og_image_url:
        og_image_url = urljoin(final_url, og_image_url)
    site_name = _first_meta(soup, ["og:site_name"])
    published_at = _first_meta(soup, ["article:published_time", "og:published_time", "datePublished"])

    # Try to read image size via HEAD without downloading the body
    og_image_size = None
    if og_image_url:
        try:
            head = requests.head(og_image_url, headers=DEFAULT_HEADERS, timeout=8, allow_redirects=True)
            cl = head.headers.get("Content-Length")
            if cl and cl.isdigit():
                og_image_size = int(cl)
        except requests.RequestException:
            pass  # silent — size will just be None

    if not title:
        title = (soup.title.string.strip() if soup.title and soup.title.string else url)

    return {
        "url": url,
        "final_url": final_url,
        "title": title,
        "body_text": body_text[:3500],   # cap so agent context stays under Tier 1 TPM
        "body_html": body_html,
        "og_image_url": og_image_url,
        "og_image_size_bytes": og_image_size,
        "site_name": site_name,
        "published_at": published_at,
    }


def _first_meta(soup: BeautifulSoup, names: list[str]) -> str | None:
    for name in names:
        # property=
        tag = soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
        # name=
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None
