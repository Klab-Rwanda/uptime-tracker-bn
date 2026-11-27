"""What incident alerts look like in Slack (Block Kit messages).

Everything that comes from customers or checked servers (monitor names, error
causes) is escaped: Slack treats <...> as links and mentions, so an unescaped
"<!channel>" in a monitor name would ping a whole channel.
"""

from datetime import UTC, datetime
from typing import Any

from app.services.email.templates import format_duration, safe_target

MAX_HEADER = 150  # Slack's limit for header text
MAX_CAUSE = 500


def escape(text: str) -> str:
    """Slack's escaping rule: only &, < and > need it."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _one_line(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _field(label: str, value: str) -> dict[str, str]:
    return {"type": "mrkdwn", "text": f"*{label}*\n{value}"}


def incident_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the Slack message for a queued alert payload (see build_payload)."""
    name = _one_line(payload.get("monitor_name") or "A monitor", 120)
    target = escape(safe_target(str(payload.get("target") or "")))
    started = _parse(payload.get("started_at"))
    resolved = _parse(payload.get("resolved_at"))

    if payload.get("event") == "resolved":
        headline = f"🟢 {name} is back up"
        fields = [_field("Target", target)]
        if started and resolved:
            fields.append(_field("Down for", format_duration(resolved - started)))
            fields.append(_field("Recovered", _time(resolved)))
    else:
        headline = f"🔴 {name} is down"
        fields = [_field("Target", target)]
        if started:
            fields.append(_field("Since", _time(started)))

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline[:MAX_HEADER]}},
        {"type": "section", "fields": fields},
    ]
    cause = payload.get("cause")
    if cause and payload.get("event") != "resolved":
        # Backticks would close the code block early
        text = escape(_one_line(str(cause), MAX_CAUSE)).replace("`", "'")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Cause*\n```{text}```"}})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"UpTimeTracker · incident {escape(str(payload.get('incident_id', '')))}",
                }
            ],
        }
    )
    # "text" is what notifications and screen readers show
    return {"text": escape(headline), "blocks": blocks}


def channel_test_message(channel_name: str, sent_at: datetime) -> dict[str, Any]:
    name = escape(_one_line(channel_name, 120))
    headline = "✅ UpTimeTracker test notification"
    return {
        "text": headline,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": headline}},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"This is a test from the notification channel *{name}*, "
                    f"sent {_time(sent_at)}. Incident alerts will arrive here.",
                },
            },
        ],
    }
