import hashlib
import hmac

from app.services.webhook.signing import generate_secret, sign, verify_signature

SECRET = "whsec_test"
BODY = b'{"a":1}'
TS = 1_700_000_000


def test_signature_format_is_documented_scheme():
    expected = "sha256=" + hmac.new(
        SECRET.encode(), b"1700000000." + BODY, hashlib.sha256
    ).hexdigest()
    assert sign(SECRET, TS, BODY) == expected


def test_valid_signature_verifies():
    assert verify_signature(SECRET, str(TS), sign(SECRET, TS, BODY), BODY, now=TS)


def test_wrong_secret_fails():
    assert not verify_signature("other", str(TS), sign(SECRET, TS, BODY), BODY, now=TS)


def test_tampered_body_fails():
    assert not verify_signature(SECRET, str(TS), sign(SECRET, TS, BODY), b'{"a":2}', now=TS)


def test_old_timestamp_is_rejected():
    assert not verify_signature(SECRET, str(TS), sign(SECRET, TS, BODY), BODY, now=TS + 301)


def test_far_future_timestamp_is_rejected():
    assert not verify_signature(SECRET, str(TS), sign(SECRET, TS, BODY), BODY, now=TS - 301)


def test_timestamp_inside_tolerance_is_accepted():
    assert verify_signature(SECRET, str(TS), sign(SECRET, TS, BODY), BODY, now=TS + 299)


def test_garbage_headers_fail_without_crashing():
    assert not verify_signature(SECRET, "not-a-number", "x", BODY, now=TS)
    assert not verify_signature(SECRET, None, None, BODY, now=TS)
    assert not verify_signature(SECRET, str(TS), "sha256=é", BODY, now=TS)


def test_generated_secrets_are_unique_and_prefixed():
    a, b = generate_secret(), generate_secret()
    assert a != b
    assert a.startswith("whsec_")
    assert len(a) > 30
