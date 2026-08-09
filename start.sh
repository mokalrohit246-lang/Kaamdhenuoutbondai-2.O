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
echo "   Gemini Model:     ${GEMINI_MODEL:-gemini-3.1-flash-live-preview}"
echo "   Gemini Voice:     ${GEMINI_TTS_VOICE:-Aoede}"
echo "   Gemini Realtime:  ${USE_GEMINI_REALTIME:-true}"
echo "   Supabase URL:     ${SUPABASE_URL:-[Not Set]}"
echo "   Vobiz SIP:        ${VOBIZ_SIP_DOMAIN:-[Not Set]}"
echo "   Outbound Trunk:   ${OUTBOUND_TRUNK_ID:-[Not Set]}"
echo "   WhatsApp From:    ${TWILIO_WA_FROM:-[Not Set]}"
echo "=========================================="

# Trap termination signals to gracefully stop both processes
cleanup() {
    echo "🛑 Shutting down services..."
    kill $SERVER_PID 2>/dev/null || true
    kill $AGENT_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "🌐 Starting FastAPI Server on 0.0.0.0:8000..."
uvicorn server:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

sleep 2

echo "🤖 Starting LiveKit Agent Worker (outbound-caller)..."
python agent.py start &
AGENT_PID=$!

# Wait for either process to terminate
wait -n $SERVER_PID $AGENT_PID || true

cleanup
