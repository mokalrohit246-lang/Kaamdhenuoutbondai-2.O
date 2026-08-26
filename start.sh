#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "=========================================="
echo "🚀 Starting OutboundAI SaaS Platform"
echo "=========================================="

# Optional fallback: load .env only for variables not already set in VPS environment
if [ -f ".env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line=$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        case "$line" in
            \#*|"") continue ;;
        esac
        key="${line%%=*}"
        val="${line#*=}"
        val=$(echo "$val" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
        # Only export if NOT already defined in VPS environment:
        if [ -z "${!key+x}" ] || [ -z "${!key}" ]; then
            export "$key"="$val"
        fi
    done < .env
fi

echo "📋 Active VPS Configuration:"
echo "   LiveKit URL:      ${LIVEKIT_URL:-[Not Set]}"
echo "   LiveKit Key:      ${LIVEKIT_API_KEY:+[Configured (hidden)]}"
echo "   Gemini Model:     ${GEMINI_MODEL:-gemini-2.0-flash}"
echo "   Gemini Voice:     ${GEMINI_TTS_VOICE:-hi-IN-Neural2-A}"
echo "   Gemini Realtime:  ${USE_GEMINI_REALTIME:-false}"
echo "   Supabase URL:     ${SUPABASE_URL:-[Not Set]}"
echo "   Vobiz SIP:        ${VOBIZ_SIP_DOMAIN:-[Not Set]}"
echo "   Outbound Trunk:   ${OUTBOUND_TRUNK_ID:-[Not Set]}"
echo "   WhatsApp From:    ${TWILIO_WA_FROM:-[Not Set]}"
echo "=========================================="

export PYTHONUNBUFFERED=1

echo "🌐 Starting FastAPI Server & AI Agent Supervisor on 0.0.0.0:8000..."
exec uvicorn server:app --host 0.0.0.0 --port 8000
