"""Dispatch a manual, clearly labelled confirmation-agent test call."""

import argparse
import asyncio
import json
import os
import random

from dotenv import load_dotenv
from livekit import api

load_dotenv(".env")


async def main():
    parser = argparse.ArgumentParser(description="Make a confirmation-agent test call.")
    parser.add_argument("--to", required=True, help="Phone number in international format")
    parser.add_argument("--name", default="there", help="Patient name used in the greeting")
    parser.add_argument("--appointment", default="your upcoming physiotherapy appointment", help="Appointment time spoken by the test agent")
    args = parser.parse_args()

    phone = args.to.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        raise SystemExit("Phone number must start with + followed by digits.")

    required = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OUTBOUND_TRUNK_ID"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise SystemExit("Missing .env values: " + ", ".join(missing))

    room_name = f"confirmation-test-{phone[1:]}-{random.randint(1000, 9999)}"
    metadata = {
        "phone_number": phone,
        "patient_name": args.name,
        "appointment_time": args.appointment,
        "test_call": True,
    }
    client = api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        dispatch = await client.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="confirmation-caller", room=room_name, metadata=json.dumps(metadata)
            )
        )
        await client.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=room_name,
                sip_trunk_id=os.environ["OUTBOUND_TRUNK_ID"],
                sip_call_to=phone,
                participant_identity=f"confirmation_{phone[1:]}",
                participant_name=args.name,
            )
        )
        print(f"Confirmation test dispatched: room={room_name} dispatch={dispatch.id}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
