from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import urlsplit


@dataclass(frozen=True)
class IncidentEmailContext:
    monitor_name: str
    target: str
    incident_id: str
    started_at: datetime
    cause: str | None = None
    resolved_at: datetime | None = None
    status_page_url: str | None = None


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


def _one_line(value: str, limit: int = 200) -> str:
    """Collapse all whitespace (including newlines) and cap the length."""
    return " ".join(value.split())[:limit]


def safe_target(target: str) -> str:
    """Show a target without credentials, query string or fragment."""
    if "://" not in target:
        return _one_line(target)
    try:
        parts = urlsplit(target)
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "[unreadable target]"
    return _one_line(f"{parts.scheme}://{parts.hostname or ''}{port}{parts.path}")


def _fmt_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_duration(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _valid_url(url: str | None) -> str | None:
    if url and urlsplit(url).scheme in ("http", "https"):
        return url
    return None


def _build(
    subject: str,
    heading: str,
    color: str,
    rows: list[tuple[str, str]],
    status_page_url: str | None,
) -> RenderedEmail:
    url = _valid_url(status_page_url)

    row_html = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#555">{escape(label)}</td>'
        f'<td style="padding:4px 0">{escape(value)}</td></tr>'
        for label, value in rows
    )
    link_html = (
        f'<p><a href="{escape(url, quote=True)}">View status page</a></p>' if url else ""
    )
    html = (
        '<div style="font-family:Arial,sans-serif;max-width:560px">'
        f'<h2 style="color:{color}">{escape(heading)}</h2>'
        f"<table>{row_html}</table>{link_html}</div>"
    )

    text = heading + "\n\n" + "\n".join(f"{label}: {value}" for label, value in rows)
    if url:
        text += f"\n\nStatus page: {url}"

    return RenderedEmail(subject=_one_line(subject), html=html, text=text)


def render_incident_opened(ctx: IncidentEmailContext) -> RenderedEmail:
    name = _one_line(ctx.monitor_name)
    rows = [
        ("Monitor", name),
        ("Target", safe_target(ctx.target)),
        ("Started", _fmt_time(ctx.started_at)),
    ]
    if ctx.cause:
        rows.append(("Cause", _one_line(ctx.cause)))
    rows.append(("Incident ID", _one_line(ctx.incident_id)))
    return _build(
        subject=f"[DOWN] {name} is down",
        heading=f"{name} is down",
        color="#c0392b",
        rows=rows,
        status_page_url=ctx.status_page_url,
    )


def render_incident_resolved(ctx: IncidentEmailContext) -> RenderedEmail:
    if ctx.resolved_at is None:
        raise ValueError("resolved_at is required for a resolved email")
    name = _one_line(ctx.monitor_name)
    rows = [
        ("Monitor", name),
        ("Target", safe_target(ctx.target)),
        ("Started", _fmt_time(ctx.started_at)),
        ("Resolved", _fmt_time(ctx.resolved_at)),
        ("Downtime", format_duration(ctx.resolved_at - ctx.started_at)),
        ("Incident ID", _one_line(ctx.incident_id)),
    ]
    return _build(
        subject=f"[RESOLVED] {name} is back up",
        heading=f"{name} is back up",
        color="#27ae60",
        rows=rows,
        status_page_url=ctx.status_page_url,
    )
