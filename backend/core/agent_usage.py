"""Sanitize Codex usage data before persistence or API exposure."""
from __future__ import annotations

from datetime import datetime, timezone


def _nonnegative(value):
    return value if type(value) is int and value >= 0 else None


def _reset_timestamp(value):
    seconds = _nonnegative(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize_turn_usage(usage: object) -> dict:
    # The SDK's `total` is cumulative for the session. `last` is the most
    # recent turn; each gateway issues one turn per execution attempt.
    last = usage.get("last") if isinstance(usage, dict) else None
    if not isinstance(last, dict):
        return {"usage_status": "unavailable", "usage_source": "codex_sdk_turn"}
    fields = {
        "input_tokens": _nonnegative(last.get("input_tokens")),
        "output_tokens": _nonnegative(last.get("output_tokens")),
        "cached_input_tokens": _nonnegative(last.get("cached_input_tokens")),
        "reasoning_tokens": _nonnegative(last.get("reasoning_output_tokens")),
        "total_tokens": _nonnegative(last.get("total_tokens")),
    }
    if fields["input_tokens"] is None or fields["output_tokens"] is None:
        return {"usage_status": "unavailable", "usage_source": "codex_sdk_turn"}
    if fields["total_tokens"] is None:
        fields["total_tokens"] = fields["input_tokens"] + fields["output_tokens"]
    if fields["total_tokens"] != fields["input_tokens"] + fields["output_tokens"]:
        return {"usage_status": "unavailable", "usage_source": "codex_sdk_turn"}
    if fields["cached_input_tokens"] is not None and fields["cached_input_tokens"] > fields["input_tokens"]:
        return {"usage_status": "unavailable", "usage_source": "codex_sdk_turn"}
    return {**fields, "usage_status": "available", "usage_source": "codex_sdk_turn"}


def normalize_quota(raw: object) -> dict:
    if not isinstance(raw, dict) or not (
        isinstance(raw.get("rateLimits"), dict)
        or isinstance(raw.get("rateLimitsByLimitId"), dict)
    ):
        raise ValueError("Invalid Codex rate-limit response")
    account = raw.get("accountId")
    account_id = account if isinstance(account, str) and account.strip() else None
    buckets = raw.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        if not isinstance(raw.get("rateLimits"), dict):
            buckets = {}
        else:
            buckets = {"legacy": raw["rateLimits"]}
    normalized = []
    for key, bucket in buckets.items():
        if not isinstance(key, str) or not isinstance(bucket, dict):
            continue
        windows = []
        non_window_fields = {"credits", "individualLimit", "limitId", "limitName",
                             "normalModelSlug", "planType", "rateLimitReachedType",
                             "spendControlReached"}
        for name, window in bucket.items():
            if name in non_window_fields or not isinstance(name, str):
                continue
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            used = used if type(used) is int and 0 <= used <= 100 else None
            duration = _nonnegative(window.get("windowDurationMins"))
            reset_at = _reset_timestamp(window.get("resetsAt"))
            windows.append({"name": name, "used_percent": used,
                            "remaining_percent": 100 - used if used is not None else None,
                            "duration_minutes": duration,
                            "reset_at": reset_at,
                            "status": "available" if used is not None else "unavailable"})
        credits = bucket.get("credits")
        credit_values = ({"balance": credits.get("balance") if isinstance(credits.get("balance"), str)
                          and credits.get("balance").strip() else None,
                          "has_credits": credits.get("hasCredits") if type(credits.get("hasCredits")) is bool else None,
                          "unlimited": credits.get("unlimited") if type(credits.get("unlimited")) is bool else None}
                         if isinstance(credits, dict) else None)
        normalized.append({"limit_id": key if key != "legacy" else bucket.get("limitId"),
                           "limit_name": bucket.get("limitName") if isinstance(bucket.get("limitName"), str) else None,
                           "model_slug": bucket.get("normalModelSlug") if isinstance(bucket.get("normalModelSlug"), str) else None,
                           "plan_type": bucket.get("planType") if isinstance(bucket.get("planType"), str) else None,
                           "windows": windows,
                           "credits": credit_values if credit_values and
                                      any(value is not None for value in credit_values.values()) else None})
    if not normalized and not isinstance(raw.get("rateLimits"), dict):
        raise ValueError("Invalid Codex rate-limit buckets")
    reset_credits = raw.get("rateLimitResetCredits")
    count = _nonnegative(reset_credits.get("availableCount")) if isinstance(reset_credits, dict) else None
    allowed = raw.get("ordinaryUsageAllowed")
    return {"account_id": account_id, "buckets": normalized,
            "ordinary_usage_allowed": allowed if type(allowed) is bool else None,
            "available_reset_credits": count}
