import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

logger = logging.getLogger("db")

# ---------------------------------------------------------------------------
# DEFAULTS — loaded from environment variables (VPS is single source of truth)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "LIVEKIT_URL":             os.getenv("LIVEKIT_URL", ""),
    "LIVEKIT_API_KEY":         os.getenv("LIVEKIT_API_KEY", ""),
    "LIVEKIT_API_SECRET":      os.getenv("LIVEKIT_API_SECRET", ""),
    "GOOGLE_API_KEY":          os.getenv("GOOGLE_API_KEY", ""),
    "GEMINI_MODEL":            os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview"),
    "GEMINI_TTS_VOICE":        os.getenv("GEMINI_TTS_VOICE", "Aoede"),
    "USE_GEMINI_REALTIME":     os.getenv("USE_GEMINI_REALTIME", "true"),
    "VOBIZ_SIP_DOMAIN":        os.getenv("VOBIZ_SIP_DOMAIN", ""),
    "VOBIZ_USERNAME":          os.getenv("VOBIZ_USERNAME", ""),
    "VOBIZ_PASSWORD":          os.getenv("VOBIZ_PASSWORD", ""),
    "VOBIZ_OUTBOUND_NUMBER":   os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
    "OUTBOUND_TRUNK_ID":       os.getenv("OUTBOUND_TRUNK_ID", ""),
    "DEFAULT_TRANSFER_NUMBER": os.getenv("DEFAULT_TRANSFER_NUMBER", ""),
    "SUPABASE_URL":            os.getenv("SUPABASE_URL", ""),
    "SUPABASE_SERVICE_KEY":    os.getenv("SUPABASE_SERVICE_KEY", ""),
    "DEEPGRAM_API_KEY":        os.getenv("DEEPGRAM_API_KEY", ""),
    "TWILIO_ACCOUNT_SID":      os.getenv("TWILIO_ACCOUNT_SID", ""),
    "TWILIO_AUTH_TOKEN":       os.getenv("TWILIO_AUTH_TOKEN", ""),
    "TWILIO_FROM_NUMBER":      os.getenv("TWILIO_FROM_NUMBER", ""),
    "TWILIO_WA_SID":           os.getenv("TWILIO_WA_SID", ""),
    "TWILIO_WA_TOKEN":         os.getenv("TWILIO_WA_TOKEN", ""),
    "TWILIO_WA_FROM":          os.getenv("TWILIO_WA_FROM", ""),
    "WALLET_BALANCE":          os.getenv("WALLET_BALANCE", "0.0"),
    "LOW_BALANCE_THRESHOLD":   os.getenv("LOW_BALANCE_THRESHOLD", "500.0"),
}

SENSITIVE_KEYS = {
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY",
    "VOBIZ_PASSWORD", "TWILIO_AUTH_TOKEN", "SUPABASE_SERVICE_KEY",
    "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "CALCOM_API_KEY",
    "DEEPGRAM_API_KEY", "TWILIO_WA_TOKEN",
}


def _sdb():
    from supabase import create_client
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    return create_client(url, key)


async def _adb():
    from supabase._async.client import create_client
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    return await create_client(url, key)


def init_db() -> None:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        logger.warning("SUPABASE_URL or SUPABASE_SERVICE_KEY not set in environment.")
        return
    try:
        db = _sdb()
        db.table("settings").select("key").limit(1).execute()
        logger.info("Supabase connected successfully")

        # Auto-create call-recordings public bucket if not present
        try:
            bucket_name = os.getenv("S3_BUCKET", "call-recordings") or "call-recordings"
            buckets = db.storage.list_buckets()
            existing_names = [b.name for b in buckets] if buckets else []
            if bucket_name not in existing_names:
                db.storage.create_bucket(bucket_name, options={"public": True})
                logger.info("Auto-created Supabase storage bucket '%s' with public access", bucket_name)
        except Exception as b_exc:
            logger.warning("Storage bucket auto-init notice: %s", b_exc)

    except Exception as exc:
        logger.warning("Supabase connection warning: %s", exc)
        logger.warning("Run supabase_schema.sql in your Supabase Dashboard -> SQL Editor")


# ── Settings ─────────────────────────────────────────────────────────────────

async def get_all_settings() -> dict:
    KNOWN_KEYS = [
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_TTS_VOICE", "USE_GEMINI_REALTIME",
        "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
        "VOBIZ_OUTBOUND_NUMBER", "OUTBOUND_TRUNK_ID", "DEFAULT_TRANSFER_NUMBER",
        "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
        "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
        "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_TIMEZONE",
        "TWILIO_WA_SID", "TWILIO_WA_TOKEN", "TWILIO_WA_FROM",
        "ENABLED_TOOLS",
        "WALLET_BALANCE", "LOW_BALANCE_THRESHOLD",
    ]
    db_rows = {}
    try:
        db = await _adb()
        result = await db.table("settings").select("key, value").execute()
        db_rows = {row["key"]: row["value"] for row in (result.data or []) if row.get("key") != "TEST_KEY"}
    except Exception as exc:
        logger.warning("Could not read settings from DB (using env vars): %s", exc)

    out: dict = {}
    for k in KNOWN_KEYS:
        env_val = os.getenv(k, "").strip()
        db_val = db_rows.get(k, "")
        if k == "OUTBOUND_TRUNK_ID":
            effective_val = env_val if env_val.startswith("ST_") else (db_val if db_val.startswith("ST_") else (env_val or db_val))
        else:
            effective_val = env_val if env_val else db_val
        is_configured = bool(effective_val)
        if k in SENSITIVE_KEYS:
            out[k] = {"value": "", "configured": is_configured, "source": "env" if env_val else "db" if db_val else "none"}
        else:
            out[k] = {"value": effective_val, "configured": is_configured, "source": "env" if env_val else "db" if db_val else "none"}
    return out


async def save_settings(data: dict) -> None:
    try:
        db = await _adb()
        updated_at = datetime.now().isoformat()
        rows = [
            {"key": k, "value": str(v), "updated_at": updated_at}
            for k, v in data.items()
            if v is not None and v != ""
        ]
        if rows:
            await db.table("settings").upsert(rows, on_conflict="key").execute()
    except Exception as exc:
        logger.warning("Could not save settings to DB: %s", exc)


async def get_setting(key: str, default: str = "") -> str:
    env_val = os.getenv(key, "").strip()
    if env_val:
        return env_val
    try:
        db = await _adb()
        result = await db.table("settings").select("value").eq("key", key).maybe_single().execute()
        if result and result.data:
            return result.data["value"]
    except Exception:
        pass
    return DEFAULTS.get(key, default)


# ── Virtual Wallet Helpers ───────────────────────────────────────────────────

async def get_wallet() -> dict:
    try:
        balance_str = await get_setting("WALLET_BALANCE", "0.0")
        thresh_str = await get_setting("LOW_BALANCE_THRESHOLD", "500.0")
        balance = float(balance_str or "0.0")
        threshold = float(thresh_str or "500.0")
        return {
            "balance": round(balance, 2),
            "threshold": round(threshold, 2),
            "is_low": balance < threshold,
        }
    except Exception as exc:
        logger.warning("Could not get wallet: %s", exc)
        return {"balance": 0.0, "threshold": 500.0, "is_low": True}


async def topup_wallet(amount: float) -> dict:
    try:
        current = await get_wallet()
        new_balance = round(current["balance"] + amount, 2)
        await save_settings({"WALLET_BALANCE": str(new_balance)})
        return {
            "balance": new_balance,
            "threshold": current["threshold"],
            "is_low": new_balance < current["threshold"],
            "added": amount,
        }
    except Exception as exc:
        logger.error("Could not topup wallet: %s", exc)
        raise exc


async def deduct_wallet(cost: float) -> float:
    try:
        current = await get_wallet()
        new_balance = round(max(0.0, current["balance"] - cost), 2)
        await save_settings({"WALLET_BALANCE": str(new_balance)})
        return new_balance
    except Exception as exc:
        logger.warning("Could not deduct wallet: %s", exc)
        return 0.0


async def set_setting(key: str, value: str) -> None:
    try:
        db = await _adb()
        await db.table("settings").upsert(
            {"key": key, "value": value, "updated_at": datetime.now().isoformat()},
            on_conflict="key",
        ).execute()
    except Exception as exc:
        logger.warning("Could not set setting %s in DB: %s", key, exc)


async def get_enabled_tools() -> list:
    raw = await get_setting("ENABLED_TOOLS", "")
    if not raw:
        return []
    try:
        import json
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ── Error logs ────────────────────────────────────────────────────────────────

async def log_error(source: str, message: str, detail: str = "", level: str = "error") -> None:
    try:
        db = await _adb()
        await db.table("error_logs").insert({
            "id": str(uuid.uuid4()),
            "source": source,
            "level": level,
            "message": str(message)[:500],
            "detail": str(detail)[:2000],
            "timestamp": datetime.now().isoformat(),
        }).execute()
    except Exception:
        pass


async def get_errors(limit: int = 100) -> list:
    try:
        db = await _adb()
        result = await db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Could not fetch error logs: %s", exc)
        return []


async def get_logs(level: Optional[str] = None, source: Optional[str] = None, limit: int = 200) -> list:
    try:
        db = await _adb()
        query = db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit)
        if level:
            query = query.eq("level", level)
        if source:
            query = query.eq("source", source)
        result = await query.execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Could not fetch logs: %s", exc)
        return []


async def clear_errors() -> None:
    try:
        db = await _adb()
        await db.table("error_logs").delete().neq("id", "").execute()
    except Exception as exc:
        logger.warning("Could not clear logs: %s", exc)


# ── Appointments ──────────────────────────────────────────────────────────────

async def insert_appointment(name: str, phone: str, date: str, time: str, service: str) -> str:
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    try:
        db = await _adb()
        await db.table("appointments").insert({
            "id": full_id, "name": name, "phone": phone,
            "date": date, "time": time, "service": service,
            "status": "booked", "created_at": datetime.now().isoformat(),
        }).execute()
    except Exception as exc:
        logger.error("Could not insert appointment: %s", exc)
    return booking_id


async def check_slot(date: str, time: str) -> bool:
    try:
        db = await _adb()
        result = await (
            db.table("appointments").select("id")
            .eq("date", date).eq("time", time).eq("status", "booked")
            .maybe_single().execute()
        )
        return result.data is None
    except Exception:
        return True


async def get_next_available(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(7 * 24):
        dt += timedelta(hours=1)
        if 9 <= dt.hour < 18:
            if await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
                return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return "no open slots found in the next 7 days"


async def get_all_appointments(date_filter: Optional[str] = None) -> list:
    try:
        db = await _adb()
        query = db.table("appointments").select("*").order("date").order("time")
        if date_filter:
            query = query.eq("date", date_filter)
        result = await query.execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Could not get appointments: %s", exc)
        return []


async def cancel_appointment(appointment_id: str) -> bool:
    try:
        db = await _adb()
        result = await (
            db.table("appointments").update({"status": "cancelled"})
            .eq("id", appointment_id).eq("status", "booked").execute()
        )
        return len(result.data or []) > 0
    except Exception as exc:
        logger.warning("Could not cancel appointment: %s", exc)
        return False


async def get_appointments_by_phone(phone: str) -> list:
    try:
        db = await _adb()
        result = await db.table("appointments").select("*").eq("phone", phone).order("date", desc=True).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Could not get appointments by phone: %s", exc)
        return []


# ── Call logs ─────────────────────────────────────────────────────────────────

async def start_call_log(
    call_id: str, phone_number: str, lead_name: Optional[str] = None,
    service_type: Optional[str] = None, property_type: Optional[str] = None,
    budget: Optional[str] = None, location: Optional[str] = None,
    notes: Optional[str] = None, campaign_id: Optional[str] = None,
) -> bool:
    """Insert initial call log record when call is initiated/dispatched."""
    try:
        db = await _adb()
        row: dict = {
            "id": call_id,
            "phone_number": phone_number,
            "lead_name": lead_name,
            "outcome": "initiated",
            "reason": "Call dispatched to LiveKit",
            "duration_seconds": 0,
            "call_cost": 0.0,
            "timestamp": datetime.now().isoformat(),
        }
        if property_type or service_type:
            row["property_type"] = property_type or service_type
        if budget:
            row["budget"] = budget
        if location:
            row["location"] = location
        if notes:
            row["notes"] = notes
        if campaign_id:
            row["campaign_id"] = campaign_id

        await db.table("call_logs").upsert(row, on_conflict="id").execute()
        logger.info("start_call_log: id=%s phone=%s lead=%s", call_id, phone_number, lead_name)
        return True
    except Exception as exc:
        logger.error("Could not insert start_call_log for %s: %s", call_id, exc)
        return False


# Alias for backward compatibility
insert_initial_call = start_call_log


async def complete_call_log(
    call_id: str, outcome: str, duration_seconds: int = 0,
    cost: Optional[float] = None, recording_url: Optional[str] = None,
    reason: Optional[str] = None, notes: Optional[str] = None,
    lead_status: Optional[str] = None, campaign_id: Optional[str] = None,
    transcript: Optional[str] = None,
    phone_number: Optional[str] = None, lead_name: Optional[str] = None,
) -> bool:
    """Finalize call log using UPSERT (INSERT ON CONFLICT UPDATE).
    This guarantees the row is created or updated regardless of prior state."""
    try:
        db = await _adb()
        calc_cost = cost if cost is not None else round((duration_seconds / 60.0) * 1.20, 2)

        # Build the full row for UPSERT — this works whether the row exists or not
        row: dict = {
            "id": call_id,
            "phone_number": phone_number or "unknown",
            "lead_name": lead_name,
            "outcome": outcome,
            "duration_seconds": duration_seconds,
            "call_cost": calc_cost,
            "timestamp": datetime.now().isoformat(),
        }
        if reason is not None:
            row["reason"] = reason
        if recording_url is not None:
            row["recording_url"] = recording_url
        if transcript is not None:
            row["transcript"] = transcript
        if notes is not None:
            row["notes"] = notes
        if lead_status is not None:
            row["lead_status"] = lead_status
        if campaign_id is not None:
            row["campaign_id"] = campaign_id

        res = await db.table("call_logs").upsert(row, on_conflict="id").execute()
        saved_rows = res.data if res and hasattr(res, 'data') else []
        logger.info("complete_call_log UPSERT: id=%s outcome=%s dur=%ss cost=₹%s rows=%d",
                    call_id, outcome, duration_seconds, calc_cost, len(saved_rows))

        # Also log to error_logs so it's visible in dashboard Logs tab
        try:
            await log_error("agent", f"DB UPSERT OK: id={call_id} outcome={outcome} dur={duration_seconds}s cost=₹{calc_cost} rec={recording_url} rows={len(saved_rows)}", "", "info")
        except Exception:
            pass

        if calc_cost > 0:
            try:
                await deduct_wallet(calc_cost)
            except Exception as _w_err:
                logger.warning("Wallet deduction notice: %s", _w_err)

        if campaign_id and duration_seconds > 0:
            call_minutes = max(1, round(duration_seconds / 60))
            await increment_consumed_minutes(campaign_id, call_minutes)

        return True
    except Exception as exc:
        logger.error("Could not complete_call_log for %s: %s", call_id, exc)
        # Log to dashboard so user can see the error
        try:
            await log_error("agent", f"DB UPSERT FAILED: id={call_id} error={exc}", "", "error")
        except Exception:
            pass
        return False


# Alias for backward compatibility
update_call_status = complete_call_log


async def log_call(
    phone_number: str, lead_name: Optional[str], outcome: str, reason: str,
    duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None,
    lead_status: Optional[str] = None, campaign_id: Optional[str] = None,
    call_id: Optional[str] = None, property_type: Optional[str] = None,
    budget: Optional[str] = None, location: Optional[str] = None,
    transcript: Optional[str] = None,
) -> None:
    try:
        db = await _adb()
        cid = call_id or str(uuid.uuid4())
        cost = round((duration_seconds / 60.0) * 1.20, 2)
        row: dict = {
            "id": cid, "phone_number": phone_number, "lead_name": lead_name,
            "outcome": outcome, "reason": reason, "duration_seconds": duration_seconds,
            "call_cost": cost,
            "timestamp": datetime.now().isoformat(),
        }
        if recording_url:
            row["recording_url"] = recording_url
        if notes:
            row["notes"] = notes
        if lead_status:
            row["lead_status"] = lead_status
        if campaign_id:
            row["campaign_id"] = campaign_id
        if property_type:
            row["property_type"] = property_type
        if budget:
            row["budget"] = budget
        if location:
            row["location"] = location
        if transcript:
            row["transcript"] = transcript
        await db.table("call_logs").upsert(row, on_conflict="id").execute()
        if cost > 0:
            await deduct_wallet(cost)
        if campaign_id and duration_seconds > 0:
            call_minutes = max(1, round(duration_seconds / 60))
            await increment_consumed_minutes(campaign_id, call_minutes)
    except Exception as exc:
        logger.warning("Could not log call: %s", exc)


async def get_all_calls(page: int = 1, limit: int = 20) -> list:
    try:
        db = await _adb()
        offset = (page - 1) * limit
        result = await db.table("call_logs").select("*").order("timestamp", desc=True).range(offset, offset + limit - 1).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Could not get calls: %s", exc)
        return []


async def get_calls_by_phone(phone: str) -> list:
    try:
        db = await _adb()
        result = await db.table("call_logs").select("*").eq("phone_number", phone).order("timestamp", desc=True).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Could not get calls by phone: %s", exc)
        return []


async def update_call_notes(call_id: str, notes: str) -> bool:
    try:
        db = await _adb()
        result = await db.table("call_logs").update({"notes": notes}).eq("id", call_id).execute()
        return len(result.data or []) > 0
    except Exception as exc:
        logger.warning("Could not update call notes: %s", exc)
        return False


async def get_contacts() -> list:
    try:
        db = await _adb()
        result = await db.table("call_logs").select("*").order("timestamp", desc=True).execute()
        rows = result.data or []
        contacts: dict = {}
        for row in rows:
            phone = row["phone_number"]
            if phone not in contacts:
                contacts[phone] = {
                    "phone_number": phone, "lead_name": row.get("lead_name"),
                    "total_calls": 0, "booked": 0,
                    "last_call": row["timestamp"], "last_outcome": row.get("outcome"),
                    "property_type": row.get("property_type"),
                    "budget": row.get("budget"),
                    "location": row.get("location"),
                }
            contacts[phone]["total_calls"] += 1
            if row.get("outcome") == "booked":
                contacts[phone]["booked"] += 1
        return sorted(contacts.values(), key=lambda c: c["last_call"], reverse=True)
    except Exception as exc:
        logger.warning("Could not get contacts: %s", exc)
        return []


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats() -> dict:
    empty_stats = {
        "total_calls": 0, "booked": 0, "not_interested": 0,
        "avg_duration_seconds": 0, "booking_rate_percent": 0,
        "outcomes": {}, "timeline": [], "duration_by_outcome": {},
    }
    try:
        db = await _adb()
        rows = (await db.table("call_logs").select("outcome, duration_seconds, timestamp").execute()).data or []
        total_calls    = len(rows)
        booked         = sum(1 for r in rows if r.get("outcome") == "booked")
        not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
        durations      = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
        avg_dur        = sum(durations) / len(durations) if durations else 0
        booking_rate   = round((booked / total_calls * 100) if total_calls else 0, 1)

        outcomes: dict = {}
        for r in rows:
            o = r.get("outcome") or "unknown"
            outcomes[o] = outcomes.get(o, 0) + 1

        daily: dict = defaultdict(int)
        for r in rows:
            ts = (r.get("timestamp") or "")[:10]
            if ts:
                daily[ts] += 1
        today = datetime.now().date()
        timeline = [{"date": (today - timedelta(days=i)).isoformat(), "count": daily.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(13, -1, -1)]

        dur_sum: dict = defaultdict(float)
        dur_cnt: dict = defaultdict(int)
        for r in rows:
            o = r.get("outcome") or "unknown"
            sec = r.get("duration_seconds")
            if sec:
                dur_sum[o] += sec
                dur_cnt[o] += 1
        duration_by_outcome = {o: dur_sum[o] / dur_cnt[o] for o in dur_sum}
        return {
            "total_calls": total_calls, "booked": booked, "not_interested": not_interested,
            "avg_duration_seconds": round(avg_dur, 1), "booking_rate_percent": booking_rate,
            "outcomes": outcomes, "timeline": timeline, "duration_by_outcome": duration_by_outcome,
        }
    except Exception as exc:
        logger.warning("Could not get stats: %s", exc)
        return empty_stats


# ── Campaigns ─────────────────────────────────────────────────────────────────

async def create_campaign(
    name: str, contacts_json: str, schedule_type: str = "once",
    schedule_time: str = "09:00", call_delay_seconds: int = 3,
    system_prompt: Optional[str] = None, agent_profile_id: Optional[str] = None,
    allocated_minutes: int = 0, broker_phone: Optional[str] = None,
) -> str:
    campaign_id = str(uuid.uuid4())
    try:
        db = await _adb()
        row: dict = {
            "id": campaign_id, "name": name, "status": "active",
            "contacts_json": contacts_json, "schedule_type": schedule_type,
            "schedule_time": schedule_time, "call_delay_seconds": call_delay_seconds,
            "created_at": datetime.now().isoformat(), "total_dispatched": 0, "total_failed": 0,
            "allocated_minutes": allocated_minutes, "consumed_minutes": 0,
        }
        if system_prompt:
            row["system_prompt"] = system_prompt
        if agent_profile_id:
            row["agent_profile_id"] = agent_profile_id
        if broker_phone:
            row["broker_phone"] = broker_phone
        await db.table("campaigns").insert(row).execute()
    except Exception as exc:
        logger.error("Could not create campaign: %s", exc)
    return campaign_id


async def get_all_campaigns() -> list:
    try:
        db = await _adb()
        result = await db.table("campaigns").select("*").order("created_at", desc=True).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Could not get campaigns: %s", exc)
        return []


async def get_campaign(campaign_id: str) -> Optional[dict]:
    try:
        db = await _adb()
        result = await db.table("campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
        return result.data if result else None
    except Exception as exc:
        logger.warning("Could not get campaign %s: %s", campaign_id, exc)
        return None


async def update_campaign_status(campaign_id: str, status: str) -> bool:
    try:
        db = await _adb()
        result = await db.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()
        return len(result.data or []) > 0
    except Exception as exc:
        logger.warning("Could not update campaign status: %s", exc)
        return False


async def update_campaign_run_stats(campaign_id: str, dispatched: int, failed: int) -> None:
    try:
        db = await _adb()
        await db.table("campaigns").update({
            "last_run_at": datetime.now().isoformat(),
            "total_dispatched": dispatched, "total_failed": failed, "status": "completed",
        }).eq("id", campaign_id).execute()
    except Exception as exc:
        logger.warning("Could not update campaign run stats: %s", exc)


async def delete_campaign(campaign_id: str) -> bool:
    try:
        db = await _adb()
        result = await db.table("campaigns").delete().eq("id", campaign_id).execute()
        return len(result.data or []) > 0
    except Exception as exc:
        logger.warning("Could not delete campaign: %s", exc)
        return False


# ── Contact Memory ────────────────────────────────────────────────────────────

async def add_contact_memory(phone: str, insight: str) -> None:
    try:
        db = await _adb()
        await db.table("contact_memory").insert({
            "id": str(uuid.uuid4()), "phone_number": phone,
            "insight": str(insight)[:1000], "created_at": datetime.now().isoformat(),
        }).execute()
    except Exception as exc:
        logger.warning("Could not add contact memory: %s", exc)


async def get_contact_memory(phone: str) -> list:
    try:
        db = await _adb()
        result = await (
            db.table("contact_memory").select("insight, created_at")
            .eq("phone_number", phone).order("created_at", desc=True).limit(20).execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("Could not get contact memory: %s", exc)
        return []


async def compress_contact_memory(phone: str, compressed: str) -> None:
    try:
        db = await _adb()
        await db.table("contact_memory").delete().eq("phone_number", phone).execute()
        await db.table("contact_memory").insert({
            "id": str(uuid.uuid4()), "phone_number": phone,
            "insight": compressed[:2000], "created_at": datetime.now().isoformat(),
        }).execute()
    except Exception as exc:
        logger.warning("Could not compress contact memory: %s", exc)


# ── Agent Profiles ────────────────────────────────────────────────────────────

async def get_all_agent_profiles() -> list:
    try:
        db = await _adb()
        result = await db.table("agent_profiles").select("*").order("created_at").execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Could not get agent profiles: %s", exc)
        return []


async def get_agent_profile(profile_id: str) -> Optional[dict]:
    try:
        db = await _adb()
        result = await db.table("agent_profiles").select("*").eq("id", profile_id).maybe_single().execute()
        return result.data if result else None
    except Exception as exc:
        logger.warning("Could not get agent profile %s: %s", profile_id, exc)
        return None


async def create_agent_profile(
    name: str, voice: str = "Aoede", model: str = "gemini-3.1-flash-live-preview",
    system_prompt: Optional[str] = None, enabled_tools: str = "[]", is_default: bool = False,
) -> str:
    profile_id = str(uuid.uuid4())
    try:
        db = await _adb()
        if is_default:
            await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
        await db.table("agent_profiles").insert({
            "id": profile_id, "name": name, "voice": voice, "model": model,
            "system_prompt": system_prompt, "enabled_tools": enabled_tools,
            "is_default": 1 if is_default else 0, "created_at": datetime.now().isoformat(),
        }).execute()
    except Exception as exc:
        logger.error("Could not create agent profile: %s", exc)
    return profile_id


async def update_agent_profile(profile_id: str, updates: dict) -> bool:
    try:
        db = await _adb()
        result = await db.table("agent_profiles").update(updates).eq("id", profile_id).execute()
        return len(result.data or []) > 0
    except Exception as exc:
        logger.warning("Could not update agent profile: %s", exc)
        return False


async def delete_agent_profile(profile_id: str) -> bool:
    try:
        db = await _adb()
        result = await db.table("agent_profiles").delete().eq("id", profile_id).execute()
        return len(result.data or []) > 0
    except Exception as exc:
        logger.warning("Could not delete agent profile: %s", exc)
        return False


async def set_default_agent_profile(profile_id: str) -> None:
    try:
        db = await _adb()
        await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
        await db.table("agent_profiles").update({"is_default": 1}).eq("id", profile_id).execute()
    except Exception as exc:
        logger.warning("Could not set default agent profile: %s", exc)


# ── Campaign Minute Cap ───────────────────────────────────────────────────────

async def increment_consumed_minutes(campaign_id: str, minutes: int) -> None:
    try:
        db = await _adb()
        campaign = await get_campaign(campaign_id)
        if not campaign:
            return
        new_consumed = (campaign.get("consumed_minutes") or 0) + minutes
        updates: dict = {"consumed_minutes": new_consumed}
        allocated = campaign.get("allocated_minutes") or 0
        if allocated > 0 and new_consumed >= allocated:
            updates["status"] = "paused"
        await db.table("campaigns").update(updates).eq("id", campaign_id).execute()
    except Exception as exc:
        logger.warning("Could not increment consumed minutes: %s", exc)


async def check_campaign_budget(campaign_id: str) -> bool:
    try:
        campaign = await get_campaign(campaign_id)
        if not campaign:
            return False
        allocated = campaign.get("allocated_minutes") or 0
        if allocated == 0:
            return True
        consumed = campaign.get("consumed_minutes") or 0
        return consumed < allocated
    except Exception:
        return True


async def add_campaign_minutes(campaign_id: str, extra_minutes: int) -> bool:
    try:
        db = await _adb()
        campaign = await get_campaign(campaign_id)
        if not campaign:
            return False
        new_allocated = (campaign.get("allocated_minutes") or 0) + extra_minutes
        updates: dict = {"allocated_minutes": new_allocated}
        if campaign.get("status") == "paused":
            consumed = campaign.get("consumed_minutes") or 0
            if consumed < new_allocated:
                updates["status"] = "active"
        await db.table("campaigns").update(updates).eq("id", campaign_id).execute()
        return True
    except Exception as exc:
        logger.warning("Could not add campaign minutes: %s", exc)
        return False


# ── WhatsApp Logs ─────────────────────────────────────────────────────────────

async def log_whatsapp_message(
    to_number: str, msg_type: str, message_preview: str,
    status: str = "sent", campaign_id: Optional[str] = None,
    lead_phone: Optional[str] = None,
) -> None:
    try:
        db = await _adb()
        await db.table("whatsapp_logs").insert({
            "id": str(uuid.uuid4()),
            "to_number": to_number,
            "msg_type": msg_type,
            "message_preview": str(message_preview)[:500],
            "status": status,
            "campaign_id": campaign_id,
            "lead_phone": lead_phone,
            "timestamp": datetime.now().isoformat(),
        }).execute()
    except Exception as exc:
        logger.warning("Could not log whatsapp message: %s", exc)


async def get_whatsapp_logs(limit: int = 100) -> list:
    try:
        db = await _adb()
        result = await db.table("whatsapp_logs").select("*").order("timestamp", desc=True).limit(limit).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Could not get whatsapp logs: %s", exc)
        return []
