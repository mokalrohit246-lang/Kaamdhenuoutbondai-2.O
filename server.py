"""FastAPI backend for the OutboundAI dashboard."""

import asyncio
import json
import logging
import os
import random
import ssl
import uuid
import aiohttp
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

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

from db import (
    SENSITIVE_KEYS, cancel_appointment, clear_errors, create_campaign, delete_campaign,
    get_all_appointments, get_all_calls, get_all_campaigns, get_all_settings,
    get_all_agent_profiles, get_agent_profile, create_agent_profile, update_agent_profile,
    delete_agent_profile, set_default_agent_profile, get_calls_by_phone, get_campaign,
    get_contacts, get_errors, get_logs, get_setting, get_stats, init_db, log_error,
    save_settings, set_setting, update_call_notes, update_campaign_run_stats, update_campaign_status,
    add_campaign_minutes, check_campaign_budget, get_whatsapp_logs,
    insert_initial_call, update_call_status, add_contact_memory,
)
from prompts import DEFAULT_SYSTEM_PROMPT

load_dotenv(override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

init_db()

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None
    logger.warning("APScheduler not installed — campaign scheduling disabled")

app = FastAPI(title="OutboundAI Dashboard", version="2.0.0")


@app.on_event("startup")
async def _startup():
    if _scheduler:
        _scheduler.start()
        await _reschedule_all_campaigns()


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def eff(key: str) -> str:
    """Resolve effective configuration with VPS environment variables as single source of truth."""
    env_val = os.getenv(key, "").strip()
    if key == "OUTBOUND_TRUNK_ID":
        # LiveKit SIP Outbound Trunk IDs MUST start with ST_
        if env_val.startswith("ST_"):
            return env_val
        db_val = (await get_setting(key, "")).strip()
        if db_val.startswith("ST_"):
            return db_val
        return env_val if env_val else db_val
    if env_val:
        return env_val
    return await get_setting(key, "")


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "livekit_configured": bool(os.getenv("LIVEKIT_URL") and os.getenv("LIVEKIT_API_KEY")),
        "gemini_configured": bool(os.getenv("GOOGLE_API_KEY")),
        "supabase_configured": bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY")),
        "trunk_configured": bool(os.getenv("OUTBOUND_TRUNK_ID")),
        "outbound_number": os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
    }


# ── Request models ────────────────────────────────────────────────────────────

class CallRequest(BaseModel):
    phone: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    property_type: Optional[str] = None
    budget: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = "gemini-3.1-flash-live-preview"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class PromptRequest(BaseModel):
    prompt: str


class SettingsRequest(BaseModel):
    settings: dict


class NotesRequest(BaseModel):
    notes: str


class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None
    allocated_minutes: int = 0
    broker_phone: Optional[str] = None


class MinutesRequest(BaseModel):
    minutes: int


class StatusRequest(BaseModel):
    status: str


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found — place index.html in ui/</h1>", status_code=404)


# ── Call dispatch ─────────────────────────────────────────────────────────────

@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")

    if not all([url, key, secret]):
        raise HTTPException(400, "LiveKit credentials not configured. Go to Settings -> LiveKit.")

    import re
    raw_phone = re.sub(r"[^\d+]", "", req.phone.strip())
    if not raw_phone.startswith("+"):
        if len(raw_phone) == 10:
            phone = "+91" + raw_phone
        else:
            phone = "+" + raw_phone
    else:
        phone = raw_phone

    if len(phone) < 8:
        raise HTTPException(400, "Phone number must be at least 8 digits with country code (e.g. +919876543210)")

    call_id = str(uuid.uuid4())
    room_name = f"call-{phone.replace('+', '')}-{call_id[:6]}"

    # 1. Immediately save initial lead & call record to database (NEVER lose lead data)
    await insert_initial_call(
        call_id=call_id, phone_number=phone, lead_name=req.lead_name,
        service_type=req.service_type, property_type=req.property_type,
        budget=req.budget, location=req.location, notes=req.notes,
    )

    # 2. Store initial lead preferences into CRM memory
    insights = []
    if req.property_type: insights.append(f"Property: {req.property_type}")
    if req.budget:        insights.append(f"Budget: {req.budget}")
    if req.location:      insights.append(f"Location: {req.location}")
    if req.notes:         insights.append(f"Notes: {req.notes}")
    if insights:
        await add_contact_memory(phone, " | ".join(insights))

    # 3. Resolve profile & prompt
    effective_prompt = req.system_prompt
    effective_voice = None
    effective_model = None
    effective_tools = None

    if req.agent_profile_id:
        profile = await get_agent_profile(req.agent_profile_id)
        if profile:
            if not effective_prompt and profile.get("system_prompt"):
                effective_prompt = profile["system_prompt"]
            effective_voice = profile.get("voice")
            effective_model = profile.get("model")
            effective_tools = profile.get("enabled_tools")

    if not effective_prompt:
        effective_prompt = await get_setting("system_prompt", "") or None

    trunk_id = await eff("OUTBOUND_TRUNK_ID")
    google_key = await eff("GOOGLE_API_KEY")
    sip_domain = await eff("VOBIZ_SIP_DOMAIN")

    metadata: dict = {
        "call_id": call_id,
        "phone_number": phone,
        "lead_name": req.lead_name,
        "business_name": req.business_name,
        "service_type": req.service_type,
        "property_type": req.property_type or req.service_type,
        "budget": req.budget,
        "location": req.location,
        "system_prompt": effective_prompt,
        "outbound_trunk_id": trunk_id,
        "google_api_key": google_key,
        "vobiz_sip_domain": sip_domain,
    }
    if effective_voice:  metadata["voice_override"] = effective_voice
    if effective_model:  metadata["model_override"] = effective_model
    if effective_tools:  metadata["tools_override"] = effective_tools

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5))
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata)
            )
        )
        await lk.aclose()
        await session.close()
        await log_error("server", f"Call dispatched to {phone}", f"room={room_name} call_id={call_id}", "info")
        return {
            "status": "dispatched",
            "call_id": call_id,
            "room": room_name,
            "phone": phone,
            "lead_name": req.lead_name,
        }
    except Exception as exc:
        logger.error("Dispatch error: %s", exc)
        await update_call_status(call_id, outcome="failed", reason=f"Dispatch error: {exc}")
        raise HTTPException(500, f"Dispatch failed: {exc}")


# ── Calls ─────────────────────────────────────────────────────────────────────

@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)


@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    ok = await update_call_notes(call_id, req.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


# ── Appointments ──────────────────────────────────────────────────────────────

@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


# ── Prompt ────────────────────────────────────────────────────────────────────

@app.get("/api/prompt")
async def api_get_prompt():
    saved = await get_setting("system_prompt", "")
    return {"prompt": saved or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(saved)}


@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    await set_setting("system_prompt", req.prompt)
    return {"status": "saved"}


@app.delete("/api/prompt")
async def api_reset_prompt():
    await set_setting("system_prompt", "")
    return {"status": "reset", "prompt": DEFAULT_SYSTEM_PROMPT}


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    filtered = {k: v for k, v in req.settings.items() if v is not None and v != ""}
    await save_settings(filtered)
    for k, v in filtered.items():
        os.environ[k] = str(v)
    return {"status": "saved", "count": len(filtered)}


# ── SIP trunk setup ───────────────────────────────────────────────────────────

@app.post("/api/setup/trunk")
async def api_setup_trunk():
    url        = await eff("LIVEKIT_URL")
    key        = await eff("LIVEKIT_API_KEY")
    secret     = await eff("LIVEKIT_API_SECRET")
    sip_domain = await eff("VOBIZ_SIP_DOMAIN")
    username   = await eff("VOBIZ_USERNAME")
    password   = await eff("VOBIZ_PASSWORD")
    phone      = await eff("VOBIZ_OUTBOUND_NUMBER")

    if not all([url, key, secret, sip_domain, username, password, phone]):
        raise HTTPException(400, "Configure LiveKit and Vobiz SIP credentials in Settings first.")

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        trunk = await lk.sip.create_sip_outbound_trunk(
            lk_api.CreateSIPOutboundTrunkRequest(
                trunk=lk_api.SIPOutboundTrunkInfo(
                    name="Vobiz Outbound Trunk",
                    address=sip_domain,
                    auth_username=username,
                    auth_password=password,
                    numbers=[phone],
                )
            )
        )
        trunk_id = trunk.sip_trunk_id
        await set_setting("OUTBOUND_TRUNK_ID", trunk_id)
        os.environ["OUTBOUND_TRUNK_ID"] = trunk_id
        await lk.aclose()
        await session.close()
        await log_error("server", f"Created LiveKit SIP Trunk: {trunk_id}", f"domain={sip_domain}", "info")
        return {"status": "created", "trunk_id": trunk_id}
    except Exception as exc:
        raise HTTPException(500, f"Trunk creation failed: {exc}")


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_get_logs(limit: int = 200, level: Optional[str] = None, source: Optional[str] = None):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}


# ── CRM ───────────────────────────────────────────────────────────────────────

@app.get("/api/crm")
async def api_get_contacts():
    return {"data": await get_contacts()}


@app.get("/api/crm/calls")
async def api_get_contact_calls(phone: str = Query(...)):
    return {"data": await get_calls_by_phone(phone)}


# ── Agent Profiles ────────────────────────────────────────────────────────────

@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    try:
        return await get_all_agent_profiles()
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    try:
        profile_id = await create_agent_profile(
            name=req.name, voice=req.voice, model=req.model,
            system_prompt=req.system_prompt, enabled_tools=req.enabled_tools, is_default=req.is_default,
        )
        return {"status": "created", "id": profile_id}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(profile_id, {
        "name": req.name, "voice": req.voice, "model": req.model,
        "system_prompt": req.system_prompt, "enabled_tools": req.enabled_tools,
        "is_default": 1 if req.is_default else 0,
    })
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    try:
        await set_default_agent_profile(profile_id)
        return {"status": "default set"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Campaigns ─────────────────────────────────────────────────────────────────

async def _dispatch_one(lk, lk_api, contact: dict, room_name: str,
                         prompt: Optional[str], profile: Optional[dict] = None,
                         campaign_id: Optional[str] = None,
                         broker_phone: Optional[str] = None,
                         call_id: Optional[str] = None) -> bool:
    try:
        cid = call_id or str(uuid.uuid4())
        phone = contact.get("phone", "")
        lead_name = contact.get("lead_name", "there")
        saved_prompt = prompt or (await get_setting("system_prompt", "")) or None

        # Insert initial campaign call log
        await insert_initial_call(
            call_id=cid, phone_number=phone, lead_name=lead_name,
            service_type=contact.get("service_type", "our service"),
            property_type=contact.get("property_type"),
            budget=contact.get("budget"), location=contact.get("location"),
            campaign_id=campaign_id,
        )

        metadata: dict = {
            "call_id": cid,
            "phone_number": phone,
            "lead_name": lead_name,
            "business_name": contact.get("business_name", "our company"),
            "service_type": contact.get("service_type", "our service"),
            "property_type": contact.get("property_type", contact.get("service_type", "our service")),
            "budget": contact.get("budget"),
            "location": contact.get("location"),
            "system_prompt": saved_prompt,
        }
        if profile:
            if not metadata["system_prompt"] and profile.get("system_prompt"):
                metadata["system_prompt"] = profile["system_prompt"]
            if profile.get("voice"):   metadata["voice_override"] = profile["voice"]
            if profile.get("model"):   metadata["model_override"] = profile["model"]
            if profile.get("enabled_tools"): metadata["tools_override"] = profile["enabled_tools"]
        if campaign_id:
            metadata["campaign_id"] = campaign_id
        if broker_phone:
            metadata["broker_phone"] = broker_phone

        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata))
        )
        return True
    except Exception as exc:
        logger.error("Campaign dispatch error for %s: %s", contact.get("phone"), exc)
        return False


async def _run_campaign(campaign_id: str) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    if campaign.get("status") == "paused":
        logger.info("Campaign %s is paused — skipping run", campaign_id)
        return
    contacts = json.loads(campaign.get("contacts_json") or "[]")
    if not contacts:
        return
    delay = int(campaign.get("call_delay_seconds") or 3)
    prompt = campaign.get("system_prompt")
    agent_profile_id = campaign.get("agent_profile_id")
    broker_phone = campaign.get("broker_phone")
    profile = None
    if agent_profile_id:
        profile = await get_agent_profile(agent_profile_id)

    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        logger.error("Campaign %s: LiveKit not configured", campaign_id)
        return

    from livekit import api as lk_api_module
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))

    ok_count = fail_count = 0
    try:
        lk = lk_api_module.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        for i, contact in enumerate(contacts):
            # Budget check before each call
            if not await check_campaign_budget(campaign_id):
                logger.info("Campaign %s hit minute cap — pausing", campaign_id)
                await update_campaign_status(campaign_id, "paused")
                break

            phone = contact.get("phone", "").strip()
            if not phone:
                fail_count += 1
                continue
            cid = str(uuid.uuid4())
            room_name = f"camp-{campaign_id[:6]}-{phone.replace('+', '')}-{cid[:4]}"
            try:
                await lk.room.create_room(lk_api_module.CreateRoomRequest(name=room_name, empty_timeout=300))
                ok = await _dispatch_one(lk, lk_api_module, contact, room_name, prompt, profile,
                                         campaign_id=campaign_id, broker_phone=broker_phone, call_id=cid)
                if ok:
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception as exc:
                logger.error("Failed to create room for %s: %s", phone, exc)
                fail_count += 1

            if i < len(contacts) - 1:
                await asyncio.sleep(delay)

        await update_campaign_run_stats(campaign_id, ok_count, fail_count)
        await lk.aclose()
        await session.close()
    except Exception as exc:
        logger.error("Campaign execution error: %s", exc)


async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    campaigns = await get_all_campaigns()
    for c in campaigns:
        st = c.get("schedule_type")
        if st in ("daily", "weekdays") and c.get("status") == "active":
            _schedule_campaign(c["id"], st, c.get("schedule_time", "09:00"))


def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    try:
        parts = schedule_time.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        hour, minute = 9, 0
    job_id = f"campaign_{campaign_id}"
    if schedule_type == "daily":
        trigger = CronTrigger(hour=hour, minute=minute)
    else:
        trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(_run_campaign, trigger=trigger, args=[campaign_id], id=job_id, replace_existing=True)
    logger.info("Scheduled campaign %s (%s at %02d:%02d)", campaign_id, schedule_type, hour, minute)


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    if req.schedule_type not in ("once", "daily", "weekdays"):
        raise HTTPException(400, "schedule_type must be: once | daily | weekdays")

    campaign_id = await create_campaign(
        name=req.name, contacts_json=json.dumps(req.contacts),
        schedule_type=req.schedule_type, schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds, system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
        allocated_minutes=req.allocated_minutes, broker_phone=req.broker_phone,
    )
    campaign = await get_campaign(campaign_id)

    if req.schedule_type == "once":
        asyncio.create_task(_run_campaign(campaign_id))
    else:
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)

    return {"status": "created", "campaign_id": campaign_id, "campaign": campaign}


@app.get("/api/campaigns")
async def api_list_campaigns():
    return await get_all_campaigns()


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"status": "deleted"}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(_run_campaign(campaign_id))
    return {"status": "dispatching", "campaign_id": campaign_id}


@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    if req.status not in ("active", "paused", "completed"):
        raise HTTPException(400, "status must be: active | paused | completed")
    ok = await update_campaign_status(campaign_id, req.status)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if req.status == "paused" and _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    elif req.status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign_id, campaign["schedule_type"], campaign.get("schedule_time", "09:00"))
    return {"status": req.status}


@app.post("/api/campaigns/{campaign_id}/add-minutes")
async def api_add_campaign_minutes(campaign_id: str, req: MinutesRequest):
    if req.minutes <= 0:
        raise HTTPException(400, "minutes must be positive")
    ok = await add_campaign_minutes(campaign_id, req.minutes)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    campaign = await get_campaign(campaign_id)
    return {"status": "updated", "campaign": campaign}


# ── WhatsApp Logs ─────────────────────────────────────────────────────────────

@app.get("/api/wa-logs")
async def api_get_wa_logs(limit: int = 100):
    return await get_whatsapp_logs(limit=limit)
