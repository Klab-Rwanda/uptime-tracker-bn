import pytest
from pydantic import ValidationError

from app.notification_channels.schemas import (
    ChannelCreate,
    ChannelUpdate,
    normalize_config,
    summarize_config,
)

ORG = "5b7b8b9e-5d0b-4c53-8f3e-3f2d3a4b5c6d"
URL = "https://hooks.acme.com/services/T0/B0/token-abc"


def webhook(url):
    return {"webhook_url": url}


def emails(*addresses):
    return {"recipients": list(addresses)}


# --- webhook ------------------------------------------------------------


def test_valid_webhook_config():
    assert normalize_config("webhook", webhook(f"  {URL}  ")) == webhook(URL)


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.acme.com/x",
        "https://user:pw@hooks.acme.com/x",
        "https://localhost/x",
        "https://app.localhost/x",
        "https://127.0.0.1/x",
        "https://10.0.0.5/x",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/x",
        "ftp://hooks.acme.com/x",
        "",
        "not a url",
    ],
)
def test_unsafe_webhook_urls_are_rejected(url):
    with pytest.raises(ValueError) as info:
        normalize_config("webhook", webhook(url))
    assert url not in str(info.value) or url == ""


@pytest.mark.parametrize(
    "config", [{}, {"webhook_url": 123}, {"webhook_url": None}, {"url": URL}]
)
def test_bad_webhook_shapes_are_rejected(config):
    with pytest.raises(ValueError):
        normalize_config("webhook", config)


def test_unknown_webhook_keys_are_rejected():
    with pytest.raises(ValueError):
        normalize_config("webhook", {"webhook_url": URL, "secret": "mine"})


def test_private_targets_are_allowed_only_in_dev_mode():
    config = webhook("http://localhost:9000/hook")
    assert normalize_config("webhook", config, allow_private=True) == config
    with pytest.raises(ValueError):
        normalize_config("webhook", config)


# --- email --------------------------------------------------------------


def test_email_recipients_are_lowercased_and_deduplicated():
    config = emails(" Ops@Acme.com ", "ops@acme.com", "dev@acme.com")
    assert normalize_config("email", config) == emails("ops@acme.com", "dev@acme.com")


@pytest.mark.parametrize(
    "address",
    [
        "plainaddress",
        "a@b",
        "a b@c.com",
        "a@b.com, c@d.com",
        "a@b.com;c@d.com",
        "<a@b.com>",
        "x@y.com\nBcc: z@w.com",
        "",
        "a" * 250 + "@b.com",
    ],
)
def test_invalid_email_addresses_are_rejected(address):
    with pytest.raises(ValueError):
        normalize_config("email", emails(address))


@pytest.mark.parametrize(
    "config",
    [{}, {"recipients": []}, {"recipients": "a@b.com"}, {"recipients": [1]}],
)
def test_bad_email_shapes_are_rejected(config):
    with pytest.raises(ValueError):
        normalize_config("email", config)


def test_too_many_recipients_are_rejected():
    with pytest.raises(ValueError):
        normalize_config("email", emails(*[f"user{i}@acme.com" for i in range(11)]))


def test_ten_recipients_are_fine():
    config = emails(*[f"user{i}@acme.com" for i in range(10)])
    assert len(normalize_config("email", config)["recipients"]) == 10


def test_unknown_email_keys_are_rejected():
    with pytest.raises(ValueError):
        normalize_config("email", {"recipients": ["a@b.com"], "cc": ["c@d.com"]})


def test_unsupported_type_is_rejected():
    with pytest.raises(ValueError):
        normalize_config("slack", webhook(URL))


# --- summaries never leak secrets -----------------------------------------


def test_webhook_summary_shows_only_the_host():
    summary = summarize_config("webhook", {"webhook_url": URL, "secret": "whsec_x"})
    assert summary == {"url_host": "hooks.acme.com"}
    assert "token-abc" not in str(summary)
    assert "whsec_x" not in str(summary)


def test_email_summary_lists_recipients():
    assert summarize_config("email", emails("ops@acme.com")) == {
        "recipients": ["ops@acme.com"]
    }


# --- request models ---------------------------------------------------------


def test_create_validates_the_config_for_its_type():
    channel = ChannelCreate(
        organization_id=ORG, name="  On-call  ", type="webhook", config=webhook(URL)
    )
    assert channel.name == "On-call"
    assert channel.config == webhook(URL)


def test_create_rejects_a_bad_config():
    with pytest.raises(ValidationError):
        ChannelCreate(
            organization_id=ORG, name="x", type="email", config=emails("nope")
        )


@pytest.mark.parametrize("channel_type", ["slack", "sms", ""])
def test_create_rejects_unsupported_types(channel_type):
    with pytest.raises(ValidationError):
        ChannelCreate(
            organization_id=ORG, name="x", type=channel_type, config=webhook(URL)
        )


@pytest.mark.parametrize("name", ["", "   ", "bad\x00name", "x" * 256])
def test_create_rejects_bad_names(name):
    with pytest.raises(ValidationError):
        ChannelCreate(
            organization_id=ORG, name=name, type="webhook", config=webhook(URL)
        )


def test_create_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ChannelCreate(
            organization_id=ORG,
            name="x",
            type="webhook",
            config=webhook(URL),
            secret="mine",
        )


def test_update_needs_at_least_one_change():
    with pytest.raises(ValidationError):
        ChannelUpdate()
    assert ChannelUpdate(rotate_secret=True).rotate_secret is True
    assert ChannelUpdate(name="  New  ").name == "New"


def test_update_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ChannelUpdate(name="x", type="email")
