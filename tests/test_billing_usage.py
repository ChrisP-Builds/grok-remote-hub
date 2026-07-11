"""Unit tests for weekly plan billing usage normalization (no live network)."""

from __future__ import annotations

from hub.billing_usage import (
    _candidate_access_token,
    _entry_expired,
    _parse_expires_at_epoch,
    clear_caches,
    normalize_credits_config,
)


SAMPLE_CONFIG = {
    "creditUsagePercent": 4.0,
    "currentPeriod": {
        "type": "USAGE_PERIOD_TYPE_WEEKLY",
        "start": "2026-07-10T21:20:20.719654+00:00",
        "end": "2026-07-17T21:20:20.719654+00:00",
    },
    "billingPeriodStart": "2026-07-10T21:20:20.719654+00:00",
    "billingPeriodEnd": "2026-07-17T21:20:20.719654+00:00",
    "productUsage": [
        {"product": "GrokBuild", "usagePercent": 4.0},
        {"product": "Api"},
        {"product": "GrokChat"},
        {"product": "GrokImagine"},
    ],
    "isUnifiedBillingUser": True,
    "onDemandCap": {"val": 0},
    "onDemandUsed": {"val": 0},
    "prepaidBalance": {"val": 0},
}


def test_normalize_weekly_sample() -> None:
    result = normalize_credits_config(SAMPLE_CONFIG)
    assert result["available"] is True
    assert result["error"] is None
    assert result["weeklyPercent"] == 4.0
    assert result["periodType"] == "WEEKLY"
    assert result["periodStart"] == "2026-07-10T21:20:20.719654+00:00"
    assert result["periodEnd"] == "2026-07-17T21:20:20.719654+00:00"
    assert result["product"] == "GrokBuild"


def test_normalize_prefers_grokbuild_product() -> None:
    config = {
        "creditUsagePercent": 99.0,
        "productUsage": [
            {"product": "GrokChat", "usagePercent": 50.0},
            {"product": "GrokBuild", "usagePercent": 12.5},
        ],
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-07-10T00:00:00+00:00",
            "end": "2026-07-17T00:00:00+00:00",
        },
    }
    result = normalize_credits_config(config)
    assert result["weeklyPercent"] == 12.5
    assert result["product"] == "GrokBuild"


def test_normalize_falls_back_to_credit_usage_percent() -> None:
    config = {
        "creditUsagePercent": 7.0,
        "productUsage": [{"product": "Api"}, {"product": "GrokChat"}],
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-08T00:00:00+00:00",
        },
    }
    result = normalize_credits_config(config)
    assert result["available"] is True
    assert result["weeklyPercent"] == 7.0
    assert result["product"] == "GrokBuild"


def test_normalize_period_type_monthly() -> None:
    result = normalize_credits_config(
        {
            "creditUsagePercent": 1.0,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_MONTHLY",
                "start": "2026-07-01T00:00:00+00:00",
                "end": "2026-08-01T00:00:00+00:00",
            },
        }
    )
    assert result["periodType"] == "MONTHLY"
    assert result["weeklyPercent"] == 1.0


def test_normalize_billing_period_fallback() -> None:
    result = normalize_credits_config(
        {
            "creditUsagePercent": 3.0,
            "billingPeriodStart": "2026-07-10T00:00:00+00:00",
            "billingPeriodEnd": "2026-07-17T00:00:00+00:00",
        }
    )
    assert result["periodStart"] == "2026-07-10T00:00:00+00:00"
    assert result["periodEnd"] == "2026-07-17T00:00:00+00:00"
    assert result["periodType"] is None


def test_normalize_clamps_percent() -> None:
    assert normalize_credits_config({"creditUsagePercent": 150})["weeklyPercent"] == 100.0
    assert normalize_credits_config({"creditUsagePercent": -2})["weeklyPercent"] == 0.0


def test_normalize_zero_percent_available() -> None:
    result = normalize_credits_config({"creditUsagePercent": 0})
    assert result["available"] is True
    assert result["weeklyPercent"] == 0.0


def test_normalize_invalid() -> None:
    assert normalize_credits_config(None)["available"] is False
    assert normalize_credits_config({})["available"] is False
    assert "error" in normalize_credits_config({})
    assert normalize_credits_config({})["error"]


def test_clear_caches_noop() -> None:
    clear_caches()


def _fake_jwt(payload_marker: str = "payload") -> str:
    # Three base64url-ish segments; not a real JWT, just shape for the helper.
    return f"eyJhbGciOiJSUzI1NiJ9.{payload_marker}.sig-part-long-enough-for-checks"


def test_candidate_prefers_access_token_over_key() -> None:
    access = _fake_jwt("access")
    key = _fake_jwt("keyval")
    assert len(access) > 40 and access.count(".") == 2
    assert _candidate_access_token({"access_token": access, "key": key}) == access


def test_candidate_falls_back_to_key() -> None:
    key = _fake_jwt("onlykey")
    assert _candidate_access_token({"key": key}) == key
    assert _candidate_access_token({"access_token": None, "key": key}) == key


def test_candidate_rejects_non_jwt() -> None:
    assert _candidate_access_token({"key": "not-a-jwt"}) is None
    assert _candidate_access_token({"access_token": "a.b"}) is None  # too short / 1 dot count ok but len
    assert _candidate_access_token({"key": "a.b.c"}) is None  # len <= 40
    assert _candidate_access_token({}) is None
    assert _candidate_access_token({"key": 123}) is None


def test_entry_expired_missing_is_false() -> None:
    assert _entry_expired({}) is False
    assert _entry_expired({"expires_at": None}) is False
    assert _entry_expired({"expires_at": ""}) is False


def test_entry_expired_iso_past_and_future() -> None:
    assert _entry_expired({"expires_at": "2000-01-01T00:00:00Z"}) is True
    assert _entry_expired({"expires_at": "2099-01-01T00:00:00+00:00"}) is False


def test_parse_expires_at_epoch_iso_z() -> None:
    epoch = _parse_expires_at_epoch("2026-07-11T00:00:00Z")
    assert epoch is not None
    assert abs(epoch - _parse_expires_at_epoch("2026-07-11T00:00:00+00:00")) < 0.001
