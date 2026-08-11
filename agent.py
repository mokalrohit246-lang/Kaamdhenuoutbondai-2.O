import asyncio
import json
import logging
import os
import ssl
import time
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

try:
    import certifi
    _orig_ssl = ssl.create_default_context
    def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
        if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
            kwargs["cafile"] = certifi.where()
        return _orig_ssl(purpose, **kwargs)
    ssl.create_default_context = _certifi_ssl
except ImportError:
    pass

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools, update_call_status, get_setting
from prompts import build_prompt
from tools import AppointmentTools

load_dotenv(override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":      logger.info(msg)
    elif level == "warning": logger.warning(msg)
    else:                    logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def load_db_settings_to_env() -> None:
    """Load Supabase settings into os.environ ONLY for keys not already set in VPS environment."""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            k = row.get("key")
            v = row.get("value")
            if k and v and not os.getenv(k):
                os.environ[k] = v
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


# ── Import Google plugin paths ───────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


# ── Session factory ──────────────────────────────────────────────────────────

def _build_session(tools: list, system_prompt: str) -> AgentSession:
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"

    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and RealtimeClass is not None:
        logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
        return AgentSession(
            llm=RealtimeClass(
                model=gemini_model,
                voice=gemini_voice,
                instructions=system_prompt,
            ),
            tools=tools,
        )

    if _google_llm is None:
        raise RuntimeError("No Google AI backend. Run: pip install 'livekit-plugins-google>=1.0'")

    logger.info("SESSION MODE: pipeline (Deepgram STT + Gemini LLM + Google TTS)")
    stt = _deepgram_stt(model="nova-3", language="multi") if _deepgram_stt else None
    tts = _google_tts() if _google_tts else None
    return AgentSession(stt=stt, llm=_google_llm(model="gemini-2.0-flash"), tts=tts, vad=silero.VAD.load(), tools=tools)


class OutboundAssistant(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


async def entrypoint(ctx: agents.JobContext) -> None:
    await _log("info", f"Job started — room: {ctx.room.name}")

    call_id: Optional[str] = None
    phone_number: Optional[str] = None
    lead_name = "there"
    business_name = "our company"
    service_type = "our service"
    property_type: Optional[str] = None
    budget: Optional[str] = None
    location: Optional[str] = None
    custom_prompt: Optional[str] = None
    voice_override: Optional[str] = None
    model_override: Optional[str] = None
    tools_override: Optional[str] = None
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    broker_phone: Optional[str] = None
    trunk_id_override: Optional[str] = None

    if ctx.job.metadata:
        try:
            data = json.loads(ctx.job.metadata)
            call_id             = data.get("call_id")
            phone_number        = data.get("phone_number")
            lead_name           = data.get("lead_name", lead_name)
            business_name       = data.get("business_name", business_name)
            service_type        = data.get("service_type", service_type)
            property_type       = data.get("property_type")
            budget              = data.get("budget")
            location            = data.get("location")
            custom_prompt       = data.get("system_prompt")
            voice_override      = data.get("voice_override")
            model_override      = data.get("model_override")
            tools_override      = data.get("tools_override")
            campaign_id         = data.get("campaign_id")
            campaign_name       = data.get("campaign_name")
            broker_phone        = data.get("broker_phone")
            trunk_id_override   = data.get("outbound_trunk_id")
            google_key_override = data.get("google_api_key")
            sip_domain_override = data.get("vobiz_sip_domain")
            if google_key_override:
                os.environ["GOOGLE_API_KEY"] = google_key_override
            if sip_domain_override:
                os.environ["VOBIZ_SIP_DOMAIN"] = sip_domain_override
        except (json.JSONDecodeError, AttributeError):
            await _log("warning", "Invalid JSON in job metadata")

    await _log("info", f"Call job received — call_id={call_id} phone={phone_number} lead={lead_name} biz={business_name}")

    system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                  service_type=service_type, custom_prompt=custom_prompt)
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name)
    tool_ctx.call_id = call_id
    tool_ctx.campaign_id = campaign_id
    tool_ctx.campaign_name = campaign_name
    tool_ctx.broker_phone = broker_phone
    tool_ctx.business_name = business_name
    tool_ctx.service_type = service_type
    tool_ctx.property_type = property_type
    tool_ctx.budget = budget
    tool_ctx.location = location

    if voice_override:
        os.environ["GEMINI_TTS_VOICE"] = voice_override
    if model_override:
        os.environ["GEMINI_MODEL"] = model_override

    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            enabled_tools = await get_enabled_tools()
    else:
        enabled_tools = await get_enabled_tools()

    # ── Connect to LiveKit Room ──────────────────────────────────────────────
    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name}")

    # ── Build and start AI session ───────────────────────────────────────────
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    await _log("info", f"Starting AI session — model={gemini_model}")
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    session = _build_session(tools=active_tools, system_prompt=system_prompt)

    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_options=_RO(input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())),
        )
    else:
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        )

    await session.start(**_session_kwargs)
    await _log("info", "Agent session active in room")

    # ── Dial SIP Participant ─────────────────────────────────────────────────
    call_start_time = None
    if phone_number:
        trunk_id = (
            (trunk_id_override if trunk_id_override and trunk_id_override.startswith("ST_") else "") or
            (os.getenv("OUTBOUND_TRUNK_ID", "").strip() if os.getenv("OUTBOUND_TRUNK_ID", "").strip().startswith("ST_") else "") or
            (await get_setting("OUTBOUND_TRUNK_ID", "")) or
            (trunk_id_override or os.getenv("OUTBOUND_TRUNK_ID", ""))
        ).strip()

        if not trunk_id:
            err_msg = "OUTBOUND_TRUNK_ID not set. Please click '⚡ Create SIP Trunk' in Settings."
            await _log("error", err_msg)
            if call_id:
                await update_call_status(call_id, outcome="failed", reason=err_msg)
            ctx.shutdown()
            return

        await _log("info", f"Dialing {phone_number} via SIP trunk {trunk_id}")
        if call_id:
            await update_call_status(call_id, outcome="ringing", reason="Dialing customer via SIP")

        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
            call_start_time = time.time()
            if call_id:
                await update_call_status(call_id, outcome="in_progress", reason="Call answered by customer")
        except Exception as exc:
            err_msg = f"SIP dial failed: {exc}"
            await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
            if call_id:
                await update_call_status(call_id, outcome="failed", reason=err_msg)
            ctx.shutdown()
            return

        await _log("info", f"Call ANSWERED — {phone_number} picked up, speaking greeting immediately")

        # Set recording URL immediately so it is always present in call log
        _s3_ep = (os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")).rstrip("/")
        _aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "call-recordings")
        if "supabase.co/storage/v1/s3" in _s3_ep:
            _public_ep = _s3_ep.replace("/storage/v1/s3", "/storage/v1/object/public")
            tool_ctx.recording_url = f"{_public_ep}/{_aws_bucket}/recordings/{ctx.room.name}.ogg"
        elif _s3_ep:
            tool_ctx.recording_url = f"{_s3_ep}/{_aws_bucket}/recordings/{ctx.room.name}.ogg"

        # ── Fast Direct Greeting ─────────────────────────────────────────────
        greeting = f"Hi {lead_name}! I am Priya from {business_name}. Am I speaking with {lead_name}?"
        try:
            await session.generate_reply(instructions=greeting)
        except Exception as _gr_exc:
            await _log("warning", f"Greeting notice: {_gr_exc}")

        # Non-blocking background recording
        async def _start_recording_bg():
            _aws_key    = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
            _aws_secret = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
            _s3_region  = os.getenv("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")
            if _aws_key and _aws_secret and _aws_bucket:
                try:
                    _recording_path = f"recordings/{ctx.room.name}.ogg"
                    _egress_req = api.RoomCompositeEgressRequest(
                        room_name=ctx.room.name, audio_only=True,
                        file_outputs=[api.EncodedFileOutput(
                            file_type=api.EncodedFileType.OGG, filepath=_recording_path,
                            s3=api.S3Upload(access_key=_aws_key, secret=_aws_secret,
                                            bucket=_aws_bucket, region=_s3_region, endpoint=_s3_ep),
                        )],
                    )
                    _egress = await ctx.api.egress.start_room_composite_egress(_egress_req)
                    await _log("info", f"Recording egress started: {_egress.egress_id}")
                except Exception as _exc:
                    await _log("warning", f"Recording start notice: {_exc}")

        asyncio.create_task(_start_recording_bg())
    else:
        await session.start(**_session_kwargs)

    # ── Keep session alive until call actually ends ──────────────────────────
    if phone_number:
        _disconnect_event = asyncio.Event()

        def _signal_disconnect(reason=""):
            logger.info("Call disconnect triggered: %s", reason)
            _disconnect_event.set()

        ctx.room.on("participant_disconnected", lambda p: _signal_disconnect(f"participant {p.identity} left"))
        ctx.room.on("disconnected", lambda: _signal_disconnect("room closed"))

        # Monitor active call: once participant joins, detect when they leave
        async def _hangup_watcher():
            has_seen_participant = False
            for _ in range(60):
                if len(ctx.room.remote_participants) > 0:
                    has_seen_participant = True
                    break
                await asyncio.sleep(0.5)

            while not _disconnect_event.is_set():
                await asyncio.sleep(2.0)
                if not ctx.room.isconnected():
                    _signal_disconnect("room isconnected=False")
                    break
                if has_seen_participant and len(ctx.room.remote_participants) == 0:
                    _signal_disconnect("customer hung up (0 remote participants remaining)")
                    break

        _watcher_task = asyncio.create_task(_hangup_watcher())

        try:
            await asyncio.wait_for(_disconnect_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _log("warning", "Call reached 1-hour safety timeout — shutting down")
        finally:
            _watcher_task.cancel()
            final_dur = max(1, int(time.time() - (call_start_time or time.time()))) if call_start_time else 0
            if call_id:
                try:
                    await update_call_status(
                        call_id=call_id,
                        outcome=tool_ctx.outcome or ("completed" if call_start_time else "failed"),
                        reason=tool_ctx.end_reason or ("Call completed normally" if call_start_time else "Call ended before answer"),
                        duration_seconds=final_dur,
                        recording_url=tool_ctx.recording_url,
                        campaign_id=campaign_id,
                    )
                    await _log("info", f"Call finalized in DB — id={call_id} outcome={tool_ctx.outcome or 'completed'} duration={final_dur}s recording_url={tool_ctx.recording_url}")
                except Exception as _up_err:
                    await _log("error", f"Call final status notice: {_up_err}")

        try:
            await session.aclose()
        except Exception:
            pass
    else:
        _done = asyncio.Event()
        ctx.room.on("disconnected", lambda: _done.set())
        try:
            await asyncio.wait_for(_done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    init_db()
    load_db_settings_to_env()
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller")
    )
