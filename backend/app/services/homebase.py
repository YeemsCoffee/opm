"""Homebase API connector.

Homebase (joinhomebase.com) exposes a REST API, but keys are issued by
Homebase support/sales on their Enterprise plan — there is no self-serve
signup and no public MCP server. Configure via environment variables once
you have credentials:

    HOMEBASE_API_KEY        the API key Homebase issues
    HOMEBASE_LOCATION_UUID  your location's UUID
    HOMEBASE_API_BASE       optional, defaults to https://api.joinhomebase.com

The shifts endpoint shape follows Homebase's published docs
(app.joinhomebase.com/api-docs): GET /locations/{uuid}/shifts filtered by
date, Bearer auth, versioned Accept header. Field parsing below is
defensive since the exact payload can only be verified with a live key.
"""

import os
from datetime import date, datetime

import httpx

TIMEOUT_S = 20


def homebase_settings() -> dict:
    return {
        "api_key": os.environ.get("HOMEBASE_API_KEY", ""),
        "location_uuid": os.environ.get("HOMEBASE_LOCATION_UUID", ""),
        "base_url": os.environ.get("HOMEBASE_API_BASE", "https://api.joinhomebase.com").rstrip("/"),
    }


def homebase_configured() -> bool:
    s = homebase_settings()
    return bool(s["api_key"] and s["location_uuid"])


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _shift_to_roster(shift: dict) -> dict | None:
    start_raw = shift.get("start_at") or shift.get("start_time") or shift.get("starts_at")
    end_raw = shift.get("end_at") or shift.get("end_time") or shift.get("ends_at")
    if not start_raw or not end_raw:
        return None
    start = _parse_time(start_raw)
    end = _parse_time(end_raw)
    name = (
        shift.get("name")
        or " ".join(p for p in (shift.get("first_name"), shift.get("last_name")) if p)
        or (shift.get("user") or {}).get("name")
        or "Unknown"
    )
    return {
        "name": name,
        "role": shift.get("role") or shift.get("department") or "",
        "start_min": start.hour * 60 + start.minute,
        "end_min": end.hour * 60 + end.minute,
    }


def fetch_day_roster(day: date) -> list[dict]:
    """Fetch the day's scheduled shifts from Homebase. Raises RuntimeError
    with a readable message on any failure."""
    s = homebase_settings()
    if not homebase_configured():
        raise RuntimeError(
            "Homebase is not configured. Set HOMEBASE_API_KEY and "
            "HOMEBASE_LOCATION_UUID (Homebase issues keys on their Enterprise "
            "plan via support), then restart the server."
        )
    url = f"{s['base_url']}/locations/{s['location_uuid']}/shifts"
    headers = {
        "Authorization": f"Bearer {s['api_key']}",
        "Accept": "application/vnd.homebase-v1+json",
    }
    params = {"start_date": day.isoformat(), "end_date": day.isoformat(), "with_note": "false"}
    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach Homebase: {exc}") from exc
    if resp.status_code == 401:
        raise RuntimeError("Homebase rejected the API key (401). Check HOMEBASE_API_KEY.")
    if resp.status_code == 404:
        raise RuntimeError("Homebase location not found (404). Check HOMEBASE_LOCATION_UUID.")
    if resp.status_code >= 400:
        raise RuntimeError(f"Homebase API error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    shifts = data if isinstance(data, list) else data.get("shifts") or data.get("data") or []
    roster = []
    for shift in shifts:
        parsed = _shift_to_roster(shift)
        if parsed and parsed["end_min"] > parsed["start_min"]:
            roster.append(parsed)
    return roster
