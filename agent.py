import os
import sys
import asyncio
import json
import logging
import ssl
import time
import tempfile
import traceback
import uuid
from datetime import datetime
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
from livekit.agents import Agent, AgentSession, llm

VoicePipelineAgent = None
try:
    from livekit.agents.pipeline import VoicePipelineAgent
except ImportError:
    try:
        from livekit.agents.voice_pipeline import VoicePipelineAgent
    except ImportError:
        try:
            from livekit.agents import VoicePipelineAgent
        except ImportError:
            VoicePipelineAgent = None

try:
    from livekit.plugins import silero, google
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    silero = None
    google = None
    _deepgram_stt = None

try:
    from google.oauth2 import service_account
except ImportError:
    service_account = None

# ── Local Module Imports ─────────────────────────────────────────────────────
from db import (
    init_db, log_error, get_enabled_tools, update_call_status,
    complete_call_log, start_call_log, get_setting, lookup_inbound_caller,
)
from prompts import build_prompt
from tools import AppointmentTools

load_dotenv(override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

# ── Global VAD Preloading ───────────────────────────────────────────────────
GLOBAL_VAD = None
if silero:
    try:
        GLOBAL_VAD = silero.VAD.load(
            min_speech_duration=0.15,
            min_silence_duration=0.30,
            prefix_padding_duration=0.10,
            activation_threshold=0.60,
        )
    except Exception:
        pass


class PerfProfiler:
    def __init__(self):
        self.t0 = time.perf_counter()
        self.last_t = self.t0

    def log(self, step_name: str):
        now = time.perf_counter()
        step_elapsed = now - self.last_t
        total_elapsed = now - self.t0
        self.last_t = now
        msg = f"[PERF-PROFILE] [{step_name}] -> +{step_elapsed:.4f}s (Total Elapsed: {total_elapsed:.4f}s)"
        print(msg, flush=True)


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.error(msg)
    try:
        asyncio.create_task(log_error("agent", msg, detail, level))
    except Exception:
        pass


# ── Agent / Session Builder ──────────────────────────────────────────────────

def _build_agent_or_session(tools: list, system_prompt: str, tool_ctx=None):
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "hi-IN-Neural2-A").strip()
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "false").lower() == "true"
    deepgram_key = os.getenv("DEEPGRAM_API_KEY", "").strip()

    # 1. Fallback to AgentSession (Gemini Realtime) if explicitly requested OR if VoicePipelineAgent is unavailable
    if use_realtime or VoicePipelineAgent is None:
        logger.info("Using GEMINI REALTIME / AgentSession mode (use_realtime=%s, VoicePipelineAgent=%s)",
                    use_realtime, VoicePipelineAgent is not None)
        RealtimeClass = (
            getattr(getattr(google, "realtime", None), "RealtimeModel", None) or
            getattr(getattr(getattr(google, "beta", None), "realtime", None), "RealtimeModel", None)
        )
        if RealtimeClass is not None:
            try:
                realtime_llm = RealtimeClass(model=gemini_model, voice=gemini_voice, instructions=system_prompt)
            except Exception:
                realtime_llm = RealtimeClass(model=gemini_model)
            return AgentSession(llm=realtime_llm, vad=GLOBAL_VAD, tools=tools), True
        elif google and hasattr(google, "LLM"):
            return AgentSession(llm=google.LLM(model=gemini_model), vad=GLOBAL_VAD, tools=tools), True

    # 2. Modular 0.5s Pipeline Mode (VoicePipelineAgent)
    logger.info("Using MODULAR PIPELINE (Deepgram STT + Gemini Flash LLM + Google TTS)")
    
    # GCP Credentials Setup
    google_json_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
    temp_cred_path = "/tmp/gcp_creds.json" if os.name != "nt" else os.path.join(tempfile.gettempdir(), "gcp_creds.json")
    if google_json_str:
        try:
            with open(temp_cred_path, "w", encoding="utf-8") as f:
                f.write(google_json_str)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_cred_path
        except Exception as f_err:
            logger.error("Failed writing GCP JSON: %s", f_err)

    stt = None
    if _deepgram_stt and deepgram_key:
        try:
            stt = _deepgram_stt(api_key=deepgram_key, model="nova-3", language="multi")
        except Exception as exc:
            logger.error("STT Init Error: %s", exc)

    tts = None
    if google and hasattr(google, "TTS"):
        try:
            tts = google.TTS(voice_name=gemini_voice, language="hi-IN" if "hi-IN" in gemini_voice else "en-IN")
        except Exception as tts_err:
            logger.error("Google TTS Init Error: %s", tts_err)

    initial_ctx = llm.ChatContext()
    try:
        initial_ctx.add_message(role="system", content=system_prompt)
    except Exception:
        try:
            initial_ctx.append(role="system", text=system_prompt)
        except Exception:
            pass

    try:
        pipeline_agent = VoicePipelineAgent(
            vad=GLOBAL_VAD,
            stt=stt,
            llm=google.LLM(model=gemini_model if "gemini" in gemini_model else "gemini-2.0-flash") if google and hasattr(google, "LLM") else None,
            tts=tts,
            chat_ctx=initial_ctx,
            fnc_ctx=tool_ctx,
        )
        return pipeline_agent, False
    except Exception as agent_err:
        logger.error("Failed to build VoicePipelineAgent, falling back to AgentSession: %s", agent_err)
        RealtimeClass = getattr(getattr(google, "realtime", None), "RealtimeModel", None)
        if RealtimeClass is not None:
            realtime_llm = RealtimeClass(model=gemini_model, voice=gemini_voice)
            return AgentSession(llm=realtime_llm, vad=GLOBAL_VAD, tools=tools), True
        return AgentSession(llm=google.LLM(model=gemini_model) if google and hasattr(google, "LLM") else None, vad=GLOBAL_VAD, tools=tools), True


class OutboundAssistant(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


async def _start_recording(ctx: agents.JobContext, tool_ctx):
    _s3_ep = (os.getenv("S3_ENDPOINT_URL") or "").rstrip("/")
    _aws_bucket = os.getenv("S3_BUCKET", "call-recordings")
    _aws_key = os.getenv("S3_ACCESS_KEY_ID", "")
    _aws_secret = os.getenv("S3_SECRET_ACCESS_KEY", "")
    _s3_region = os.getenv("S3_REGION", "ap-northeast-1")

    if "supabase.co/storage/v1/s3" in _s3_ep:
        tool_ctx.recording_url = f"{_s3_ep.replace('/storage/v1/s3', '/storage/v1/object/public')}/{_aws_bucket}/recordings/{ctx.room.name}.ogg"

    if _aws_key and _aws_secret and _aws_bucket:
        try:
            _egress_req = api.RoomCompositeEgressRequest(
                room_name=ctx.room.name, audio_only=True,
                file_outputs=[api.EncodedFileOutput(
                    file_type=api.EncodedFileType.OGG, filepath=f"recordings/{ctx.room.name}.ogg",
                    s3=api.S3Upload(access_key=_aws_key, secret=_aws_secret, bucket=_aws_bucket, region=_s3_region, endpoint=_s3_ep),
                )],
            )
            await ctx.api.egress.start_room_composite_egress(_egress_req)
        except Exception as exc:
            logger.warning("Recording notice: %s", exc)


async def _wait_for_hangup(ctx: agents.JobContext, label: str = "Participant"):
    while ctx.room.isconnected():
        await asyncio.sleep(1.0)
        if len(ctx.room.remote_participants) == 0:
            await asyncio.sleep(1.0)
            if len(ctx.room.remote_participants) == 0 or not ctx.room.isconnected():
                break


# ── Inbound Handler ──────────────────────────────────────────────────────────

async def _handle_inbound(ctx: agents.JobContext, metadata: dict, call_id: str, tool_ctx, enabled_tools: list):
    remote_p = next(iter(ctx.room.remote_participants.values()), None)
    caller_phone = (remote_p.identity or "").replace("sip_", "").strip() if remote_p else "inbound-caller"
    called_to = os.getenv("VOBIZ_OUTBOUND_NUMBER", "").strip() or None

    system_prompt = build_prompt(
        lead_name="there", business_name="Kaamdhenu Real Estate", service_type="Real Estate Services",
        custom_prompt=f"This is an INBOUND call from ({caller_phone}). Greet them immediately as Priya, AI Receptionist for Kaamdhenu Real Estate."
    )

    tool_ctx.phone_number = caller_phone
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    
    agent_or_session, is_realtime = _build_agent_or_session(active_tools, system_prompt, tool_ctx)
    
    if is_realtime:
        await agent_or_session.start(room=ctx.room, agent=OutboundAssistant(instructions=system_prompt))
    else:
        agent_or_session.start(ctx.room)

    greeting = "Hello! Namaste, thank you for calling Kaamdhenu Real Estate. I am Priya, your AI property advisor. How may I assist you today?"
    greeting_fired = False

    async def _speak_opening():
        nonlocal greeting_fired
        if greeting_fired:
            return
        greeting_fired = True
        try:
            await asyncio.sleep(0.4)
            if is_realtime:
                await agent_or_session.generate_reply(instructions=f"Speak greeting in Hindi: {greeting}", allow_interruptions=False)
            else:
                await agent_or_session.say(greeting, allow_interruptions=False)
            await _log("info", f"Opening greeting spoken to {caller_phone}")
        except Exception as exc:
            await _log("error", f"Failed to speak opening greeting: {exc}")

    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.create_task(_speak_opening())

    for p in ctx.room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(_speak_opening())
                break

    call_start_time = time.time()
    tool_ctx._call_start_time = call_start_time

    asyncio.create_task(start_call_log(call_id=call_id, phone_number=caller_phone, lead_name="there", service_type="Real Estate", notes="Inbound Call", call_direction="inbound", called_to=called_to))
    asyncio.create_task(_start_recording(ctx, tool_ctx))
    await _wait_for_hangup(ctx, label="Inbound caller")

    return agent_or_session, call_start_time, caller_phone, "there", None, called_to


# ── Outbound Handler ─────────────────────────────────────────────────────────

async def _handle_outbound(ctx: agents.JobContext, metadata: dict, call_id: str, tool_ctx, enabled_tools: list):
    phone_number = metadata.get("phone_number")
    lead_name = metadata.get("lead_name", "there")
    business_name = metadata.get("business_name", "our company")
    service_type = metadata.get("service_type", "our service")
    campaign_id = metadata.get("campaign_id")
    campaign_name = metadata.get("campaign_name")
    broker_phone = metadata.get("broker_phone")
    trunk_id_override = metadata.get("outbound_trunk_id")

    tool_ctx.phone_number = phone_number
    tool_ctx.lead_name = lead_name
    tool_ctx.campaign_id = campaign_id
    tool_ctx.campaign_name = campaign_name
    tool_ctx.broker_phone = broker_phone
    tool_ctx.business_name = business_name
    tool_ctx.service_type = service_type

    system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                 service_type=service_type, custom_prompt=metadata.get("system_prompt"))
    active_tools = tool_ctx.build_tool_list(enabled_tools)

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
        return None, None, phone_number, lead_name, campaign_id, None

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
    except Exception as exc:
        err_msg = f"SIP dial failed: {exc}"
        await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
        tool_ctx.outcome = "failed"
        tool_ctx.end_reason = err_msg
        return None, None, phone_number, lead_name, campaign_id, None

    await _log("info", f"Customer answered {phone_number} — starting AI session NOW")
    agent_or_session, is_realtime = _build_agent_or_session(active_tools, system_prompt, tool_ctx)

    if is_realtime:
        await agent_or_session.start(room=ctx.room, agent=OutboundAssistant(instructions=system_prompt))
    else:
        agent_or_session.start(ctx.room)

    greeting = f"Hello! Namaste {lead_name}, I am Priya calling from {business_name} regarding your inquiry for {service_type}. Am I speaking with {lead_name}?"
    greeting_fired = False

    async def _speak_opening():
        nonlocal greeting_fired
        if greeting_fired:
            return
        greeting_fired = True
        try:
            await asyncio.sleep(0.4)
            if is_realtime:
                await agent_or_session.generate_reply(instructions=f"Speak greeting in Hindi: {greeting}", allow_interruptions=False)
            else:
                await agent_or_session.say(greeting, allow_interruptions=False)
            await _log("info", f"Opening greeting spoken to {phone_number}")
        except Exception as exc:
            await _log("error", f"Failed to speak opening greeting: {exc}")

    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.create_task(_speak_opening())

    for p in ctx.room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(_speak_opening())
                break

    asyncio.create_task(complete_call_log(call_id, outcome="in_progress", reason="Call answered by customer", call_direction="outbound"))
    asyncio.create_task(_start_recording(ctx, tool_ctx))
    await _wait_for_hangup(ctx, label="Customer")

    return agent_or_session, call_start_time, phone_number, lead_name, campaign_id, None


# ── Entrypoint ───────────────────────────────────────────────────────────────

async def entrypoint(ctx: agents.JobContext) -> None:
    ctx.perf = PerfProfiler()
    ctx.perf.log("T0: entrypoint triggered")
    
    raw_meta = ctx.job.metadata or ""
    metadata = {}
    if raw_meta:
        try:
            metadata = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
        except Exception:
            metadata = {}

    is_inbound = False
    if (not metadata
            or not metadata.get("phone_number")
            or metadata.get("direction") == "inbound"
            or metadata.get("inbound")
            or ctx.room.name.startswith("inbound")):
        is_inbound = True

    call_id = metadata.get("call_id") or str(uuid.uuid4())

    await ctx.connect(auto_subscribe=agents.AutoSubscribe.AUDIO_ONLY)
    enabled_tools = await get_enabled_tools()
    
    phone_number = metadata.get("phone_number")
    lead_name = metadata.get("lead_name", "there")
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name)
    tool_ctx.call_id = call_id

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
    finally:
        final_dur = max(1, int(time.time() - call_start_time)) if call_start_time else 0
        final_cost = round((final_dur / 60.0) * 1.20, 2)
        await complete_call_log(
            call_id=call_id,
            outcome=getattr(tool_ctx, "outcome", None) or ("completed" if call_start_time else "failed"),
            duration_seconds=final_dur,
            cost=final_cost,
            recording_url=getattr(tool_ctx, "recording_url", None),
            reason=getattr(tool_ctx, "end_reason", None) or ("Call completed" if call_start_time else "Call ended before answer"),
            phone_number=phone_number,
            call_direction="inbound" if is_inbound else "outbound",
            called_to=called_to if is_inbound else None,
        )
        if session and hasattr(session, "aclose"):
            try:
                await session.aclose()
            except Exception:
                pass


async def request_fnc(req: agents.JobRequest) -> None:
    await req.accept()


if __name__ == "__main__":
    if len(sys.argv) <= 1:
        sys.argv.append("start")
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, request_fnc=request_fnc, agent_name="outbound-caller")
    )
