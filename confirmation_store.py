"""Private Supabase storage for the appointment-confirmation agent."""

import os
import re
from uuid import uuid4
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from supabase import create_client


DEFAULT_SETTINGS = {
    "enabled": False,
    "lead_hours": 24,
    "timezone": "Asia/Kolkata",
    "call_window_start": "09:00:00",
    "call_window_end": "19:00:00",
    "max_attempts": 2,
    "retry_delay_minutes": 120,
    "script": "",
    "voice": "kavya",
    "language": "hi-IN",
}

_SETTING_KEYS = set(DEFAULT_SETTINGS)


def _client():
    """Return a private server-side client; never use the browser anon key here."""
    url = os.environ.get("SUPABASE_URL", "")
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not service_role_key:
        raise RuntimeError("Confirmation storage is not configured on the server.")
    return create_client(url, service_role_key)


def get_settings() -> dict:
    result = _client().table("confirmation_settings").select("*").eq("id", True).single().execute()
    return {**DEFAULT_SETTINGS, **(result.data or {})}


def update_settings(data: dict) -> dict:
    """Validate and persist the non-secret confirmation controls."""
    payload = {key: value for key, value in data.items() if key in _SETTING_KEYS}
    if "lead_hours" in payload:
        payload["lead_hours"] = int(payload["lead_hours"])
        if not 1 <= payload["lead_hours"] <= 720:
            raise ValueError("Lead time must be between 1 and 720 hours.")
    if "max_attempts" in payload:
        payload["max_attempts"] = int(payload["max_attempts"])
        if not 1 <= payload["max_attempts"] <= 5:
            raise ValueError("Attempts must be between 1 and 5.")
    if "retry_delay_minutes" in payload:
        payload["retry_delay_minutes"] = int(payload["retry_delay_minutes"])
        if not 5 <= payload["retry_delay_minutes"] <= 10080:
            raise ValueError("Retry delay must be between 5 and 10080 minutes.")
    if "enabled" in payload:
        payload["enabled"] = bool(payload["enabled"])

    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = _client().table("confirmation_settings").update(payload).eq("id", True).execute()
    if not result.data:
        raise RuntimeError("Confirmation settings record was not found.")
    return {**DEFAULT_SETTINGS, **result.data[0]}


def list_contact_preferences(limit: int = 200) -> list[dict]:
    result = (
        _client()
        .table("contact_preferences")
        .select("patient_phone, confirmation_enabled, opted_out_at, opt_out_reason, updated_at")
        .order("updated_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def set_contact_confirmation(phone: str, enabled: bool, reason: str = "") -> dict:
    clean_phone = (phone or "").strip()
    if not re.fullmatch(r"\+\d{7,15}", clean_phone):
        raise ValueError("Phone number must include country code, for example +919876543210.")

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "patient_phone": clean_phone,
        "confirmation_enabled": bool(enabled),
        "opted_out_at": None if enabled else now,
        "opt_out_reason": "" if enabled else (reason or "Manual dashboard opt-out"),
        "updated_at": now,
    }
    result = _client().table("contact_preferences").upsert(
        payload, on_conflict="patient_phone"
    ).execute()
    if not result.data:
        raise RuntimeError("Contact preference could not be saved.")
    return result.data[0]


def _parse_ist_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{label} must be a valid date and time.") from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return parsed.astimezone(timezone.utc)


def create_manual_appointment(data: dict) -> dict:
    """Create a dashboard-only appointment; it never creates a Cal.com booking."""
    phone = (data.get("patient_phone") or "").strip()
    if not re.fullmatch(r"\+\d{7,15}", phone):
        raise ValueError("Phone number must include country code, for example +919876543210.")

    starts_at = _parse_ist_datetime(data.get("starts_at", ""), "Appointment time")
    confirmation_call_at = data.get("confirmation_call_at")
    call_at = _parse_ist_datetime(confirmation_call_at, "Confirmation call time") if confirmation_call_at else None
    if call_at and call_at >= starts_at:
        raise ValueError("Confirmation call time must be before the appointment.")

    payload = {
        "cal_booking_id": f"manual:{uuid4()}",
        "source": "manual",
        "patient_name": (data.get("patient_name") or "").strip(),
        "patient_phone": phone,
        "patient_email": (data.get("patient_email") or "").strip().lower() or None,
        "starts_at": starts_at.isoformat(),
        "confirmation_call_at": call_at.isoformat() if call_at else None,
        "notes": (data.get("notes") or "").strip(),
    }
    result = _client().table("appointments").insert(payload).execute()
    if not result.data:
        raise RuntimeError("Manual appointment could not be saved.")
    return result.data[0]


def list_manual_appointments(limit: int = 100) -> list[dict]:
    result = (
        _client().table("appointments")
        .select("id, patient_name, patient_phone, starts_at, confirmation_call_at, notes, confirmation_status, created_at")
        .eq("source", "manual")
        .eq("booking_status", "booked")
        .order("starts_at")
        .limit(limit)
        .execute()
    )
    return result.data or []


def delete_manual_appointment(appointment_id: str) -> None:
    existing = _client().table("appointments").select("source").eq("id", appointment_id).single().execute().data
    if not existing:
        raise ValueError("Manual appointment was not found.")
    if existing.get("source") != "manual":
        raise ValueError("Only manually added appointments can be removed here.")
    _client().table("appointments").delete().eq("id", appointment_id).execute()
