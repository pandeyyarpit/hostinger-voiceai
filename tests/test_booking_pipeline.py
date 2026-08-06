"""Offline regression tests for the booking, notification, and CRM pipeline."""

import asyncio
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import calendar_tools
import db
import notify


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("request failed", request=None, response=None)

    def json(self):
        return self._payload


class CalendarToolsTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_spelled_email(self):
        email = calendar_tools.normalize_email("A R P I T 1 2 3@gmail.com")
        self.assertEqual(email, "arpit123@gmail.com")
        self.assertTrue(calendar_tools.is_valid_email(email))
        self.assertFalse(calendar_tools.is_valid_email("arpit123@gmail"))

    def test_ist_day_range_is_sent_to_calcom_in_utc(self):
        self.assertEqual(
            calendar_tools._ist_day_range_as_utc("2026-08-04"),
            ("2026-08-03T18:30:00Z", "2026-08-04T18:29:59Z"),
        )

    def test_slot_lookup_uses_slots_contract(self):
        captured = {}

        def fake_get(*_args, **kwargs):
            captured.update(kwargs)
            return FakeResponse({"data": {"2026-08-04": [{"start": "2026-08-04T12:45:00+05:30"}]}})

        with patch.object(calendar_tools.requests, "get", fake_get):
            slots = calendar_tools._get_slots_calcom("2026-08-04")

        self.assertEqual(slots[0]["label"], "12:45 PM")
        self.assertEqual(captured["params"]["timeZone"], "Asia/Kolkata")
        self.assertEqual(captured["params"]["start"], "2026-08-03T18:30:00Z")
        self.assertEqual(captured["headers"]["cal-api-version"], "2024-09-04")

    async def test_stale_slot_never_reaches_booking_api(self):
        with patch.object(calendar_tools, "is_slot_available", return_value=(False, [])):
            result = await calendar_tools._create_booking_calcom(
                "2026-08-04T13:45:00+05:30", "Test", "+919315085245", "test@example.com", ""
            )

        self.assertFalse(result["success"])
        self.assertIn("no longer available", result["message"])

    async def test_timeout_reconciles_existing_booking(self):
        class TimeoutThenFoundClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                raise httpx.TimeoutException("simulated timeout")

            async def get(self, *_args, **_kwargs):
                return FakeResponse({"data": [{
                    "uid": "created-despite-timeout",
                    "start": "2026-08-04T07:15:00Z",
                    "attendees": [{"email": "test@example.com"}],
                }]})

        with (
            patch.object(calendar_tools, "is_slot_available", return_value=(True, [])),
            patch.object(calendar_tools, "get_cal_creds", return_value={"api_key": "test", "event_id": 1}),
            patch.object(calendar_tools.httpx, "AsyncClient", TimeoutThenFoundClient),
        ):
            result = await calendar_tools._create_booking_calcom(
                "2026-08-04T12:45:00+05:30", "Test", "+919315085245", "test@example.com", ""
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["booking_id"], "created-despite-timeout")

    async def test_booking_uses_current_calcom_contract(self):
        captured = {}

        class SuccessfulClient:
            def __init__(self, timeout):
                captured["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **kwargs):
                captured.update(kwargs)
                return FakeResponse({"data": {"uid": "booking-123"}}, status_code=201)

        with (
            patch.object(calendar_tools, "is_slot_available", return_value=(True, [])),
            patch.object(calendar_tools, "get_cal_creds", return_value={"api_key": "test", "event_id": 1}),
            patch.object(calendar_tools.httpx, "AsyncClient", SuccessfulClient),
        ):
            result = await calendar_tools._create_booking_calcom(
                "2026-08-04T12:45:00+05:30", "Test", "+919315085245", "test@example.com", ""
            )

        self.assertTrue(result["success"])
        self.assertEqual(captured["timeout"], 60.0)
        self.assertEqual(captured["headers"]["cal-api-version"], "2026-02-25")
        self.assertEqual(captured["json"]["start"], "2026-08-04T07:15:00Z")

    async def test_calcom_validation_error_is_not_confirmed(self):
        class ValidationErrorClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                return FakeResponse({"error": {"message": "email_validation_error"}}, status_code=400)

        with (
            patch.object(calendar_tools, "is_slot_available", return_value=(True, [])),
            patch.object(calendar_tools, "get_cal_creds", return_value={"api_key": "test", "event_id": 1}),
            patch.object(calendar_tools.httpx, "AsyncClient", ValidationErrorClient),
        ):
            result = await calendar_tools._create_booking_calcom(
                "2026-08-04T12:45:00+05:30", "Test", "+919315085245", "test@example.com", ""
            )

        self.assertFalse(result["success"])
        self.assertIn("email_validation_error", result["message"])

    def test_calcom_empty_text_error_uses_json_message(self):
        response = FakeResponse({"error": {"message": "slot already booked"}}, status_code=400)
        self.assertEqual(calendar_tools._cal_error_message(response), "slot already booked")

    async def test_calcom_outage_is_not_confirmed(self):
        class OfflineClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                raise httpx.ConnectError("simulated Cal.com outage")

        with (
            patch.object(calendar_tools, "is_slot_available", return_value=(True, [])),
            patch.object(calendar_tools, "get_cal_creds", return_value={"api_key": "test", "event_id": 1}),
            patch.object(calendar_tools.httpx, "AsyncClient", OfflineClient),
        ):
            result = await calendar_tools._create_booking_calcom(
                "2026-08-04T12:45:00+05:30", "Test", "+919315085245", "test@example.com", ""
            )

        self.assertFalse(result["success"])
        self.assertIn("outage", result["message"])

    async def test_simultaneous_same_slot_requests_have_one_winner(self):
        class AtomicCalClient:
            claimed = False
            lock = asyncio.Lock()

            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                async with self.lock:
                    if self.claimed:
                        return FakeResponse({"error": "slot already booked"}, status_code=400)
                    self.__class__.claimed = True
                    return FakeResponse({"data": {"uid": "only-winner"}}, status_code=201)

        AtomicCalClient.claimed = False
        with (
            patch.object(calendar_tools, "is_slot_available", return_value=(True, [])),
            patch.object(calendar_tools, "get_cal_creds", return_value={"api_key": "test", "event_id": 1}),
            patch.object(calendar_tools.httpx, "AsyncClient", AtomicCalClient),
        ):
            results = await asyncio.gather(*[
                calendar_tools._create_booking_calcom(
                    "2026-08-04T12:45:00+05:30", "Test", "+919315085245", "test@example.com", ""
                )
                for _ in range(2)
            ])

        self.assertEqual(sum(result["success"] for result in results), 1)
        self.assertEqual(sum(not result["success"] for result in results), 1)


class NotificationTests(unittest.TestCase):
    def test_failed_booking_sends_actionable_alert(self):
        with patch.object(notify, "send_telegram", return_value=True) as send:
            sent = notify.notify_booking_failed(
                "Test Caller", "+919315085245", "2026-08-04T12:45:00+05:30", "Slot unavailable"
            )

        self.assertTrue(sent)
        self.assertIn("Booking Failed", send.call_args.args[0])
        self.assertIn("Slot unavailable", send.call_args.args[0])

    def test_telegram_outage_returns_failure_without_raising(self):
        with (
            patch.object(notify, "TELEGRAM_BOT_TOKEN", "test-token"),
            patch.object(notify, "TELEGRAM_CHAT_ID", "test-chat"),
            patch.object(notify.requests, "post", side_effect=notify.requests.RequestException("simulated outage")),
        ):
            self.assertFalse(notify.send_telegram("test alert"))

    def test_agent_error_alert_contains_call_identifier(self):
        with patch.object(notify, "send_telegram", return_value=True) as send:
            sent = notify.notify_agent_error("+919315085245", "CRM call-log save failed")

        self.assertTrue(sent)
        self.assertIn("Agent Error", send.call_args.args[0])
        self.assertIn("+919315085245", send.call_args.args[0])


class SupabaseReliabilityTests(unittest.TestCase):
    def _save(self, **overrides):
        data = {
            "phone": "+919315085245",
            "duration": 30,
            "transcript": "[USER] Test",
            "summary": "Booking Confirmed: test",
            "was_booked": True,
        }
        data.update(overrides)
        return db.save_call_log(**data)

    def test_schema_mismatch_falls_back_to_base_columns(self):
        class Table:
            inserts = []

            def insert(self, data):
                self.__class__.inserts.append(data)
                return self

            def execute(self):
                if len(self.__class__.inserts) == 1:
                    raise Exception("PGRST204 column was not found in schema cache")
                return type("Result", (), {"data": [{"id": "crm-row"}]})()

        class Supabase:
            def table(self, name):
                self.name = name
                return Table()

        Table.inserts = []
        with (
            patch.dict(os.environ, {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_KEY": "test"}),
            patch.object(db, "get_supabase", return_value=Supabase()),
        ):
            result = self._save()

        self.assertTrue(result["success"])
        self.assertEqual(len(Table.inserts), 2)
        self.assertIn("was_booked", Table.inserts[0])
        self.assertNotIn("was_booked", Table.inserts[1])
        self.assertEqual(Table.inserts[1]["summary"], "Booking Confirmed: test")

    def test_supabase_outage_returns_a_clear_failure(self):
        class Table:
            def insert(self, _data):
                return self

            def execute(self):
                raise Exception("simulated connection outage")

        class Supabase:
            def table(self, _name):
                return Table()

        with (
            patch.dict(os.environ, {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_KEY": "test"}),
            patch.object(db, "get_supabase", return_value=Supabase()),
            patch.object(db, "_MAX_RETRIES", 1),
        ):
            result = self._save()

        self.assertFalse(result["success"])
        self.assertIn("connection outage", result["message"])


class AgentAndUiRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_booking_tool_confirms_then_waits_for_conversation_close(self):
        from agent import AgentTools

        tools = AgentTools(caller_phone="+919315085245")
        created = {"success": True, "booking_id": "live-booking", "message": "Booking confirmed"}
        scheduled_task = MagicMock()

        def capture_scheduled_task(coro):
            # This test checks scheduling only; close the coroutine rather than
            # letting a mocked create_task leave it unawaited.
            coro.close()
            return scheduled_task

        with (
            patch("agent.is_slot_available", return_value=(True, [])),
            patch("agent.async_create_booking", new=AsyncMock(return_value=created)) as create,
            patch("agent.notify_booking_confirmed", return_value=True) as notify,
            patch("agent.asyncio.create_task", side_effect=capture_scheduled_task),
        ):
            reply = await tools.save_booking_intent(
                "2026-08-04T15:30:00+05:30", "Rahul", caller_email="Rahul123@gmail.com"
            )

        create.assert_awaited_once()
        notify.assert_called_once()
        self.assertTrue(tools.booking_result["success"])
        self.assertEqual(tools.booking_intent["caller_email"], "rahul123@gmail.com")
        self.assertTrue(tools.booking_notification_sent)
        self.assertIn("confirmed in Cal.com", reply)
        self.assertIn("anything else", reply)
        self.assertIsNone(tools._end_task)
        self.assertIs(tools._closure_idle_task, scheduled_task)

    async def test_booking_tool_failure_does_not_start_hangup_or_claim_success(self):
        from agent import AgentTools

        tools = AgentTools(caller_phone="+919315085245")
        with (
            patch("agent.is_slot_available", side_effect=[(True, []), (False, ["3:45 PM"])]),
            patch(
                "agent.async_create_booking",
                new=AsyncMock(return_value={"success": False, "booking_id": None, "message": "slot already booked"}),
            ),
            patch("agent.notify_booking_confirmed") as notify,
            patch("agent.asyncio.create_task") as create_task,
        ):
            reply = await tools.save_booking_intent(
                "2026-08-04T15:30:00+05:30", "Rahul", caller_email="rahul123@gmail.com"
            )

        self.assertIsNone(tools.booking_intent)
        self.assertIsNotNone(tools.last_booking_failure)
        notify.assert_not_called()
        create_task.assert_not_called()
        self.assertIn("not created", reply)

    async def test_automatic_termination_deletes_room(self):
        from agent import AgentTools

        class FakeJobContext:
            deleted_room = None

            async def delete_room(self, room_name):
                self.deleted_room = room_name

        tools = AgentTools(caller_phone="+919315085245")
        tools.room_name = "test-room"
        tools.job_ctx = FakeJobContext()

        self.assertTrue(await tools._terminate_call())
        self.assertEqual(tools.job_ctx.deleted_room, "test-room")

    async def test_explicit_end_call_waits_five_seconds_then_deletes_room(self):
        from agent import AgentTools

        class FakeJobContext:
            deleted_room = None

            async def delete_room(self, room_name):
                self.deleted_room = room_name

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        tools = AgentTools(caller_phone="+919315085245")
        tools.room_name = "test-goodbye-room"
        tools.job_ctx = FakeJobContext()
        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            task = MagicMock()
            task.done.return_value = False
            return task

        with patch("agent.asyncio.create_task", side_effect=capture_task):
            reply = await tools.end_call()

        self.assertIsNone(tools.job_ctx.deleted_room)
        self.assertIn("Goodbye", reply)
        self.assertEqual(len(scheduled), 1)

        with patch("agent.asyncio.sleep", fake_sleep):
            await scheduled[0]

        self.assertEqual(sleep_calls, [5])
        self.assertEqual(tools.job_ctx.deleted_room, "test-goodbye-room")

    async def test_closing_idle_fallback_says_goodbye_then_deletes_room(self):
        from agent import AgentTools

        class FakeJobContext:
            deleted_room = None

            async def delete_room(self, room_name):
                self.deleted_room = room_name

        class FakeSession:
            instructions = None

            async def generate_reply(self, *, instructions):
                self.instructions = instructions

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        tools = AgentTools(caller_phone="+919315085245")
        tools.room_name = "test-idle-room"
        tools.job_ctx = FakeJobContext()
        tools.session = FakeSession()

        with patch("agent.asyncio.sleep", fake_sleep):
            await tools._close_if_no_reply(20)

        self.assertEqual(sleep_calls, [20, 5])
        self.assertIn("Goodbye", tools.session.instructions)
        self.assertEqual(tools.job_ctx.deleted_room, "test-idle-room")

    def test_caller_activity_cancels_closing_idle_timer(self):
        from agent import AgentTools

        tools = AgentTools(caller_phone="+919315085245")
        timer = MagicMock()
        timer.done.return_value = False
        tools._closure_idle_task = timer

        tools.cancel_closure_idle()

        timer.cancel.assert_called_once()
        self.assertIsNone(tools._closure_idle_task)

    def test_maximum_turn_limit_schedules_a_goodbye_hangup(self):
        source = Path("agent.py").read_text()
        limit_at = source.index("if turn_count >= max_turns:")
        schedule_at = source.index('_schedule_end_after_goodbye("maximum turn limit")', limit_at)
        self.assertGreater(schedule_at, limit_at)

    def test_crm_persistence_precedes_optional_shutdown_work(self):
        source = Path("agent.py").read_text()
        save_at = source.index("save_result = save_call_log(")
        sentiment_at = source.index("# Sentiment analysis (#14)", save_at)
        recording_at = source.index("# Stop recording", save_at)
        self.assertLess(save_at, sentiment_at)
        self.assertLess(sentiment_at, recording_at)

    def test_crm_failure_triggers_an_agent_alert(self):
        source = Path("agent.py").read_text()
        crm_save_at = source.index("save_result = save_call_log(")
        crm_alert_at = source.index("CRM call-log save failed", crm_save_at)
        self.assertGreater(crm_alert_at, crm_save_at)

    def test_dashboard_polls_for_live_updates(self):
        source = Path("ui_server.py").read_text()
        self.assertIn("setInterval(refreshLiveData, 10000)", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
