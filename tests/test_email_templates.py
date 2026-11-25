from datetime import UTC, datetime, timedelta

import pytest

from app.services.email.templates import (
    IncidentEmailContext,
    format_duration,
    render_incident_opened,
    render_incident_resolved,
    safe_target,
)

START = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def make_ctx(**overrides) -> IncidentEmailContext:
    values = {
        "monitor_name": "Customer API",
        "target": "https://api.acme.com/health",
        "incident_id": "inc_123",
        "started_at": START,
        "cause": "HTTP 503 from API",
    }
    values.update(overrides)
    return IncidentEmailContext(**values)


def test_opened_email_content():
    mail = render_incident_opened(make_ctx())
    assert mail.subject == "[DOWN] Customer API is down"
    assert "HTTP 503 from API" in mail.text
    assert "2026-11-20 10:00:00 UTC" in mail.text
    assert "inc_123" in mail.html


def test_resolved_email_has_downtime():
    ctx = make_ctx(resolved_at=START + timedelta(hours=1, minutes=5))
    mail = render_incident_resolved(ctx)
    assert mail.subject == "[RESOLVED] Customer API is back up"
    assert "1h 5m" in mail.text


def test_resolved_requires_resolved_at():
    with pytest.raises(ValueError):
        render_incident_resolved(make_ctx())


def test_html_is_escaped():
    mail = render_incident_opened(
        make_ctx(monitor_name="<script>alert(1)</script>", cause="<b>bad</b>")
    )
    assert "<script>" not in mail.html
    assert "&lt;script&gt;" in mail.html
    assert "<b>bad</b>" not in mail.html


def test_subject_cannot_contain_newlines():
    mail = render_incident_opened(make_ctx(monitor_name="API\r\nBcc: evil@x.com"))
    assert "\n" not in mail.subject
    assert "\r" not in mail.subject


def test_target_hides_credentials_and_query():
    shown = safe_target("https://user:pass@api.acme.com:8443/health?token=abc#x")
    assert shown == "https://api.acme.com:8443/health"
    assert "pass" not in shown
    assert "token" not in shown


def test_non_url_targets_are_kept():
    assert safe_target("db.acme.com:5432") == "db.acme.com:5432"


def test_status_page_link_only_for_http_urls():
    good = render_incident_opened(make_ctx(status_page_url="https://status.acme.com"))
    assert 'href="https://status.acme.com"' in good.html
    bad = render_incident_opened(make_ctx(status_page_url="javascript:alert(1)"))
    assert "javascript:" not in bad.html
    assert "javascript:" not in bad.text


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (45, "45s"), (125, "2m 5s"), (3900, "1h 5m"), (90061, "1d 1h 1m 1s")],
)
def test_format_duration(seconds, expected):
    assert format_duration(timedelta(seconds=seconds)) == expected
