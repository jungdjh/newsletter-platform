"""Notion API client for recipients + feedback.

Wraps notion-client for two operations:
1. Fetch active recipients for a given newsletter (BCC list).
2. Read recent feedback entries since a cutoff timestamp.
3. Insert new feedback entries when readers reply.

Data source IDs and display names are operator-workspace-specific: an env var
wins, else the private audience config supplies them. Never hardcoded — they
must not ship in a public mirror of the engine.
"""

from __future__ import annotations

import os
from typing import Any
from datetime import datetime

from notion_client import Client

from scripts import audience_config


def _recipients_ds_id() -> str:
    ds = os.environ.get("NOTION_RECIPIENTS_DS_ID") or audience_config.notion_recipients_ds_id()
    if not ds:
        raise RuntimeError("recipients data-source ID not set (NOTION_RECIPIENTS_DS_ID env or operator.json)")
    return ds


def _feedback_ds_id() -> str:
    ds = os.environ.get("NOTION_FEEDBACK_DS_ID") or audience_config.notion_feedback_ds_id()
    if not ds:
        raise RuntimeError("feedback data-source ID not set (NOTION_FEEDBACK_DS_ID env or operator.json)")
    return ds


def _display(newsletter: str) -> str:
    """Newsletter property value used in Notion for a short-name."""
    return audience_config.newsletter_display_names().get(newsletter, newsletter)


def _client() -> Client:
    token = os.environ.get("NOTION_API_KEY")
    if not token:
        raise RuntimeError("NOTION_API_KEY not set in environment")
    return Client(auth=token)


def get_active_recipients(newsletter: str) -> list[dict[str, str]]:
    """Return active recipients subscribed to the given newsletter.

    Args:
        newsletter: The audience short-name.

    Returns:
        List of {name, email} dicts.
    """
    display = _display(newsletter)
    notion = _client()
    results: list[dict[str, str]] = []
    cursor = None
    while True:
        payload: dict[str, Any] = {
            "data_source_id": _recipients_ds_id(),
            "filter": {
                "and": [
                    {"property": "Status", "select": {"equals": "Active"}},
                    {"property": "Newsletter", "multi_select": {"contains": display}},
                ]
            },
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor
        response = notion.data_sources.query(**payload)
        for page in response.get("results", []):
            props = page.get("properties", {})
            email = _get_prop_text(props.get("Email", {}))
            name = _get_prop_text(props.get("Name", {})) or _get_title(props.get("Name", {}))
            if email:
                results.append({"name": name or "", "email": email})
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return results


def get_recent_feedback(newsletter: str, since_iso: str) -> list[dict[str, Any]]:
    """Return feedback entries created since the given ISO timestamp."""
    display = _display(newsletter)
    notion = _client()
    results = []
    cursor = None
    while True:
        payload: dict[str, Any] = {
            "data_source_id": _feedback_ds_id(),
            "filter": {
                "and": [
                    {"property": "Newsletter", "select": {"equals": display}},
                    {"timestamp": "created_time", "created_time": {"on_or_after": since_iso}},
                ]
            },
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor
        response = notion.data_sources.query(**payload)
        for page in response.get("results", []):
            props = page.get("properties", {})
            results.append({
                "id": page.get("id"),
                "created_time": page.get("created_time"),
                "summary": _get_title(props.get("Summary", {})) or _get_prop_text(props.get("Summary", {})),
                "from": _get_prop_text(props.get("From", {})),
                "body": _get_prop_text(props.get("Body", {})),
            })
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return results


def create_feedback_entry(
    *,
    newsletter: str,
    summary: str,
    from_email: str,
    body: str,
    received_at: str | None = None,
) -> str:
    """Insert a new feedback row, return its page id."""
    display = _display(newsletter)
    notion = _client()
    properties = {
        "Summary": {"title": [{"text": {"content": summary[:200]}}]},
        "Newsletter": {"select": {"name": display}},
        "From": {"rich_text": [{"text": {"content": from_email[:200]}}]},
        "Body": {"rich_text": [{"text": {"content": body[:1900]}}]},
    }
    if received_at:
        properties["Received"] = {"date": {"start": received_at}}
    page = notion.pages.create(
        parent={"data_source_id": _feedback_ds_id()},
        properties=properties,
    )
    return page["id"]


# --- small property-extraction helpers ---

def _get_prop_text(prop: dict[str, Any]) -> str:
    """Pull plain text out of a Notion property regardless of type."""
    if not prop:
        return ""
    t = prop.get("type")
    if t == "email":
        return prop.get("email") or ""
    if t == "rich_text":
        return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))
    if t == "title":
        return "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
    if t == "url":
        return prop.get("url") or ""
    if t == "select":
        sel = prop.get("select") or {}
        return sel.get("name", "")
    return ""


def _get_title(prop: dict[str, Any]) -> str:
    if not prop:
        return ""
    return "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
