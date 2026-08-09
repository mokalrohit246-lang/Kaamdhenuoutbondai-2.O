# 🚀 OutboundAI — Production-Grade AI Voice Calling SaaS Platform

> **Automated outbound voice calling powered by Gemini Live AI, LiveKit WebRTC, and Vobiz SIP telephony.**
> Sub-300ms latency · Real-time AI conversations · WhatsApp lead routing · Campaign management · ₹1.20/minute total cost

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green?logo=fastapi)
![Gemini](https://img.shields.io/badge/Gemini_Live-3.1_Flash-orange?logo=google)
![LiveKit](https://img.shields.io/badge/LiveKit-WebRTC-purple?logo=webrtc)
![Docker](https://img.shields.io/badge/Docker-Ready-blue?logo=docker)
![Supabase](https://img.shields.io/badge/Supabase-Database-darkgreen?logo=supabase)

---

## 📋 Table of Contents

- [What It Does](#-what-it-does)
- [Architecture](#-architecture)
- [Tech Stack](#-tech-stack)
- [Cost Breakdown](#-cost-breakdown)
- [Quick Start](#-quick-start)
- [Environment Variables](#-environment-variables)
- [Database Setup](#-database-setup)
- [Deployment](#-deployment)
- [Dashboard](#-dashboard)
- [AI Agent Tools](#-ai-agent-tools)
- [API Endpoints](#-api-endpoints)

---

## 🎯 What It Does

OutboundAI is a **complete AI outbound voice calling SaaS** that:

1. **Dials phone numbers** automatically via SIP telephony (Vobiz)
2. **Connects each call** to a **Gemini Live real-time AI voice agent** (sub-100ms latency, zero separate STT/TTS)
3. **Books appointments** into Supabase and optionally into Cal.com
4. **Runs mass campaign calling** with APScheduler (once / daily / weekdays at a scheduled time)
5. **Maintains a CRM** with per-contact history, editable notes, and AI-extracted memory
6. **Remembers key facts** about each lead across calls using Gemini Flash compression
7. **Records calls** to S3-compatible storage (Supabase Storage / AWS S3)
8. **Sends SMS confirmations** via Twilio after booking
9. **Routes hot leads via WhatsApp** to assigned brokers with instant notifications
10. **Tracks campaign budgets** with per-minute billing caps and auto-pause

---

## 🏗 Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        VPS / Docker Host                         │
│                                                                  │
│  ┌─────────────────┐     ┌──────────────────────────────────┐   │
│  │  FastAPI Server  │     │     LiveKit Agent Worker         │   │
│  │   (server.py)    │     │       (agent.py)                 │   │
│  │                  │     │                                  │   │
│  │  • REST API      │     │  • Gemini Live Realtime Model    │   │
│  │  • Dashboard UI  │     │  • SIP Dial-first Pattern        │   │
│  │  • Campaign Mgr  │     │  • Noise Cancellation            │   │
│  │  • APScheduler   │     │  • 10 AI Function Tools          │   │
│  └────────┬─────────┘     └──────────┬───────────────────────┘   │
│           │                          │                           │
│           ▼                          ▼                           │
│  ┌─────────────────┐     ┌──────────────────────────────────┐   │
│  │   Supabase DB    │     │      LiveKit Cloud (WebRTC)      │   │
│  │  (PostgreSQL)    │     │                                  │   │
│  │                  │     │  Room ◄──► Gemini Live Session    │   │
│  │  • settings      │     │    │                             │   │
│  │  • call_logs     │     │    ▼                             │   │
│  │  • appointments  │     │  SIP Participant (Vobiz PSTN)    │   │
│  │  • campaigns     │     │    │                             │   │
│  │  • contacts      │     │    ▼                             │   │
│  │  • whatsapp_logs │     │  📞 Real Phone Call              │   │
│  └──────────────────┘     └──────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

### Call Flow (Dial-First Pattern)

```
1. FastAPI dispatches job to LiveKit  ──►  LiveKit creates Room
2. Agent creates SIP participant      ──►  Vobiz dials the phone number
3. wait_until_answered=True           ──►  Phone rings (20-30 seconds)
4. Call answered                      ──►  Gemini Live session starts
5. AI greets the lead autonomously    ──►  Real-time conversation begins
6. AI books appointment via tools     ──►  Supabase + Cal.com + SMS + WhatsApp
7. AI ends call                       ──►  Call logged with duration & outcome
```

---

## 🛠 Tech Stack

| Component | Technology | Purpose |
|---|---|---|
| **AI Voice Engine** | Google Gemini 3.1 Flash Live | Real-time multimodal voice AI (native audio, no STT/TTS needed) |
| **WebRTC Engine** | LiveKit Cloud | Low-latency audio transport between AI and SIP |
| **SIP Telephony** | Vobiz | PSTN calling infrastructure (India & international) |
| **Backend API** | FastAPI + Uvicorn | REST API, campaign scheduler, dashboard serving |
| **Database** | Supabase (PostgreSQL) | All state: calls, appointments, campaigns, settings, CRM |
| **Campaign Scheduler** | APScheduler | Cron-based campaign execution (once/daily/weekdays) |
| **SMS** | Twilio | Booking confirmation messages |
| **WhatsApp** | Twilio WhatsApp API | Lead notifications & broker hot-lead alerts |
| **Calendar** | Cal.com | Optional appointment sync |
| **Recording** | S3 / Supabase Storage | Call audio recording via LiveKit Egress |
| **Dashboard** | Single-file HTML + Chart.js | 13-tab premium SaaS dashboard |

---

## 💰 Cost Breakdown

| Service | Cost per Minute (₹) | Cost per Minute ($) |
|---|---|---|
| Vobiz SIP Calling | ₹1.00 | $0.012 |
| LiveKit Cloud | ₹0.17 | $0.002 |
| Gemini Multimodal Live | ₹0.03 | $0.0004 |
| **Total** | **₹1.20** | **$0.014** |

**Example:** 100 calls/day × 3 min avg = **₹360/day** or **₹10,800/month**

---

## ⚡ Quick Start

### Prerequisites

- Python 3.11+
- Docker (for VPS deployment)
- Accounts: [LiveKit Cloud](https://cloud.livekit.io), [Google AI Studio](https://aistudio.google.com), [Vobiz](https://vobiz.ai), [Supabase](https://supabase.com)

### 1. Clone & Configure

```bash
git clone https://github.com/mokalrohit246-lang/Kaamdhenuoutbondai-2.git
cd Kaamdhenuoutbondai-2
cp .env.example .env
nano .env   # Fill in your API keys
```

### 2. Set Up Database

Run the SQL in your Supabase Dashboard → SQL Editor:

```bash
# Copy contents of supabase_schema.sql and execute in Supabase SQL Editor
```

### 3. Run Locally

```bash
pip install -r requirements.txt
bash start.sh
```

### 4. Deploy to VPS (Docker)

```bash
docker compose up -d --build
```

Dashboard available at: `http://your-vps-ip:8000`

---

## 🔐 Environment Variables

VPS environment variables are the **single source of truth**. The `.env` file is only a fallback.

### Required

| Variable | Description | Example |
|---|---|---|
| `LIVEKIT_URL` | LiveKit Cloud WebSocket URL | `wss://your-project.livekit.cloud` |
| `LIVEKIT_API_KEY` | LiveKit API Key | `APIxxxxxxxxx` |
| `LIVEKIT_API_SECRET` | LiveKit API Secret | `xxxxxxxxxxxxxxxx` |
| `GOOGLE_API_KEY` | Gemini API Key | `AIzaSyxxxxxxxxxx` |
| `SUPABASE_URL` | Supabase Project URL | `https://xxxxx.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase Service Role Key | `eyJhbGci...` |
| `VOBIZ_SIP_DOMAIN` | Vobiz SIP Domain | `xxxxx.sip.vobiz.ai` |
| `VOBIZ_USERNAME` | Vobiz SIP Username | `your_username` |
| `VOBIZ_PASSWORD` | Vobiz SIP Password | `your_password` |
| `VOBIZ_OUTBOUND_NUMBER` | Caller ID number | `+919876543210` |
| `OUTBOUND_TRUNK_ID` | LiveKit SIP Trunk ID | `ST_xxxxxxxx` |

### Optional

| Variable | Description | Default |
|---|---|---|
| `GEMINI_MODEL` | Gemini model name | `gemini-3.1-flash-live-preview` |
| `GEMINI_TTS_VOICE` | AI voice name | `Aoede` |
| `USE_GEMINI_REALTIME` | Use Gemini Live mode | `true` |
| `TWILIO_WA_SID` | Twilio WhatsApp Account SID | — |
| `TWILIO_WA_TOKEN` | Twilio WhatsApp Auth Token | — |
| `TWILIO_WA_FROM` | WhatsApp sender number | `whatsapp:+14155238886` |
| `TWILIO_ACCOUNT_SID` | Twilio SMS Account SID | — |
| `TWILIO_AUTH_TOKEN` | Twilio SMS Auth Token | — |
| `TWILIO_FROM_NUMBER` | SMS sender number | — |
| `CALCOM_API_KEY` | Cal.com API Key | — |
| `CALCOM_EVENT_TYPE_ID` | Cal.com Event Type ID | — |
| `CALCOM_TIMEZONE` | Timezone for bookings | `Asia/Kolkata` |
| `S3_ACCESS_KEY_ID` | S3 Access Key for recordings | — |
| `S3_SECRET_ACCESS_KEY` | S3 Secret Key | — |
| `S3_ENDPOINT_URL` | S3 Endpoint URL | — |
| `S3_BUCKET` | S3 Bucket name | — |

---

## 🗄 Database Setup

Execute `supabase_schema.sql` in your **Supabase Dashboard → SQL Editor**. It creates:

| Table | Purpose |
|---|---|
| `settings` | Key-value config store (API keys, model settings) |
| `call_logs` | Every call with outcome, duration, recording URL, notes |
| `appointments` | Booked appointments with date, time, service, status |
| `campaigns` | Mass calling campaigns with scheduling and budget tracking |
| `contacts` | CRM memory — AI-extracted facts about each lead |
| `agent_profiles` | Named AI agent configurations (voice, model, tools, prompt) |
| `error_logs` | System logs from agent and server |
| `whatsapp_logs` | WhatsApp message delivery tracking |

---

## 🖥 Dashboard

Premium 13-tab SaaS dashboard served at `http://your-server:8000`:

| Tab | Features |
|---|---|
| 📊 **Stats** | KPI cards, outcomes donut chart, 14-day timeline, duration bar chart |
| 📞 **Single Call** | Dial one number with agent profile selection |
| 📋 **Batch Call** | Upload CSV, preview contacts, sequential calling with delay |
| 🚀 **Campaigns** | Create scheduled campaigns, minute budget tracking, run/pause/delete |
| 🤖 **Agents** | Create/edit agent profiles (voice, model, tools, system prompt) |
| ✏️ **AI Prompt** | Edit the system prompt with character count |
| 📅 **Appointments** | View/cancel booked appointments with date filtering |
| 📝 **Call Logs** | Paginated logs with inline notes editor and recording links |
| 👥 **CRM** | Contact list with drill-down call history per phone |
| ⚙️ **Settings** | All API keys, SIP config, WhatsApp, S3, Cal.com, tool toggles |
| 📋 **Logs** | Real-time system log viewer with level/source filtering |
| 🔧 **Setup** | Quick-start guide, cost breakdown, architecture overview |
| 💰 **API Costs** | Live latency metrics, pricing table, interactive cost calculator |

### Design

- **Theme:** Deep Obsidian (`#070A11`) with Metallic Gold (`#D4AF37`) and Sci-Fi Cyan (`#00F0FF`) accents
- **Typography:** Playfair Display (headings) + Rajdhani (data/metrics)
- **Effects:** Glassmorphism cards, neon glow borders, gradient KPI values, pulsing status indicators

---

## 🤖 AI Agent Tools

The Gemini Live agent has access to **10 function tools**:

| Tool | Description |
|---|---|
| `lookup_contact` | Retrieve prior call history and AI memory for a phone number |
| `check_availability` | Check if a date/time slot is available for booking |
| `book_appointment` | Book an appointment into Supabase (and optionally Cal.com) |
| `send_sms_confirmation` | Send SMS booking confirmation via Twilio |
| `end_call` | End the call with outcome classification and call logging |
| `transfer_to_human` | Transfer the call to a human agent via SIP REFER |
| `remember_details` | Store key facts about a lead for future call context |
| `cancel_appointment` | Cancel a previously booked Cal.com appointment |
| `qualify_and_route_lead` | Qualify lead as hot/warm and send WhatsApp alerts to lead + broker |

---

## 📡 API Endpoints

### Calls
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/call` | Place a single outbound call |
| `GET` | `/api/calls` | Get paginated call logs |
| `PATCH` | `/api/calls/{id}/notes` | Update call notes |
| `GET` | `/api/stats` | Dashboard statistics |

### Campaigns
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/campaigns` | Create a new campaign |
| `GET` | `/api/campaigns` | List all campaigns |
| `POST` | `/api/campaigns/{id}/run` | Trigger campaign immediately |
| `PATCH` | `/api/campaigns/{id}/status` | Pause/resume campaign |
| `DELETE` | `/api/campaigns/{id}` | Delete campaign |
| `POST` | `/api/campaigns/{id}/add-minutes` | Top up campaign minutes |

### Agent Profiles
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/agent-profiles` | Create agent profile |
| `GET` | `/api/agent-profiles` | List all profiles |
| `PUT` | `/api/agent-profiles/{id}` | Update profile |
| `DELETE` | `/api/agent-profiles/{id}` | Delete profile |

### Other
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/appointments` | List appointments |
| `DELETE` | `/api/appointments/{id}` | Cancel appointment |
| `GET` | `/api/crm` | CRM contact list |
| `GET` | `/api/crm/calls?phone=X` | Contact call history |
| `GET/POST` | `/api/settings` | Read/write settings |
| `GET/POST` | `/api/prompt` | Read/write system prompt |
| `GET` | `/api/logs` | System logs |
| `GET` | `/api/wa-logs` | WhatsApp message logs |
| `GET` | `/health` | Health check |
| `POST` | `/api/setup/trunk` | Create SIP trunk |

---

## 🐳 Docker Deployment

```bash
# Build and run
docker compose up -d --build

# View logs
docker compose logs -f

# Check health
curl http://localhost:8000/health

# Stop
docker compose down
```

The container runs both **FastAPI server** (port 8000) and **LiveKit agent worker** concurrently with signal-safe process supervision.

---

## 📁 Project Structure

```
OutboundAI/
├── agent.py              # LiveKit agent worker — Gemini Live voice AI
├── server.py             # FastAPI backend — REST API + campaign scheduler
├── db.py                 # Supabase database layer — all CRUD operations
├── tools.py              # 10 AI function tools (booking, SMS, WhatsApp, CRM)
├── prompts.py            # System prompt template with variable interpolation
├── ui/
│   └── index.html        # Single-file premium SaaS dashboard (13 tabs)
├── Dockerfile            # Production Docker image
├── docker-compose.yml    # 1-click VPS deployment
├── start.sh              # Dual-process startup with signal trapping
├── requirements.txt      # Python dependencies
├── supabase_schema.sql   # Database migration script
├── .env.example          # Environment variables reference
└── .gitignore
```

---

## 📄 License

This project is proprietary software. All rights reserved.

---

**Built with ❤️ for real estate & service businesses that need to scale outbound calling with AI.**
