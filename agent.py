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

from db import (
    init_db, log_error, get_enabled_tools, update_call_status,
    complete_call_log, start_call_log, get_setting, lookup_inbound_caller,
)
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

    is_inbound = False

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
            if data.get("inbound") or data.get("direction") == "inbound":
                is_inbound = True
            if google_key_override:
                os.environ["GOOGLE_API_KEY"] = google_key_override
            if sip_domain_override:
                os.environ["VOBIZ_SIP_DOMAIN"] = sip_domain_override
        except (json.JSONDecodeError, AttributeError):
            await _log("warning", "Invalid JSON in job metadata")

    if not phone_number or ctx.room.name.startswith("inbound") or not ctx.job.metadata:
        is_inbound = True

    if not call_id:
        call_id = str(uuid.uuid4())

    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name} (mode: {'INBOUND' if is_inbound else 'OUTBOUND'})")

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

    tool_ctx = AppointmentTools(ctx, phone_number, lead_name)
    tool_ctx.call_id = call_id

    call_start_time = None

    try:
        if is_inbound:
            remote_p = next(iter(ctx.room.remote_participants.values()), None)
            if not remote_p:
                for _ in range(20):
                    await asyncio.sleep(0.2)
                    remote_p = next(iter(ctx.room.remote_participants.values()), None)
                    if remote_p:
                        break

            caller_phone = "unknown"
            called_to = None
            if remote_p:
                caller_phone = remote_p.identity.replace("sip_", "").strip()
                if hasattr(remote_p, "attributes") and remote_p.attributes:
                    called_to = (
                        remote_p.attributes.get("sip.callTo") or
                        remote_p.attributes.get("sip.trunkPhoneNumber") or
                        remote_p.attributes.get("sip.phoneNumber")
                    )

            if caller_phone in ("unknown", "") and ("inbound-call-" in ctx.room.name or "inbound-" in ctx.room.name):
                caller_phone = ctx.room.name.replace("inbound-call-", "").replace("inbound-", "").strip()

            if not called_to:
                called_to = os.getenv("VOBIZ_OUTBOUND_NUMBER", "").strip() or None

            phone_number = caller_phone or "inbound-caller"
            await _log("info", f"Inbound call received: from={caller_phone} to={called_to}")

            # Dual-Routing CRM / Client Inbound Lookup
            caller_info = await lookup_inbound_caller(caller_phone=caller_phone, called_to=called_to)
            lead_name = caller_info.get("lead_name") or "there"
            business_name = caller_info.get("business_name") or "Kaamdhenu Real Estate"
            service_type = caller_info.get("service_type") or "Real Estate Services"
            property_type = caller_info.get("property_type")
            budget = caller_info.get("budget")
            location = caller_info.get("location")
            campaign_id = caller_info.get("campaign_id")
            broker_phone = caller_info.get("broker_phone")
            routing_type = caller_info.get("routing_type", "global_receptionist")

            if caller_info.get("agent_voice"):
                os.environ["GEMINI_TTS_VOICE"] = caller_info["agent_voice"]

            tool_ctx.phone_number = phone_number
            tool_ctx.lead_name = lead_name
            tool_ctx.campaign_id = campaign_id
            tool_ctx.campaign_name = caller_info.get("campaign_name")
            tool_ctx.broker_phone = broker_phone
            tool_ctx.business_name = business_name
            tool_ctx.service_type = service_type
            tool_ctx.property_type = property_type
            tool_ctx.budget = budget
            tool_ctx.location = location

            # Register initial inbound call log in DB
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

            # Build Context & Greeting based on Dual-Routing Conditions
            if routing_type == "client_specific":
                # Condition B: Client-Specific Inbound Number
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
                # Condition A: Missed Call Return on Global Number
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
                # Fresh Inbound Caller on Global Number
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
            _session_kwargs = dict(
                room=ctx.room,
                agent=OutboundAssistant(instructions=system_prompt),
            )
            await session.start(**_session_kwargs)
            call_start_time = time.time()
            tool_ctx._call_start_time = call_start_time

            _s3_ep = (os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")).rstrip("/")
            _aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "call-recordings")
            if "supabase.co/storage/v1/s3" in _s3_ep:
                _public_ep = _s3_ep.replace("/storage/v1/s3", "/storage/v1/object/public")
                tool_ctx.recording_url = f"{_public_ep}/{_aws_bucket}/recordings/{ctx.room.name}.ogg"
            elif _s3_ep:
                tool_ctx.recording_url = f"{_s3_ep}/{_aws_bucket}/recordings/{ctx.room.name}.ogg"

            try:
                await session.generate_reply(instructions=greeting)
                await _log("info", f"Inbound greeting spoken ({routing_type}) to {phone_number}")
            except Exception as _gr_exc:
                await _log("warning", f"Inbound greeting notice: {_gr_exc}")

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

            while ctx.room.isconnected():
                await asyncio.sleep(1.0)
                if len(ctx.room.remote_participants) == 0:
                    await asyncio.sleep(1.0)
                    if len(ctx.room.remote_participants) == 0 or not ctx.room.isconnected():
                        await _log("info", "Inbound caller hung up (0 remote participants remaining)")
                        break

        else:
            tool_ctx.phone_number = phone_number
            tool_ctx.lead_name = lead_name
            tool_ctx.campaign_id = campaign_id
            tool_ctx.campaign_name = campaign_name
            tool_ctx.broker_phone = broker_phone
            tool_ctx.business_name = business_name
            tool_ctx.service_type = service_type
            tool_ctx.property_type = property_type
            tool_ctx.budget = budget
            tool_ctx.location = location

            system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                          service_type=service_type, custom_prompt=custom_prompt)
            active_tools = tool_ctx.build_tool_list(enabled_tools)
            session = _build_session(tools=active_tools, system_prompt=system_prompt)
            _session_kwargs = dict(
                room=ctx.room,
                agent=OutboundAssistant(instructions=system_prompt),
            )
            await session.start(**_session_kwargs)

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
                return

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
                return

            _s3_ep = (os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")).rstrip("/")
            _aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "call-recordings")
            if "supabase.co/storage/v1/s3" in _s3_ep:
                _public_ep = _s3_ep.replace("/storage/v1/s3", "/storage/v1/object/public")
                tool_ctx.recording_url = f"{_public_ep}/{_aws_bucket}/recordings/{ctx.room.name}.ogg"
            elif _s3_ep:
                tool_ctx.recording_url = f"{_s3_ep}/{_aws_bucket}/recordings/{ctx.room.name}.ogg"

            greeting = f"Hi {lead_name}! I am Priya calling from {business_name}. Am I speaking with {lead_name}?"
            try:
                await session.generate_reply(instructions=greeting)
            except Exception as _gr_exc:
                await _log("warning", f"Greeting notice: {_gr_exc}")

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

            while ctx.room.isconnected():
                await asyncio.sleep(1.0)
                if len(ctx.room.remote_participants) == 0:
                    await asyncio.sleep(1.0)
                    if len(ctx.room.remote_participants) == 0 or not ctx.room.isconnected():
                        await _log("info", "Customer hung up (0 remote participants remaining)")
                        break

    finally:
        final_dur = max(1, int(time.time() - (call_start_time or time.time()))) if call_start_time else 0
        final_outcome = tool_ctx.outcome or ("completed" if call_start_time else "failed")
        final_reason = tool_ctx.end_reason or ("Call completed normally" if call_start_time else "Call ended before answer")
        final_cost = round((final_dur / 60.0) * 1.20, 2)

        logger.info("EXECUTING FINALLY BLOCK: id=%s outcome=%s dur=%ss cost=₹%s dir=%s",
                    call_id, final_outcome, final_dur, final_cost, "inbound" if is_inbound else "outbound")

        if call_id:
            try:
                db_ok = await complete_call_log(
                    call_id=call_id,
                    outcome=final_outcome,
                    duration_seconds=final_dur,
                    cost=final_cost,
                    recording_url=tool_ctx.recording_url,
                    reason=final_reason,
                    campaign_id=campaign_id,
                    phone_number=phone_number,
                    lead_name=lead_name,
                    call_direction="inbound" if is_inbound else "outbound",
                    called_to=called_to if is_inbound else None,
                )
                if db_ok:
                    await _log("info", f"Call finalized in DB — id={call_id} outcome={final_outcome} duration={final_dur}s cost=₹{final_cost} rec={tool_ctx.recording_url}")
                else:
                    await _log("error", f"complete_call_log returned False — id={call_id} outcome={final_outcome} duration={final_dur}s. DB update may have failed!")
            except Exception as _db_err:
                await _log("error", f"Failed to complete_call_log in finally: {_db_err}")

        try:
            await session.aclose()
        except Exception:
            pass


if __name__ == "__main__":
    init_db()
    load_db_settings_to_env()
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint)
    )
