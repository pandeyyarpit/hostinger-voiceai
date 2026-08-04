import os
import logging
import re
import requests
import httpx
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("calendar-tools")

CAL_BASE    = "https://api.cal.com/v1"
CAL_V2_BASE = "https://api.cal.com/v2"
# Cal.com currently versions the Slots and Bookings endpoints separately.
CAL_SLOTS_API_VERSION = "2024-09-04"
CAL_BOOKINGS_API_VERSION = "2026-02-25"
IST = timezone(timedelta(hours=5, minutes=30))
EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def get_cal_creds() -> dict:
    return {
        "api_key":  os.environ.get("CAL_API_KEY", ""),
        "event_id": int(os.environ.get("CAL_EVENT_TYPE_ID", "0") or "0"),
    }


def normalize_email(email: str) -> str:
    """Convert a spelled-out email captured by speech-to-text into a usable value."""
    return re.sub(r"\s+", "", (email or "")).lower()


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch(email))


def _to_utc_iso(value: datetime) -> str:
    """Format an aware datetime as the UTC timestamp required by Cal.com v2."""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ist_day_range_as_utc(date_str: str) -> tuple[str, str]:
    """Return a calendar day in IST as the UTC range required by Cal.com slots."""
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    day_end = day_start + timedelta(days=1) - timedelta(seconds=1)
    return _to_utc_iso(day_start), _to_utc_iso(day_end)


def _cal_error_message(resp) -> str:
    """Return a useful Cal.com error even when its plain-text body is empty."""
    if getattr(resp, "text", ""):
        return resp.text
    try:
        body = resp.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            return error.get("message") or error.get("code") or str(body)
        return str(error or body)
    except Exception:
        return f"Cal.com returned HTTP {getattr(resp, 'status_code', 'unknown')}"


# ─── Cal.com: Get available slots ─────────────────────────────────────────────

def get_available_slots(date_str: str) -> list:
    """
    Fetch open slots for a given date from Cal.com OR Google Calendar,
    depending on which is configured.
    date_str: "YYYY-MM-DD"
    """
    # Try Google Calendar first if configured (#36)
    gcal_id = os.environ.get("GOOGLE_CALENDAR_ID", "")
    gcal_creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "google_creds.json")
    if gcal_id and os.path.exists(gcal_creds):
        try:
            return _get_slots_gcal(date_str, gcal_id, gcal_creds)
        except Exception as e:
            logger.warning(f"[GCAL] Falling back to Cal.com: {e}")

    # Default: Cal.com
    return _get_slots_calcom(date_str)


def _get_slots_calcom(date_str: str) -> list:
    creds = get_cal_creds()
    try:
        start_utc, end_utc = _ist_day_range_as_utc(date_str)
        resp = requests.get(
            f"{CAL_V2_BASE}/slots",
            headers={
                "Authorization":  f"Bearer {creds['api_key']}",
                "cal-api-version": CAL_SLOTS_API_VERSION,
                "Content-Type":   "application/json",
            },
            params={
                "eventTypeId": creds["event_id"],
                "start":       start_utc,
                "end":         end_utc,
                "timeZone":    "Asia/Kolkata",
            },
            timeout=8,
        )
        resp.raise_for_status()
        body = resp.json()
        # v2 response: {"data": {"YYYY-MM-DD": [{"start": "..."}, ...]}}
        raw_slots = body.get("data", {}).get(date_str, [])
        slots = []
        for s in raw_slots:
            slot_time = s.get("start") or s.get("time", "")
            if not slot_time:
                continue
            dt = datetime.fromisoformat(slot_time)
            # Convert UTC to IST for display
            dt_ist = dt.astimezone(IST)
            slots.append({"time": slot_time, "label": dt_ist.strftime("%-I:%M %p")})
        logger.info(f"[CAL] {len(slots)} slots for {date_str}")
        return slots
    except Exception as e:
        logger.error(f"[CAL] get_available_slots error: {e}")
        return []


def is_slot_available(start_time: str) -> tuple[bool, list[str]]:
    """Return whether an ISO appointment time exactly matches a Cal.com slot."""
    try:
        requested = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        if requested.tzinfo is None:
            logger.warning("[CAL] Appointment time has no timezone: %s", start_time)
            return False, []

        requested_ist = requested.astimezone(IST)
        slots = get_available_slots(requested_ist.date().isoformat())

        labels = []
        for slot in slots:
            slot_time = slot.get("time", "")
            if not slot_time:
                continue
            candidate = datetime.fromisoformat(slot_time.replace("Z", "+00:00"))
            labels.append(slot.get("label", candidate.astimezone(IST).strftime("%-I:%M %p")))
            if candidate == requested:
                return True, labels

        logger.info("[CAL] Requested slot is unavailable: %s", start_time)
        return False, labels
    except (TypeError, ValueError) as e:
        logger.warning("[CAL] Invalid appointment time %r: %s", start_time, e)
        return False, []




def _get_slots_gcal(date_str: str, calendar_id: str, creds_file: str) -> list:
    """
    Fetch busy slots from Google Calendar and compute free windows (#36).
    Requires: google-api-python-client, google-auth
    """
    from googleapiclient.discovery import build
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        creds_file,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    service = build("calendar", "v3", credentials=creds)

    start = f"{date_str}T00:00:00+05:30"
    end   = f"{date_str}T23:59:59+05:30"

    result = service.freebusy().query(body={
        "timeMin": start,
        "timeMax": end,
        "items":   [{"id": calendar_id}],
    }).execute()

    busy_slots = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])

    # Generate free 30-min slots between 10:00 and 19:00 IST
    import pytz
    from datetime import timedelta
    ist = pytz.timezone("Asia/Kolkata")
    day_start = ist.localize(datetime.strptime(f"{date_str} 10:00", "%Y-%m-%d %H:%M"))
    day_end   = ist.localize(datetime.strptime(f"{date_str} 19:00", "%Y-%m-%d %H:%M"))

    busy_ranges = []
    for b in busy_slots:
        bs = datetime.fromisoformat(b["start"]).astimezone(ist)
        be = datetime.fromisoformat(b["end"]).astimezone(ist)
        busy_ranges.append((bs, be))

    free_slots = []
    slot = day_start
    while slot < day_end:
        slot_end = slot + timedelta(minutes=30)
        is_busy = any(bs <= slot < be for bs, be in busy_ranges)
        if not is_busy:
            free_slots.append({
                "time":  slot.isoformat(),
                "label": slot.strftime("%-I:%M %p"),
            })
        slot = slot_end

    logger.info(f"[GCAL] {len(free_slots)} free slots for {date_str}")
    return free_slots


# ─── Create a booking ──────────────────────────────────────────────────────────

def create_booking(
    start_time: str,
    caller_name: str,
    caller_phone: str,
    caller_email: str = "",
    notes: str = "",
) -> dict:
    """Synchronous wrapper — calls async_create_booking."""
    import asyncio
    try:
        return asyncio.get_event_loop().run_until_complete(
            async_create_booking(start_time, caller_name, caller_phone, caller_email, notes)
        )
    except RuntimeError:
        return asyncio.run(async_create_booking(start_time, caller_name, caller_phone, caller_email, notes))


async def async_create_booking(
    start_time: str,
    caller_name: str,
    caller_phone: str,
    caller_email: str = "",
    notes: str = "",
) -> dict:
    """
    Book a slot — uses Google Calendar if configured, else Cal.com v2.
    start_time: ISO 8601 with IST offset e.g. "2026-02-24T10:00:00+05:30"
    Returns: {"success": bool, "booking_id": str|None, "message": str}
    """
    gcal_id    = os.environ.get("GOOGLE_CALENDAR_ID", "")
    gcal_creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "google_creds.json")

    if gcal_id and os.path.exists(gcal_creds):
        return await _create_booking_gcal(start_time, caller_name, caller_phone, notes, gcal_id, gcal_creds)

    return await _create_booking_calcom(start_time, caller_name, caller_phone, caller_email, notes)


async def _create_booking_calcom(
    start_time: str, caller_name: str, caller_phone: str, caller_email: str, notes: str
) -> dict:
    creds = get_cal_creds()
    normalized_email = normalize_email(caller_email)
    if normalized_email and not is_valid_email(normalized_email):
        logger.warning("[CAL] Refusing booking with invalid email address")
        return {
            "success": False,
            "booking_id": None,
            "message": "The caller's email address is invalid. Please collect and verify it again.",
        }

    # Use the real email provided by caller; fall back to phone-based placeholder only if blank
    email = normalized_email or f"{caller_phone.replace('+','').replace(' ','')}@voiceagent.placeholder"
    try:
        booking_start = _to_utc_iso(datetime.fromisoformat(start_time.replace("Z", "+00:00")))
    except ValueError:
        return {"success": False, "booking_id": None, "message": "Invalid appointment time."}

    # Recheck immediately before creating the booking. The earlier check occurs
    # while collecting caller details, so its result can become stale.
    available, _ = is_slot_available(start_time)
    if not available:
        return {
            "success": False,
            "booking_id": None,
            "message": "The selected slot is no longer available. Please choose another time.",
        }

    logger.info(f"[CAL] Booking with email={email}, start={booking_start}")
    payload = {
        "eventTypeId": creds["event_id"],
        "start": booking_start,
        "attendee": {
            "name":        caller_name,
            "email":       email,
            "phoneNumber": caller_phone,
            "timeZone":    "Asia/Kolkata",
            "language":    "en",
        },
    }
    # Only include bookingFieldsResponses if notes exist; unrecognized fields cause 400
    if notes:
        payload["bookingFieldsResponses"] = {"notes": notes}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.cal.com/v2/bookings",
                headers={
                    "Authorization":  f"Bearer {creds['api_key']}",
                    "cal-api-version": CAL_BOOKINGS_API_VERSION,
                    "Content-Type":   "application/json",
                },
                json=payload,
            )
            if resp.status_code not in (200, 201):
                error_message = _cal_error_message(resp)
                logger.error(f"[CAL] Booking failed {resp.status_code}: {error_message}")
                return {"success": False, "booking_id": None, "message": error_message}
            uid = resp.json().get("data", {}).get("uid", "unknown")
            logger.info(f"[CAL] Booking created: uid={uid}")
            return {"success": True, "booking_id": uid, "message": "Booking confirmed"}
    except httpx.TimeoutException:
        # A timeout is ambiguous: Cal.com may have completed the booking after the
        # client stopped waiting. Reconcile before reporting a failure or retrying.
        booking_id = await _find_booking_after_timeout(booking_start, creds["event_id"], email)
        if booking_id:
            logger.info(f"[CAL] Booking confirmed after timeout: uid={booking_id}")
            return {"success": True, "booking_id": booking_id, "message": "Booking confirmed after timeout"}
        return {"success": False, "booking_id": None, "message": "Booking timed out and could not be confirmed."}
    except Exception as e:
        logger.error(f"[CAL] Booking error: {e}")
        return {"success": False, "booking_id": None, "message": str(e)}


async def _find_booking_after_timeout(booking_start: str, event_type_id: int, attendee_email: str) -> str | None:
    """Check whether Cal.com created a booking despite a timed-out POST response."""
    target = datetime.fromisoformat(booking_start.replace("Z", "+00:00"))
    params = {
        "eventTypeId": event_type_id,
        "afterStart": _to_utc_iso(target - timedelta(minutes=1)),
        "beforeEnd": _to_utc_iso(target + timedelta(hours=4)),
    }
    headers = {
        "Authorization": f"Bearer {get_cal_creds()['api_key']}",
        "cal-api-version": CAL_BOOKINGS_API_VERSION,
    }

    # Cal.com can finish processing immediately after the POST deadline, so
    # check a few times rather than issuing another create request.
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{CAL_V2_BASE}/bookings", headers=headers, params=params)
                resp.raise_for_status()
                bookings = resp.json().get("data", [])
                if isinstance(bookings, dict):
                    bookings = bookings.get("bookings", [])

                for booking in bookings:
                    existing_start = booking.get("start", "")
                    attendees = booking.get("attendees", []) or []
                    attendee_matches = any(
                        normalize_email(a.get("email", "")) == attendee_email for a in attendees
                    )
                    if existing_start and attendee_matches:
                        existing = datetime.fromisoformat(existing_start.replace("Z", "+00:00"))
                        if existing == target:
                            return booking.get("uid") or booking.get("id")
        except Exception as e:
            logger.warning(f"[CAL] Timeout reconciliation attempt {attempt + 1} failed: {e}")

        if attempt < 2:
            import asyncio
            await asyncio.sleep(2)
    return None


async def _create_booking_gcal(
    start_time: str,
    caller_name: str,
    caller_phone: str,
    notes: str,
    calendar_id: str,
    creds_file: str,
) -> dict:
    """Create a Google Calendar event (#36)."""
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
        from datetime import timedelta

        creds = service_account.Credentials.from_service_account_file(
            creds_file,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        service = build("calendar", "v3", credentials=creds)

        dt_start = datetime.fromisoformat(start_time)
        dt_end   = dt_start + timedelta(minutes=30)

        event = {
            "summary":     f"Appointment — {caller_name}",
            "description": f"Phone: {caller_phone}\nNotes: {notes}\nBooked via Sahay AI Voice Agent",
            "start":       {"dateTime": dt_start.isoformat(), "timeZone": "Asia/Kolkata"},
            "end":         {"dateTime": dt_end.isoformat(),   "timeZone": "Asia/Kolkata"},
            "attendees":   [{"displayName": caller_name, "comment": caller_phone}],
        }

        created = service.events().insert(calendarId=calendar_id, body=event).execute()
        event_id = created.get("id", "unknown")
        logger.info(f"[GCAL] Event created: id={event_id}")
        return {"success": True, "booking_id": event_id, "message": "Google Calendar event created"}
    except Exception as e:
        logger.error(f"[GCAL] Create booking failed: {e}")
        return {"success": False, "booking_id": None, "message": str(e)}


# ─── Cancel a booking ──────────────────────────────────────────────────────────

def cancel_booking(booking_id: str, reason: str = "Cancelled by caller") -> dict:
    """Cancel a Cal.com booking by UID."""
    creds = get_cal_creds()
    try:
        resp = requests.delete(
            f"{CAL_BASE}/bookings/{booking_id}/cancel?apiKey={creds['api_key']}",
            headers={"Content-Type": "application/json"},
            json={"reason": reason},
            timeout=8,
        )
        resp.raise_for_status()
        logger.info(f"[CAL] Booking cancelled: {booking_id}")
        return {"success": True, "message": "Cancelled successfully"}
    except Exception as e:
        logger.error(f"[CAL] cancel_booking error: {e}")
        return {"success": False, "message": str(e)}
