import os
import sys
from datetime import datetime

print("=" * 65, flush=True)
print("🚨🚨🚨 AGENT SCRIPT IS EXECUTING (agent.py) 🚨🚨🚨", flush=True)
print(f"PID: {os.getpid()} | Python: {sys.version.split()[0]} | Time: {datetime.now().isoformat()}", flush=True)
print("=" * 65, flush=True)

import asyncio
import json
import logging
import ssl
import time
import traceback
import uuid
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
from livekit.agents import Agent, AgentSession, llm

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
        try:
            asyncio.create_task(_log("info", msg))
        except Exception:
            pass


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _log(level: str, msg: str, detail: str = "") -> None:
    """Log locally AND persist to Supabase error_logs table in background (non-blocking)."""
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


# ── Global VAD Preloading ───────────────────────────────────────────────────
GLOBAL_VAD = None
if silero:
    try:
        GLOBAL_VAD = silero.VAD.load(
            min_speech_duration=0.15,
            min_silence_duration=0.30,
            prefix_padding_duration=0.10,
            max_buffered_speech=2.0,
            activation_threshold=0.5,
        )
    except Exception:
        try:
            GLOBAL_VAD = silero.VAD.load(
                min_speech_duration=0.15,
                min_silence_duration=0.30,
                prefix_padding_duration=0.10,
            )
        except Exception:
            try:
                GLOBAL_VAD = silero.VAD.load()
            except Exception:
                pass


# ── Chat Context Helper ──────────────────────────────────────────────────────

def _build_initial_chat_context(system_prompt: str, user_text: str) -> llm.ChatContext:
    init_ctx = llm.ChatContext()
    try:
        init_ctx.add_message(role="system", content=system_prompt)
        init_ctx.add_message(role="user", content=user_text)
    except Exception:
        try:
            init_ctx.add_message(role="system", text=system_prompt)
            init_ctx.add_message(role="user", text=user_text)
        except Exception:
            pass
    return init_ctx


# ── Session Factory ──────────────────────────────────────────────────────────

def _build_session(tools: list, system_prompt: str) -> AgentSession:
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"
    deepgram_key = os.getenv("DEEPGRAM_API_KEY", "").strip()

    RealtimeClass = _google_realtime or _google_beta_realtime
    vad = GLOBAL_VAD

    # Prioritize Gemini Live Realtime WebSocket
    if use_realtime and RealtimeClass is not None:
        logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
        try:
            realtime_llm = RealtimeClass(
                model=gemini_model,
                voice=gemini_voice,
                instructions=system_prompt,
            )
        except Exception:
            try:
                realtime_llm = RealtimeClass(
                    model=gemini_model,
                    voice=gemini_voice,
                )
            except Exception:
                realtime_llm = RealtimeClass(model=gemini_model)
        return AgentSession(
            llm=realtime_llm,
            vad=vad,
            tools=tools,
        )

    if _google_llm is None:
        raise RuntimeError("No Google AI backend. Run: pip install 'livekit-plugins-google>=1.0'")

    logger.info("SESSION MODE: Modular Pipeline (Deepgram STT + Gemini Flash LLM + Google TTS)")
    stt = None
    if _deepgram_stt and deepgram_key:
        try:
            stt = _deepgram_stt(
                api_key=deepgram_key,
                model="nova-3",
                language="multi",
                endpointing=0.30,
                interim_results=True,
            )
        except Exception as stt_exc:
            logger.warning("Deepgram STT initialization notice: %s", stt_exc)

    tts = None
    if _google_tts:
        try:
            tts = _google_tts(voice_name=gemini_voice)
        except Exception:
            try:
                tts = _google_tts()
            except Exception:
                pass

    return AgentSession(
        stt=stt,
        llm=_google_llm(model="gemini-2.0-flash"),
        tts=tts,
        vad=vad,
        tools=tools,
    )


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
#  INBOUND CALL HANDLER — INSTANT GREETING (< 500ms)
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_inbound(ctx: agents.JobContext, metadata: dict, call_id: str,
                          tool_ctx, enabled_tools: list):
    """Handle an incoming SIP call with instant greeting response (< 500ms)."""
    await _log("info", f"INBOUND HANDLER started for room={ctx.room.name}")

    # 1. Instant non-blocking participant identity resolution (0ms)
    remote_p = next(iter(ctx.room.remote_participants.values()), None)
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

    if caller_phone in ("unknown", ""):
        room = ctx.room.name
        if "inbound-call-" in room:
            caller_phone = room.replace("inbound-call-", "").strip()
        elif "inbound-" in room:
            caller_phone = room.replace("inbound-", "").strip()

    if not called_to:
        called_to = os.getenv("VOBIZ_OUTBOUND_NUMBER", "").strip() or None

    phone_number = caller_phone or "inbound-caller"

    # 2. Build default system prompt & greeting INSTANTLY (0ms)
    custom_prompt = metadata.get("system_prompt") or ""
    inbound_context = (
        f"\n\n[GLOBAL INBOUND RECEPTIONIST CONTEXT]\n"
        f"This is an INBOUND call from ({phone_number}). "
        f"Greet them immediately as Priya, the AI Receptionist for Kaamdhenu Real Estate."
    )
    effective_prompt = custom_prompt + inbound_context
    system_prompt = build_prompt(lead_name="there", business_name="Kaamdhenu Real Estate",
                                 service_type="Real Estate Services", custom_prompt=effective_prompt)

    # 3. Start AI Session IMMEDIATELY
    tool_ctx.phone_number = phone_number
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    session = _build_session(tools=active_tools, system_prompt=system_prompt)
    if hasattr(ctx, "perf"):
        ctx.perf.log("T2: _build_session completed")

    # Hook up T8 (agent speaking) and T9 (user speaking) listeners
    t8_logged = False
    greeting = "Hello! Namaste, thank you for calling Kaamdhenu Real Estate. I am Priya, your AI property advisor. How may I assist you today?"

    @session.on("agent_state_changed")
    def on_agent_state_changed(ev):
        nonlocal t8_logged
        if hasattr(ctx, "perf"):
            ctx.perf.log(f"Agent state changed: {ev.old_state} -> {ev.new_state}")
        if ev.new_state == "speaking" and not t8_logged:
            t8_logged = True
            if hasattr(ctx, "perf"):
                ctx.perf.log("T8: First audio frame received back from Gemini / agent speaking")

    t9_logged = False
    @session.on("user_state_changed")
    def on_user_state_changed(ev):
        nonlocal t9_logged
        if ev.new_state == "speaking" and not t9_logged:
            t9_logged = True
            if hasattr(ctx, "perf"):
                ctx.perf.log("T9: First user speech detected by VAD")

    await session.start(
        room=ctx.room,
        agent=OutboundAssistant(instructions=system_prompt),
    )
    if hasattr(ctx, "perf"):
        ctx.perf.log("T3: session.start completed")

    # 4. RELIABLE EVENT-DRIVEN OPENING GREETING — triggers once audio track is locked
    greeting_fired = False

    async def _speak_opening():
        nonlocal greeting_fired
        if greeting_fired:
            return
        greeting_fired = True
        try:
            await asyncio.sleep(0.4)
            if getattr(session, "tts", None) is not None:
                await session.say(greeting, allow_interruptions=False)
            else:
                await session.generate_reply(
                    instructions=f"Speak opening greeting immediately in natural Hindi: {greeting}",
                    allow_interruptions=False
                )
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

    call_start_time = time.time()
    tool_ctx._call_start_time = call_start_time

    # 5. Background CRM lookup, DB logging & S3 recording (non-blocking)
    async def _bg_inbound_setup():
        try:
            caller_info = await lookup_inbound_caller(caller_phone=caller_phone, called_to=called_to)
            lead_name = caller_info.get("lead_name") or "there"
            business_name = caller_info.get("business_name") or "Kaamdhenu Real Estate"
            service_type = caller_info.get("service_type") or "Real Estate Services"
            
            tool_ctx.lead_name = lead_name
            tool_ctx.business_name = business_name
            tool_ctx.service_type = service_type
            tool_ctx.campaign_id = caller_info.get("campaign_id")

            notes_text = f"Client Inbound ({caller_info.get('client_name')})" if caller_info.get('routing_type') == "client_specific" else "Inbound Call"
            await start_call_log(
                call_id=call_id,
                phone_number=phone_number,
                lead_name=lead_name,
                service_type=service_type,
                notes=notes_text,
                campaign_id=caller_info.get("campaign_id"),
                call_direction="inbound",
                called_to=called_to,
            )
        except Exception as _exc:
            logger.warning("Background inbound setup notice: %s", _exc)

    asyncio.create_task(_bg_inbound_setup())
    asyncio.create_task(_start_recording(ctx, tool_ctx))

    # 6. Wait for caller to hang up
    await _wait_for_hangup(ctx, label="Inbound caller")

    return session, call_start_time, phone_number, "there", None, called_to


# ══════════════════════════════════════════════════════════════════════════════
#  OUTBOUND CALL HANDLER — INSTANT GREETING ON ANSWER (< 200ms)
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_outbound(ctx: agents.JobContext, metadata: dict, call_id: str,
                           tool_ctx, enabled_tools: list):
    """Handle an outbound SIP call. Session starts AFTER customer answers to keep WebSocket fresh."""
    phone_number    = metadata.get("phone_number")
    lead_name       = metadata.get("lead_name", "there")
    business_name   = metadata.get("business_name", "our company")
    service_type    = metadata.get("service_type", "our service")
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

    system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                 service_type=service_type, custom_prompt=metadata.get("system_prompt"))
    active_tools = tool_ctx.build_tool_list(enabled_tools)

    # 1. Resolve SIP trunk ID FIRST (before session start — avoids wasting a WebSocket)
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

    # 2. Dial SIP participant — blocks while phone rings (NO session running yet, no WebSocket wasted)
    try:
        if hasattr(ctx, "perf"):
            ctx.perf.log("T4: create_sip_participant calling")
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=trunk_id,
                sip_call_to=phone_number,
                participant_identity=f"sip_{phone_number}",
                wait_until_answered=True,
            )
        )
        if hasattr(ctx, "perf"):
            ctx.perf.log("T4: create_sip_participant answered")
        call_start_time = time.time()
        tool_ctx._call_start_time = call_start_time
    except Exception as exc:
        err_msg = f"SIP dial failed: {exc}"
        await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
        tool_ctx.outcome = "failed"
        tool_ctx.end_reason = err_msg
        return None, None, phone_number, lead_name, campaign_id, None

    # 3. CUSTOMER ANSWERED → Start AI Session NOW (fresh WebSocket, no staleness)
    await _log("info", f"Customer answered {phone_number} — starting Gemini session NOW")
    session = _build_session(tools=active_tools, system_prompt=system_prompt)
    if hasattr(ctx, "perf"):
        ctx.perf.log("T2: _build_session completed")

    # Hook up T8 (agent speaking) and T9 (user speaking) listeners
    t8_logged = False
    greeting = f"Hello! Namaste {lead_name}, I am Priya calling from {business_name} regarding your inquiry for {service_type}. Am I speaking with {lead_name}?"

    @session.on("agent_state_changed")
    def on_agent_state_changed(ev):
        nonlocal t8_logged
        if hasattr(ctx, "perf"):
            ctx.perf.log(f"Agent state changed: {ev.old_state} -> {ev.new_state}")
        if ev.new_state == "speaking" and not t8_logged:
            t8_logged = True
            if hasattr(ctx, "perf"):
                ctx.perf.log("T8: First audio frame received back from Gemini / agent speaking")

    t9_logged = False
    @session.on("user_state_changed")
    def on_user_state_changed(ev):
        nonlocal t9_logged
        if ev.new_state == "speaking" and not t9_logged:
            t9_logged = True
            if hasattr(ctx, "perf"):
                ctx.perf.log("T9: First user speech detected by VAD")

    await session.start(
        room=ctx.room,
        agent=OutboundAssistant(instructions=system_prompt),
    )
    if hasattr(ctx, "perf"):
        ctx.perf.log("T3: session.start completed")

    # 4. RELIABLE EVENT-DRIVEN OPENING GREETING — triggers once audio track is locked
    greeting_fired = False

    async def _speak_opening():
        nonlocal greeting_fired
        if greeting_fired:
            return
        greeting_fired = True
        try:
            await asyncio.sleep(0.4)
            if getattr(session, "tts", None) is not None:
                await session.say(greeting, allow_interruptions=False)
            else:
                await session.generate_reply(
                    instructions=f"Speak opening greeting immediately in natural Hindi: {greeting}",
                    allow_interruptions=False
                )
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

    # 5. Background tasks for DB logging & S3 recording (non-blocking)
    asyncio.create_task(complete_call_log(call_id, outcome="in_progress", reason="Call answered by customer", call_direction="outbound"))
    asyncio.create_task(_start_recording(ctx, tool_ctx))

    # 6. Wait for customer to hang up
    await _wait_for_hangup(ctx, label="Customer")

    return session, call_start_time, phone_number, lead_name, campaign_id, None


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRYPOINT — Receives ALL jobs (inbound + outbound)
# ══════════════════════════════════════════════════════════════════════════════

async def entrypoint(ctx: agents.JobContext) -> None:
    ctx.perf = PerfProfiler()
    ctx.perf.log("T0: entrypoint triggered")

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

    # ── Register track_subscribed listener for T5 ──────────────────────────
    t5_logged = False
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        nonlocal t5_logged
        if track.kind == rtc.TrackKind.KIND_AUDIO and not t5_logged:
            t5_logged = True
            if hasattr(ctx, "perf"):
                ctx.perf.log(f"T5: First remote audio track subscribed ({participant.identity})")

    # ── Connect to LiveKit room + resolve tools IN PARALLEL ────────────────
    tools_override = metadata.get("tools_override")

    async def _resolve_tools():
        if tools_override:
            try:
                return json.loads(tools_override)
            except Exception:
                pass
        return await get_enabled_tools()

    _connect_result, enabled_tools = await asyncio.gather(
        ctx.connect(auto_subscribe=agents.AutoSubscribe.AUDIO_ONLY),
        _resolve_tools(),
    )
    ctx.perf.log("T1: connect completed")
    await _log("info", f"Connected to LiveKit room: {ctx.room.name} (mode: {'INBOUND' if is_inbound else 'OUTBOUND'})")

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
    if len(sys.argv) <= 1:
        sys.argv.append("start")
    print(f"🚀 [agent.py] Initializing with CLI args: {sys.argv}", flush=True)
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
