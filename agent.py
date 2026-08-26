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

# ── LiveKit SDK Imports ──────────────────────────────────────────────────────
from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, llm
try:
    from livekit.agents.pipeline import VoicePipelineAgent
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

    # 1. Realtime Mode Fallback
    if use_realtime:
        logger.info("Using GEMINI REALTIME mode")
        RealtimeClass = getattr(getattr(google, "realtime", None), "RealtimeModel", None)
        realtime_llm = RealtimeClass(model=gemini_model, voice=gemini_voice, instructions=system_prompt)
        return AgentSession(llm=realtime_llm, vad=GLOBAL_VAD, tools=tools), True

    # 2. Modular 0.5s Pipeline Mode (VoicePipelineAgent)
    logger.info("Using MODULAR PIPELINE (Deepgram + Flash + Google TTS)")
    
    # GCP Credentials Setup
    google_json_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
    temp_cred_path = "/tmp/gcp_creds.json"
    if google_json_str:
        try:
            with open(temp_cred_path, "w", encoding="utf-8") as f:
                f.write(google_json_str)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_cred_path
        except Exception as f_err:
            logger.error("Failed writing GCP JSON: %s", f_err)

    stt = _deepgram_stt(api_key=deepgram_key, model="nova-3", language="multi", endpointing=0.30) if _deepgram_stt and deepgram_key else None
    tts = google.TTS(voice_name=gemini_voice, language="hi-IN" if "hi-IN" in gemini_voice else "en-IN") if google and hasattr(google, "TTS") else None
    
    initial_ctx = llm.ChatContext()
    initial_ctx.append(role="system", text=system_prompt)

    pipeline_agent = VoicePipelineAgent(
        vad=GLOBAL_VAD,
        stt=stt,
        llm=google.LLM(model="gemini-2.0-flash") if google else None,
        tts=tts,
        chat_ctx=initial_ctx,
        fnc_ctx=tool_ctx,
    )
    return pipeline_agent, False


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


async def _wait_for_hangup(ctx: agents.JobContext):
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
    await _wait_for_hangup(ctx)

    return agent_or_session, call_start_time, caller_phone, "there", None, called_to


# ── Entrypoint ───────────────────────────────────────────────────────────────

async def entrypoint(ctx: agents.JobContext) -> None:
    ctx.perf = PerfProfiler()
    ctx.perf.log("T0: entrypoint triggered")
    
    metadata = json.loads(ctx.job.metadata) if ctx.job.metadata else {}
    call_id = metadata.get("call_id") or str(uuid.uuid4())

    await ctx.connect(auto_subscribe=agents.AutoSubscribe.AUDIO_ONLY)
    enabled_tools = await get_enabled_tools()
    
    tool_ctx = AppointmentTools(ctx, metadata.get("phone_number"), metadata.get("lead_name", "there"))
    tool_ctx.call_id = call_id

    session = None
    call_start_time = None
    phone_number = "unknown"
    lead_name = "there"
    campaign_id = None
    called_to = None

    try:
        session, call_start_time, phone_number, lead_name, campaign_id, called_to = await _handle_inbound(ctx, metadata, call_id, tool_ctx, enabled_tools)
    except Exception as e:
        logger.exception("CRITICAL ERROR: %s", e)
    finally:
        final_dur = max(1, int(time.time() - call_start_time)) if call_start_time else 0
        final_cost = round((final_dur / 60.0) * 1.20, 2)
        await complete_call_log(call_id=call_id, outcome="completed", duration_seconds=final_dur, cost=final_cost, recording_url=getattr(tool_ctx, "recording_url", None), reason="Call completed", phone_number=phone_number, call_direction="inbound", called_to=called_to)


async def request_fnc(req: agents.JobRequest) -> None:
    await req.accept()


if __name__ == "__main__":
    if len(sys.argv) <= 1:
        sys.argv.append("start")
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, request_fnc=request_fnc, agent_name="outbound-caller")
    )
