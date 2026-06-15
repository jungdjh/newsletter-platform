"""Invariant tests — rules that MUST hold regardless of content.

Unlike snapshot tests, these don't need re-blessing when the renderer changes
intentionally. They encode hard guarantees:

  1. No broken style attributes (an inner unescaped `"` truncates the attribute)
  2. The issue meta strip contains the issue number
  3. Every source_url from the input appears in the HTML
"""

import json
import re
from pathlib import Path

import pytest

from scripts.render_html import render_newsletter

FIXTURES = Path(__file__).parent / "fixtures"
ALL_NEWSLETTERS = ["ai-pms"]


def _build_render(newsletter: str) -> dict:
    payload = json.loads((FIXTURES / f"{newsletter}_input.json").read_text())
    html, plaintext = render_newsletter(
        payload["newsletter"],
        payload["content"],
        payload["meta"],
        editor_concerns=payload.get("editor_concerns"),
    )
    return {"newsletter": newsletter, "html": html, "plaintext": plaintext, "payload": payload}


@pytest.fixture(params=ALL_NEWSLETTERS)
def render(request):
    return _build_render(request.param)


def test_no_broken_style_attributes(render):
    """Every style="..." attribute value must NOT contain an unescaped inner `"`,
    which would terminate the attribute early and drop the styles after it."""
    html = render["html"]
    style_attrs = re.findall(r'style="([^"]*)"', html)
    assert style_attrs, "Expected at least one style attribute in rendered HTML"
    style_opens = html.count('style="')
    assert len(style_attrs) == style_opens, (
        f"[{render['newsletter']}] Mismatch between style=\" openings ({style_opens}) and "
        f"parsed style attributes ({len(style_attrs)}) — an unescaped inner `\"` truncated one."
    )


def test_issue_number_in_html(render):
    issue_num = render["payload"]["meta"]["issue_number"]
    html = render["html"]
    assert (
        f"{issue_num:03d}" in html or f"No.{issue_num}" in html or f"#{issue_num}" in html
    ), f"[{render['newsletter']}] Issue number {issue_num} not found in rendered HTML"


def test_every_source_url_appears_in_html(render):
    html = render["html"]
    content = render["payload"]["content"]
    expected_urls = [s["source_url"] for s in content.get("top_stories", [])]
    expected_urls += [item["source_url"] for item in content.get("other_news", [])]
    for url in expected_urls:
        assert url in html, (
            f"[{render['newsletter']}] Source URL missing from rendered HTML: {url}"
        )
