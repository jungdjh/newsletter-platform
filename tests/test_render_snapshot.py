"""Snapshot tests for render_newsletter.

What this catches:
  - ANY change to the rendered HTML or plaintext, intentional or not.
  - Run after every render_html.py edit. If output drifts, either the
    change is a bug, or you blessed it via `make update-goldens`.

How to update a golden after an intentional change:
  $ make update-goldens
  Review the diff (git), commit if correct.

Why this exists:
  Freezes the rendered output so any unintended drift from a render
  change fails loudly until you re-bless it.
"""

import json
import os
from pathlib import Path

import pytest

from scripts.render_html import render_newsletter

FIXTURES = Path(__file__).parent / "fixtures"
NEWSLETTERS = ["ai-pms"]


def _render(newsletter: str) -> tuple[str, str]:
    payload = json.loads((FIXTURES / f"{newsletter}_input.json").read_text())
    return render_newsletter(
        payload["newsletter"],
        payload["content"],
        payload["meta"],
        editor_concerns=payload.get("editor_concerns"),
    )


@pytest.mark.parametrize("newsletter", NEWSLETTERS)
def test_html_matches_golden(newsletter: str) -> None:
    html, _ = _render(newsletter)
    golden_path = FIXTURES / f"{newsletter}_golden.html"

    if os.environ.get("UPDATE_GOLDENS") == "1":
        golden_path.write_text(html)
        pytest.skip(f"Updated {golden_path.name}")
        return

    assert golden_path.exists(), (
        f"No golden yet for {newsletter}. Run `make update-goldens` to bless current output."
    )
    expected = golden_path.read_text()
    assert html == expected, (
        f"\nHTML drifted from {golden_path.name}.\n"
        f"If this is intentional, run `make update-goldens` and review the diff.\n"
    )


@pytest.mark.parametrize("newsletter", NEWSLETTERS)
def test_plaintext_matches_golden(newsletter: str) -> None:
    _, plaintext = _render(newsletter)
    golden_path = FIXTURES / f"{newsletter}_golden.txt"

    if os.environ.get("UPDATE_GOLDENS") == "1":
        golden_path.write_text(plaintext)
        pytest.skip(f"Updated {golden_path.name}")
        return

    assert golden_path.exists(), (
        f"No golden yet for {newsletter}. Run `make update-goldens` to bless current output."
    )
    expected = golden_path.read_text()
    assert plaintext == expected, (
        f"\nPlaintext drifted from {golden_path.name}.\n"
        f"If this is intentional, run `make update-goldens` and review the diff.\n"
    )
