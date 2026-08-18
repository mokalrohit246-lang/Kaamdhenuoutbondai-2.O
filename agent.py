import asyncio
import json
import logging
import os
import ssl
import time
import traceback
import uuid
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

# ── SSL Certificate Fix ─────────────────────────────────────────────────────
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

# ── LiveKit SDK Imports ──────────────────────────────────────────────────────
from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession

# Optional imports — wrapped in try/except so missing packages don't crash startup
try:
    from livekit.agents import RoomInputOptions
except ImportError:
    RoomInputOptions = None

try:
    from livekit.plugins import silero
except ImportError:
    silero = None

try:
    from livekit.plugins import noise_cancellation
except ImportError:
    noise_cancellation = None

# ── Local Module Imports ─────────────────────────────────────────────────────
from db import (
    init_db, log_error, get_enabled_tools, update_call_status,
    complete_call_log, start_call_log, get_setting, lookup_inbound_caller,
)
from prompts import build_prompt
from tools import AppointmentTools

# ── Bootstrap ────────────────────────────────────────────────────────────────
load_dotenv(override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _log(level: str, msg: str, detail: str = "") -> None:
    """Log locally AND persist to Supabase error_logs table."""
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def load_db_settings_to_env() -> None:
    """Load Supabase settings into os.environ ONLY for keys not already set."""
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


# ── Google AI Plugin Discovery ───────────────────────────────────────────────
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


# ── Session Factory ──────────────────────────────────────────────────────────

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
    vad = silero.VAD.load() if silero else None
    return AgentSession(stt=stt, llm=_google_llm(model="gemini-2.0-flash"), tts=tts, vad=vad, tools=tools)


class OutboundAssistant(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


# ── Recording Helper ─────────────────────────────────────────────────────────

async def _start_recording(ctx: agents.JobContext, tool_ctx):
    """Start S3 egress recording in background."""
    _s3_ep = (os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")).rstrip("/")
    _aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "call-recordings")
    _aws_key = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
    _aws_secret = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
    _s3_region = os.getenv("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")

    # Set recording URL on tool context
    if "supabase.co/storage/v1/s3" in _s3_ep:
        _public_ep = _s3_ep.replace("/storage/v1/s3", "/storage/v1/object/public")
        tool_ctx.recording_url = f"{_public_ep}/{_aws_bucket}/recordings/{ctx.room.name}.ogg"
    elif _s3_ep:
        tool_ctx.recording_url = f"{_s3_ep}/{_aws_bucket}/recordings/{ctx.room.name}.ogg"

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


async def _wait_for_hangup(ctx: agents.JobContext, label: str = "Participant"):
    """Block until room disconnects or all remote participants leave."""
    while ctx.room.isconnected():
        await asyncio.sleep(1.0)
        if len(ctx.room.remote_participants) == 0:
            await asyncio.sleep(1.0)
            if len(ctx.room.remote_participants) == 0 or not ctx.room.isconnected():
                await _log("info", f"{label} hung up (0 remote participants remaining)")
                break


# ══════════════════════════════════════════════════════════════════════════════
#  INBOUND CALL HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_inbound(ctx: agents.JobContext, metadata: dict, call_id: str,
                          tool_ctx, enabled_tools: list):
    """Handle an incoming SIP call. The caller is already in the room."""
    await _log("info", f"INBOUND HANDLER started for room={ctx.room.name}")

    # Wait for SIP participant to appear
    remote_p = next(iter(ctx.room.remote_participants.values()), None)
    if not remote_p:
        for _ in range(25):
            await asyncio.sleep(0.2)
            remote_p = next(iter(ctx.room.remote_participants.values()), None)
            if remote_p:
                break

    # Extract caller phone and dialed number
    caller_phone = "unknown"
    called_to = None
    if remote_p:
        caller_phone = (remote_p.identity or "").replace("sip_", "").strip() or "unknown"
        attrs = getattr(remote_p, "attributes", None) or {}
        called_to = (
            attrs.get("sip.callTo") or
            attrs.get("sip.trunkPhoneNumber") or
            attrs.get("sip.phoneNumber")
        )

    # Fallback: extract from room name
    if caller_phone in ("unknown", ""):
        room = ctx.room.name
        if "inbound-call-" in room:
            caller_phone = room.replace("inbound-call-", "").strip()
        elif "inbound-" in room:
            caller_phone = room.replace("inbound-", "").strip()

    if not called_to:
        called_to = os.getenv("VOBIZ_OUTBOUND_NUMBER", "").strip() or None

    phone_number = caller_phone or "inbound-caller"
    await _log("info", f"Inbound call active: from={phone_number} to={called_to}")

    # CRM / Client Inbound Lookup
    caller_info = await lookup_inbound_caller(caller_phone=caller_phone, called_to=called_to)
    lead_name       = caller_info.get("lead_name") or "there"
    business_name   = caller_info.get("business_name") or "Kaamdhenu Real Estate"
    service_type    = caller_info.get("service_type") or "Real Estate Services"
    property_type   = caller_info.get("property_type")
    budget          = caller_info.get("budget")
    location        = caller_info.get("location")
    campaign_id     = caller_info.get("campaign_id")
    broker_phone    = caller_info.get("broker_phone")
    routing_type    = caller_info.get("routing_type", "global_receptionist")

    if caller_info.get("agent_voice"):
        os.environ["GEMINI_TTS_VOICE"] = caller_info["agent_voice"]

    # Update tool context
    tool_ctx.phone_number   = phone_number
    tool_ctx.lead_name      = lead_name
    tool_ctx.campaign_id    = campaign_id
    tool_ctx.campaign_name  = caller_info.get("campaign_name")
    tool_ctx.broker_phone   = broker_phone
    tool_ctx.business_name  = business_name
    tool_ctx.service_type   = service_type
    tool_ctx.property_type  = property_type
    tool_ctx.budget         = budget
    tool_ctx.location       = location

    # Log inbound call to DB
    notes_text = (
        f"Client Inbound ({caller_info.get('client_name')})" if routing_type == "client_specific"
        else (f"Missed Call Return (Previous: {caller_info.get('last_outcome')})" if routing_type == "missed_call_return"
        else "Inbound Call")
    )
    asyncio.create_task(start_call_log(
        call_id=call_id,
        phone_number=phone_number,
        lead_name=lead_name,
        service_type=service_type,
        property_type=property_type,
        budget=budget,
        location=location,
        notes=notes_text,
        campaign_id=campaign_id,
        call_direction="inbound",
        called_to=called_to,
    ))

    # Build Context & Greeting based on routing
    custom_prompt = metadata.get("system_prompt") or ""

    if routing_type == "client_specific":
        inbound_context = (
            f"\n\n[CLIENT INBOUND RECEPTIONIST CONTEXT]\n"
            f"You are the dedicated AI Assistant for '{business_name}' (Client: {caller_info.get('client_name')}). "
            f"The user dialed the dedicated client line ({called_to}). "
            f"Adopt the brand voice and services of {business_name} entirely. "
            f"Caller: {lead_name} ({phone_number})."
        )
        if lead_name != "there":
            greeting = f"Hello {lead_name}! Welcome to {business_name}. How can I assist you with your property search today?"
        else:
            greeting = f"Hello! Welcome to {business_name}. I am Priya, your AI property advisor. How may I assist you today?"

    elif routing_type == "missed_call_return":
        inbound_context = (
            f"\n\n[GLOBAL MISSED CALL RETURN CONTEXT]\n"
            f"This is an INBOUND callback from {lead_name} ({phone_number}). "
            f"They missed our recent call regarding {service_type}. "
            f"Warmly acknowledge that they called back and assist them with their inquiry."
        )
        if lead_name != "there":
            greeting = f"Hello {lead_name}! Thank you for calling back {business_name}. You recently received a call from us regarding {service_type or 'our properties'}. How can I assist you today?"
        else:
            greeting = f"Hello! Thank you for calling back {business_name}. I understand you missed our call. How can I assist you today?"

    else:
        inbound_context = (
            f"\n\n[GLOBAL INBOUND RECEPTIONIST CONTEXT]\n"
            f"This is an INBOUND call from a new prospective client ({phone_number}). "
            f"Professionally greet them as the AI Receptionist for {business_name} and qualify their real estate inquiry."
        )
        greeting = f"Hello! Thank you for calling {business_name}. I am Priya, your AI property advisor. How may I assist you today?"

    effective_prompt = (caller_info.get("custom_prompt") or custom_prompt or "") + inbound_context
    system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                 service_type=service_type, custom_prompt=effective_prompt)

    active_tools = tool_ctx.build_tool_list(enabled_tools)
    session = _build_session(tools=active_tools, system_prompt=system_prompt)
    await session.start(
        room=ctx.room,
        agent=OutboundAssistant(instructions=system_prompt),
    )
    call_start_time = time.time()
    tool_ctx._call_start_time = call_start_time

    # Start recording
    asyncio.create_task(_start_recording(ctx, tool_ctx))

    # Speak greeting immediately
    try:
        await session.generate_reply(instructions=greeting)
        await _log("info", f"Inbound greeting spoken ({routing_type}) to {phone_number}")
    except Exception as _gr_exc:
        await _log("warning", f"Inbound greeting notice: {_gr_exc}")

    # Wait for caller to hang up
    await _wait_for_hangup(ctx, label="Inbound caller")

    return session, call_start_time, phone_number, lead_name, campaign_id, called_to


# ══════════════════════════════════════════════════════════════════════════════
#  OUTBOUND CALL HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_outbound(ctx: agents.JobContext, metadata: dict, call_id: str,
                           tool_ctx, enabled_tools: list):
    """Handle an outbound SIP call. We dial the customer."""
    phone_number    = metadata.get("phone_number")
    lead_name       = metadata.get("lead_name", "there")
    business_name   = metadata.get("business_name", "our company")
    service_type    = metadata.get("service_type", "our service")
    property_type   = metadata.get("property_type")
    budget          = metadata.get("budget")
    location        = metadata.get("location")
    custom_prompt   = metadata.get("system_prompt")
    campaign_id     = metadata.get("campaign_id")
    campaign_name   = metadata.get("campaign_name")
    broker_phone    = metadata.get("broker_phone")
    trunk_id_override = metadata.get("outbound_trunk_id")

    await _log("info", f"OUTBOUND HANDLER started for room={ctx.room.name} phone={phone_number}")

    tool_ctx.phone_number   = phone_number
    tool_ctx.lead_name      = lead_name
    tool_ctx.campaign_id    = campaign_id
    tool_ctx.campaign_name  = campaign_name
    tool_ctx.broker_phone   = broker_phone
    tool_ctx.business_name  = business_name
    tool_ctx.service_type   = service_type
    tool_ctx.property_type  = property_type
    tool_ctx.budget         = budget
    tool_ctx.location       = location

    system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                 service_type=service_type, custom_prompt=custom_prompt)
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    session = _build_session(tools=active_tools, system_prompt=system_prompt)
    await session.start(
        room=ctx.room,
        agent=OutboundAssistant(instructions=system_prompt),
    )

    # Resolve SIP trunk ID
    trunk_id = (
        (trunk_id_override if trunk_id_override and trunk_id_override.startswith("ST_") else "") or
        (os.getenv("OUTBOUND_TRUNK_ID", "").strip() if os.getenv("OUTBOUND_TRUNK_ID", "").strip().startswith("ST_") else "") or
        (await get_setting("OUTBOUND_TRUNK_ID", "")) or
        (trunk_id_override or os.getenv("OUTBOUND_TRUNK_ID", ""))
    ).strip()

    if not trunk_id:
        err_msg = "OUTBOUND_TRUNK_ID not set. Please click '⚡ Create Outbound SIP Trunk' in Settings."
        await _log("error", err_msg)
        tool_ctx.outcome = "failed"
        tool_ctx.end_reason = err_msg
        return session, None, phone_number, lead_name, campaign_id, None

    await _log("info", f"Dialing {phone_number} via SIP trunk {trunk_id}")
    asyncio.create_task(complete_call_log(call_id, outcome="ringing", reason="Dialing customer via SIP", call_direction="outbound"))

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
        tool_ctx._call_start_time = call_start_time
        asyncio.create_task(complete_call_log(call_id, outcome="in_progress", reason="Call answered by customer", call_direction="outbound"))
    except Exception as exc:
        err_msg = f"SIP dial failed: {exc}"
        await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
        tool_ctx.outcome = "failed"
        tool_ctx.end_reason = err_msg
        return session, None, phone_number, lead_name, campaign_id, None

    # Start recording
    asyncio.create_task(_start_recording(ctx, tool_ctx))

    # Speak greeting
    greeting = f"Hi {lead_name}! I am Priya calling from {business_name}. Am I speaking with {lead_name}?"
    try:
        await session.generate_reply(instructions=greeting)
    except Exception as _gr_exc:
        await _log("warning", f"Greeting notice: {_gr_exc}")

    # Wait for customer to hang up
    await _wait_for_hangup(ctx, label="Customer")

    return session, call_start_time, phone_number, lead_name, campaign_id, None


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRYPOINT — Receives ALL jobs (inbound + outbound)
# ══════════════════════════════════════════════════════════════════════════════

async def entrypoint(ctx: agents.JobContext) -> None:
    logger.info("═══ JOB RECEIVED ═══ id=%s room=%s metadata=%s",
                ctx.job.id, ctx.room.name, ctx.job.metadata)
    await _log("info", f"JOB RECEIVED: {ctx.job.id} — room: {ctx.room.name} — metadata: {ctx.job.metadata}")

    # ── Safe metadata parsing ────────────────────────────────────────────
    raw_meta = ctx.job.metadata or ""
    metadata = {}
    if raw_meta:
        try:
            parsed = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
            metadata = parsed if isinstance(parsed, dict) else {}
        except Exception:
            await _log("warning", f"Invalid JSON in job metadata, treating as inbound: {raw_meta}")
            metadata = {}

    # ── Apply environment overrides ──────────────────────────────────────
    if metadata.get("google_api_key"):
        os.environ["GOOGLE_API_KEY"] = metadata["google_api_key"]
    if metadata.get("vobiz_sip_domain"):
        os.environ["VOBIZ_SIP_DOMAIN"] = metadata["vobiz_sip_domain"]
    if metadata.get("voice_override"):
        os.environ["GEMINI_TTS_VOICE"] = metadata["voice_override"]
    if metadata.get("model_override"):
        os.environ["GEMINI_MODEL"] = metadata["model_override"]

    # ── Determine direction ──────────────────────────────────────────────
    is_inbound = False
    if (not metadata
            or not metadata.get("phone_number")
            or metadata.get("direction") == "inbound"
            or metadata.get("inbound")
            or ctx.room.name.startswith("inbound")):
        is_inbound = True

    await _log("info", f"Direction resolved: {'INBOUND' if is_inbound else 'OUTBOUND'}")

    # ── Generate call_id ─────────────────────────────────────────────────
    call_id = metadata.get("call_id") or str(uuid.uuid4())

    # ── Connect to LiveKit room ──────────────────────────────────────────
    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name} (mode: {'INBOUND' if is_inbound else 'OUTBOUND'})")

    # ── Resolve enabled tools ────────────────────────────────────────────
    tools_override = metadata.get("tools_override")
    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            enabled_tools = await get_enabled_tools()
    else:
        enabled_tools = await get_enabled_tools()

    # ── Create tool context ──────────────────────────────────────────────
    phone_number = metadata.get("phone_number")
    lead_name = metadata.get("lead_name", "there")
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name)
    tool_ctx.call_id = call_id

    # ── Declare variables for finally block ──────────────────────────────
    session = None
    call_start_time = None
    campaign_id = metadata.get("campaign_id")
    called_to = None

    try:
        if is_inbound:
            session, call_start_time, phone_number, lead_name, campaign_id, called_to = \
                await _handle_inbound(ctx, metadata, call_id, tool_ctx, enabled_tools)
        else:
            session, call_start_time, phone_number, lead_name, campaign_id, called_to = \
                await _handle_outbound(ctx, metadata, call_id, tool_ctx, enabled_tools)

    except Exception as e:
        logger.exception("CRITICAL ERROR in entrypoint: %s", e)
        await _log("error", f"Entrypoint crash: {e}", traceback.format_exc())
        tool_ctx.outcome = "failed"
        tool_ctx.end_reason = str(e)

    finally:
        # ── Compute final metrics ────────────────────────────────────────
        final_dur = max(1, int(time.time() - call_start_time)) if call_start_time else 0
        final_outcome = getattr(tool_ctx, "outcome", None) or ("completed" if call_start_time else "failed")
        final_reason = getattr(tool_ctx, "end_reason", None) or ("Call completed normally" if call_start_time else "Call ended before answer")
        final_cost = round((final_dur / 60.0) * 1.20, 2)

        logger.info("FINALLY BLOCK: id=%s outcome=%s dur=%ss cost=₹%s dir=%s",
                     call_id, final_outcome, final_dur, final_cost,
                     "inbound" if is_inbound else "outbound")

        # ── Persist to DB ────────────────────────────────────────────────
        if call_id:
            try:
                db_ok = await complete_call_log(
                    call_id=call_id,
                    outcome=final_outcome,
                    duration_seconds=final_dur,
                    cost=final_cost,
                    recording_url=getattr(tool_ctx, "recording_url", None),
                    reason=final_reason,
                    campaign_id=campaign_id,
                    phone_number=phone_number,
                    lead_name=lead_name,
                    call_direction="inbound" if is_inbound else "outbound",
                    called_to=called_to if is_inbound else None,
                )
                if db_ok:
                    await _log("info", f"Call finalized — id={call_id} outcome={final_outcome} dur={final_dur}s cost=₹{final_cost}")
                else:
                    await _log("error", f"complete_call_log returned False — id={call_id}")
            except Exception as _db_err:
                await _log("error", f"Failed to complete_call_log in finally: {_db_err}")

        # ── Close session ────────────────────────────────────────────────
        if session:
            try:
                await session.aclose()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  REQUEST HANDLER — Accepts ALL incoming LiveKit jobs
# ══════════════════════════════════════════════════════════════════════════════

async def request_fnc(req: agents.JobRequest) -> None:
    """Accept every incoming job request without filtering."""
    try:
        job_id = getattr(req.job, "id", "N/A")
        room_name = "N/A"
        try:
            room_name = req.job.room.name
        except Exception:
            pass
        agent_name = getattr(req.job, "agent_name", "N/A")
        logger.info("JOB REQUEST: id=%s room=%s agent=%s — ACCEPTING", job_id, room_name, agent_name)
        await req.accept()
    except Exception as exc:
        logger.error("FAILED to accept job request: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN — Worker Startup
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    load_db_settings_to_env()
    logger.info("🚀 AGENT WORKER INITIALIZED AND LISTENING — agent_name=outbound-caller")
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            request_fnc=request_fnc,
            agent_name="outbound-caller",
        )
    )
