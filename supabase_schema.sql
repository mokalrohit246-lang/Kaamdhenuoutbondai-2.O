-- ═══════════════════════════════════════════════════════
-- OutboundAI — Complete & Idempotent Database Schema
-- Safe to run on fresh database OR existing database
-- ═══════════════════════════════════════════════════════

-- 1. Appointments Table
CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',
    calcom_booking_uid TEXT,
    created_at TEXT NOT NULL
);
ALTER TABLE appointments DISABLE ROW LEVEL SECURITY;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calcom_booking_uid TEXT;
CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments (phone);
CREATE INDEX IF NOT EXISTS idx_appointments_date ON appointments (date);

-- 2. Call Logs Table (With Real Estate, Lifecycle, & Cost Columns)
CREATE TABLE IF NOT EXISTS call_logs (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    lead_name TEXT,
    outcome TEXT DEFAULT 'initiated',
    reason TEXT,
    duration_seconds INTEGER DEFAULT 0,
    call_cost REAL DEFAULT 0.0,
    recording_url TEXT,
    notes TEXT,
    lead_status TEXT,
    campaign_id TEXT,
    property_type TEXT,
    budget TEXT,
    location TEXT,
    transcript TEXT,
    timestamp TEXT NOT NULL
);
ALTER TABLE call_logs DISABLE ROW LEVEL SECURITY;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS call_cost REAL DEFAULT 0.0;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_url TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS lead_status TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS campaign_id TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS property_type TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS budget TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS location TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS transcript TEXT;
CREATE INDEX IF NOT EXISTS idx_call_logs_phone ON call_logs (phone_number);
CREATE INDEX IF NOT EXISTS idx_call_logs_timestamp ON call_logs (timestamp);
CREATE INDEX IF NOT EXISTS idx_call_logs_campaign ON call_logs (campaign_id);

-- 3. Settings Key-Value Store (With Virtual Wallet Defaults)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE settings DISABLE ROW LEVEL SECURITY;
INSERT INTO settings (key, value, updated_at)
VALUES 
    ('WALLET_BALANCE', '0.0', NOW()::TEXT),
    ('LOW_BALANCE_THRESHOLD', '500.0', NOW()::TEXT)
ON CONFLICT (key) DO NOTHING;

-- 4. Error & System Logs Table
CREATE TABLE IF NOT EXISTS error_logs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'error',
    message TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
);
ALTER TABLE error_logs DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_error_logs_timestamp ON error_logs (timestamp);
CREATE INDEX IF NOT EXISTS idx_error_logs_level ON error_logs (level);

-- 5. Campaigns Table
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    contacts_json TEXT NOT NULL DEFAULT '[]',
    schedule_type TEXT NOT NULL DEFAULT 'once',
    schedule_time TEXT DEFAULT '09:00',
    call_delay_seconds INTEGER DEFAULT 3,
    system_prompt TEXT,
    agent_profile_id TEXT,
    allocated_minutes INTEGER DEFAULT 0,
    consumed_minutes INTEGER DEFAULT 0,
    broker_phone TEXT,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    total_dispatched INTEGER DEFAULT 0,
    total_failed INTEGER DEFAULT 0
);
ALTER TABLE campaigns DISABLE ROW LEVEL SECURITY;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS agent_profile_id TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS allocated_minutes INTEGER DEFAULT 0;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS consumed_minutes INTEGER DEFAULT 0;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS broker_phone TEXT;

-- 6. Contact Memory (CRM Insights)
CREATE TABLE IF NOT EXISTS contact_memory (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    insight TEXT NOT NULL,
    created_at TEXT NOT NULL
);
ALTER TABLE contact_memory DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_contact_memory_phone ON contact_memory (phone_number);

-- 7. Agent Profiles Table
CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    voice TEXT NOT NULL DEFAULT 'Aoede',
    model TEXT NOT NULL DEFAULT 'gemini-3.1-flash-live-preview',
    system_prompt TEXT,
    enabled_tools TEXT DEFAULT '[]',
    is_default INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
ALTER TABLE agent_profiles DISABLE ROW LEVEL SECURITY;

-- 8. WhatsApp Logs Table
CREATE TABLE IF NOT EXISTS whatsapp_logs (
    id TEXT PRIMARY KEY,
    to_number TEXT NOT NULL,
    msg_type TEXT NOT NULL DEFAULT 'lead',
    message_preview TEXT,
    status TEXT NOT NULL DEFAULT 'sent',
    campaign_id TEXT,
    lead_phone TEXT,
    timestamp TEXT NOT NULL
);
ALTER TABLE whatsapp_logs DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_timestamp ON whatsapp_logs (timestamp);
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_lead ON whatsapp_logs (lead_phone);

-- 9. Storage Bucket for Call Audio Recordings
INSERT INTO storage.buckets (id, name, public)
VALUES ('call-recordings', 'call-recordings', true)
ON CONFLICT (id) DO UPDATE SET public = true;

