"""
gmail_list skill — searches Gmail and returns a structured table with
subject, sender, date and a direct link per message.

Internally chains two MCP calls:
  1. search_gmail_messages  → message IDs + web links
  2. get_gmail_messages_content_batch (metadata) → subject, from, date
"""
import re
from typing import Any

from runtime.models import RequestContext, SkillResult
from skills.base import BaseSkill
from skills.mcp_tools import _MCP_GMAIL_USER_EMAIL, _run_async, _call_tool


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_search_results(text: str) -> list[dict]:
    """Extract message_id + web_link pairs from search_gmail_messages output."""
    rows = []
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        mid = re.search(r"Message ID:\s*(\S+)", block)
        url = re.search(r"Web Link:\s*(\S+)", block)
        if mid and mid.group(1) != "unknown":
            rows.append({
                "id": mid.group(1),
                "url": url.group(1) if url else "",
            })
    return rows


def _parse_batch_content(text: str) -> dict[str, dict]:
    """
    Parse get_gmail_messages_content_batch (metadata format) output.
    Returns a dict keyed by message_id.
    """
    result = {}
    # Each message block starts with "Message ID: <id>"
    blocks = re.split(r"\n---\n", text)
    for block in blocks:
        mid = re.search(r"Message ID:\s*(\S+)", block)
        if not mid:
            continue
        key = mid.group(1)
        subject = re.search(r"^Subject:\s*(.+)$", block, re.MULTILINE)
        sender = re.search(r"^From:\s*(.+)$", block, re.MULTILINE)
        date = re.search(r"^Date:\s*(.+)$", block, re.MULTILINE)
        url = re.search(r"^Web Link:\s*(\S+)", block, re.MULTILINE)
        result[key] = {
            "subject": subject.group(1).strip() if subject else "(no subject)",
            "from": _shorten_sender(sender.group(1).strip() if sender else ""),
            "date": _shorten_date(date.group(1).strip() if date else ""),
            "url": url.group(1).strip() if url else "",
        }
    return result


def _shorten_sender(raw: str) -> str:
    """'John Doe <john@example.com>' → 'John Doe'  or  'john@example.com'"""
    m = re.match(r"^(.+?)\s*<[^>]+>$", raw)
    if m:
        name = m.group(1).strip().strip('"')
        return name if name else raw
    return raw.split("@")[0] if "@" in raw else raw


def _shorten_date(raw: str) -> str:
    """Keep only 'DD Mon YYYY' or 'Mon DD' portion."""
    m = re.search(r"\d{1,2}\s+\w{3}\s+\d{4}", raw)
    if m:
        return m.group(0)
    m = re.search(r"\w{3},\s+\d{1,2}\s+\w{3}", raw)
    if m:
        return m.group(0)
    return raw[:16] if len(raw) > 16 else raw


def _build_table(rows: list[dict], meta: dict[str, dict]) -> str:
    lines = ["*#* | *Assunto* | *De* | *Data* | *Link*"]
    lines.append("---|---|---|---|---")
    for i, row in enumerate(rows, 1):
        m = meta.get(row["id"], {})
        subject = m.get("subject", "(sem assunto)")[:55]
        sender = m.get("from", "")[:30]
        date = m.get("date", "")
        url = m.get("url") or row.get("url", "")
        lines.append(f"{i} | {subject} | {sender} | {date} | {url}")
    return "\n".join(lines)


# ── skill ─────────────────────────────────────────────────────────────────────

class GmailListSkill(BaseSkill):
    name = "gmail_list"
    description = (
        "Lists Gmail emails matching a query as a table with subject, sender, date "
        "and a direct link. Supports any Gmail search operator "
        "(e.g. 'in:inbox is:unread', 'in:spam', 'label:work', 'from:boss@co.com'). "
        "Use this whenever the user wants to browse, list or search their email."
    )

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        query: str = args.get("query", "in:inbox")
        page_size: int = int(args.get("page_size", 10))
        email = _MCP_GMAIL_USER_EMAIL

        if not email:
            return SkillResult(ok=False, error="MCP_GMAIL_USER_EMAIL not configured")

        try:
            # Step 1 — get message IDs
            search_raw = _run_async(
                _call_tool("search_gmail_messages", {
                    "query": query,
                    "page_size": page_size,
                    "user_google_email": email,
                })
            )
            search_text = _extract_text(search_raw)
            rows = _parse_search_results(search_text)

            if not rows:
                return SkillResult(
                    ok=True,
                    output={"rows": []},
                    user_visible_text=f"Nenhuma mensagem encontrada para: {query}",
                )

            # Step 2 — fetch metadata (subject, from, date) in one batch call
            batch_raw = _run_async(
                _call_tool("get_gmail_messages_content_batch", {
                    "message_ids": [r["id"] for r in rows],
                    "format": "metadata",
                    "user_google_email": email,
                })
            )
            batch_text = _extract_text(batch_raw)
            meta = _parse_batch_content(batch_text)

            table = _build_table(rows, meta)
            return SkillResult(ok=True, output={"rows": rows, "meta": meta}, user_visible_text=table)

        except Exception as exc:
            return SkillResult(ok=False, error=f"gmail_list failed: {exc}")


def _extract_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("result", "text"):
            if isinstance(result.get(key), str):
                return result[key]
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                t = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
                if isinstance(t, str):
                    parts.append(t)
            return "\n".join(parts)
    # object with .content list
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts = []
        for item in content:
            t = getattr(item, "text", None)
            if isinstance(t, str):
                parts.append(t)
        return "\n".join(parts)
    return str(result)
