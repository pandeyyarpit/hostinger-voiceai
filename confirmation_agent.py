"""Separate LiveKit worker for appointment-confirmation calls."""

import asyncio
import json
import logging
import os
from datetime import datetime

import certifi
from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, RoomInputOptions, WorkerOptions, cli, llm
from livekit.plugins import openai, sarvam

from confirmation_store import get_settings
from notify import send_telegram

load_dotenv()
os.environ["SSL_CERT_FILE"] = certifi.where()

logging.basicConfig(level=logging.INFO)
logging.getLogger("hpack").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("confirmation-agent")


def attach_latency_logging(room_name: str, agent_stt, agent_llm, agent_tts) -> None:
    """Write per-stage inference timings to the worker log without caller content."""

    def log_metric(stage: str, metric) -> None:
        logger.info(
            "[LATENCY] room=%s stage=%s stt=%.2fs llm_ttft=%.2fs tts_ttfb=%.2fs total=%.2fs",
            room_name,
            stage,
            getattr(metric, "duration", 0.0) if stage == "stt" else 0.0,
            getattr(metric, "ttft", 0.0),
            getattr(metric, "ttfb", 0.0),
            getattr(metric, "duration", 0.0),
        )

    agent_stt.on("metrics_collected", lambda metric: log_metric("stt", metric))
    agent_llm.on("metrics_collected", lambda metric: log_metric("llm", metric))
    agent_tts.on("metrics_collected", lambda metric: log_metric("tts", metric))


class ConfirmationTools(llm.ToolContext):
    def __init__(self, job_ctx: JobContext, room_name: str, metadata: dict):
        super().__init__(tools=[])
        self.job_ctx = job_ctx
        self.room_name = room_name
        self.metadata = metadata
        self.outcome: str | None = None
        self._end_task = None

    async def _end_after_five_seconds(self) -> None:
        await asyncio.sleep(5)
        await self.job_ctx.delete_room(self.room_name)

    def _notify_test_outcome(self, outcome: str) -> None:
        if self.metadata.get("test_call"):
            send_telegram(
                f"🧪 *Confirmation Agent Test*\n"
                f"📞 `{self.metadata.get('phone_number', 'unknown')}`\n"
                f"✅ *Outcome:* {outcome}\n"
                f"_No Cal.com appointment was changed._"
            )

    @llm.function_tool(description="Record a clear yes/confirmation from the patient. Use only after they say yes, confirm, or equivalent.")
    async def confirm_appointment(self) -> str:
        self.outcome = "confirmed"
        self._notify_test_outcome("Patient confirmed")
        if not self._end_task or self._end_task.done():
            self._end_task = asyncio.create_task(self._end_after_five_seconds())
        return "Confirmation recorded. Thank the patient, say goodbye, and do not ask another question. The call ends automatically in five seconds."

    @llm.function_tool(description="Record that the patient wants to cancel. Use only after a clear no/cancel response.")
    async def cancel_appointment(self) -> str:
        self.outcome = "cancel_requested"
        if self.metadata.get("test_call"):
            self._notify_test_outcome("Cancellation requested (test only; Cal.com unchanged)")
            reply = "This is a test call, so no appointment was cancelled. Thank the patient and say goodbye."
        else:
            # The production scheduler supplies a Cal.com booking ID and performs
            # the actual cancellation in its audited job handler.
            reply = "Cancellation request recorded. Tell the patient the clinic will confirm the cancellation shortly."
        if not self._end_task or self._end_task.done():
            self._end_task = asyncio.create_task(self._end_after_five_seconds())
        return reply + " The call ends automatically in five seconds."

    @llm.function_tool(description="Record that the patient wants a different appointment time.")
    async def request_reschedule(self) -> str:
        self.outcome = "reschedule_requested"
        self._notify_test_outcome("Reschedule requested")
        if not self._end_task or self._end_task.done():
            self._end_task = asyncio.create_task(self._end_after_five_seconds())
        return "Reschedule request recorded. Tell the patient the clinic will contact them with available times, then say goodbye. The call ends automatically in five seconds."


class ConfirmationAgent(Agent):
    def __init__(self, tools: ConfirmationTools, patient_name: str, appointment_time: str, script: str):
        instructions = (
            "You are Ria from Sahay Health calling to confirm one physiotherapy appointment. "
            "Keep every response to one short sentence. Ask once whether the patient confirms, wants to cancel, or wants to reschedule. "
            "On a clear yes call confirm_appointment. On a clear no/cancel call cancel_appointment. "
            "On reschedule call request_reschedule. Never invent an appointment time or diagnose medical issues."
        )
        self.patient_name = patient_name or "there"
        self.appointment_time = appointment_time or "your upcoming appointment"
        self.script = script
        super().__init__(instructions=instructions, tools=llm.find_function_tools(tools))

    async def on_enter(self):
        greeting = self.script or (
            f"Hi {self.patient_name}, this is Ria from Sahay Health. "
            f"I am calling to confirm {self.appointment_time}. Is that still convenient?"
        )
        greeting = greeting.replace("{{name}}", self.patient_name).replace("{{time}}", self.appointment_time)
        await self.session.generate_reply(instructions=f"Say exactly this phrase: '{greeting}'")


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    try:
        metadata = json.loads(ctx.job.metadata or "{}")
    except json.JSONDecodeError:
        metadata = {}

    settings = get_settings()
    if not settings["enabled"] and not metadata.get("test_call"):
        logger.warning("Confirmation worker rejected call because the master toggle is off.")
        await ctx.delete_room(ctx.room.name)
        return

    voice = settings.get("voice", "kavya")
    language = settings.get("language", "hi-IN")
    tools = ConfirmationTools(ctx, ctx.room.name, metadata)
    agent = ConfirmationAgent(
        tools,
        metadata.get("patient_name", ""),
        metadata.get("appointment_time", ""),
        settings.get("script", ""),
    )
    agent_stt = sarvam.STT(language="unknown", model="saaras:v3", mode="translate", flush_signal=True, sample_rate=16000)
    agent_llm = openai.LLM(model=os.getenv("LLM_MODEL", "gpt-4o-mini"), max_completion_tokens=100)
    agent_tts = sarvam.TTS(target_language_code=language, model="bulbul:v3", speaker=voice, speech_sample_rate=24000)
    session = AgentSession(
        stt=agent_stt,
        llm=agent_llm,
        tts=agent_tts,
        turn_detection="stt",
        min_endpointing_delay=0.3,
        max_endpointing_delay=1.5,
        allow_interruptions=True,
    )
    attach_latency_logging(ctx.room.name, agent_stt, agent_llm, agent_tts)
    await session.start(room=ctx.room, agent=agent, room_input_options=RoomInputOptions(close_on_disconnect=False))

    async def expire_unanswered_call():
        await asyncio.sleep(45)
        if not tools.outcome:
            tools.outcome = "no_answer"
            if metadata.get("test_call"):
                tools._notify_test_outcome("No answer before 45-second timeout")
            await ctx.delete_room(ctx.room.name)

    asyncio.create_task(expire_unanswered_call())
    logger.info("Confirmation session active for %s", metadata.get("phone_number", "unknown"))


if __name__ == "__main__":
    # The inbound worker uses LiveKit's default health/metrics port 8081.
    # This second worker must use a different internal port in the same container.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="confirmation-caller", prometheus_port=8082))
