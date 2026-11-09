#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# agentx.sh — single-command launcher for AgentX with all features enabled
#
# First-time setup:
#   cp .env.example .env
#   # fill in your API keys in .env
#   chmod +x agentx.sh
#   ./agentx.sh
#
# To pass a one-shot task instead of using the wake word:
#   ./agentx.sh "open Chrome and search for the weather"
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport
else
    echo "⚠️  No .env file found."
    echo "   Run:  cp .env.example .env  then fill in your keys."
    exit 1
fi

# ── Defaults (overridable from .env) ─────────────────────────────────────────
AGENTX_PROVIDER="${AGENTX_PROVIDER:-openai}"
AGENTX_MODEL="${AGENTX_MODEL:-gpt-5-2025-08-07}"
AGENTX_GROUND_PROVIDER="${AGENTX_GROUND_PROVIDER:-huggingface}"
AGENTX_GROUND_URL="${AGENTX_GROUND_URL:-http://localhost:8080}"
AGENTX_GROUND_MODEL="${AGENTX_GROUND_MODEL:-ui-tars-1.5-7b}"
AGENTX_GROUND_API_KEY="${AGENTX_GROUND_API_KEY:-}"
AGENTX_GROUNDING_WIDTH="${AGENTX_GROUNDING_WIDTH:-1920}"
AGENTX_GROUNDING_HEIGHT="${AGENTX_GROUNDING_HEIGHT:-1080}"
AGENTX_WAKE_PHRASE="${AGENTX_WAKE_PHRASE:-hey agent}"
AGENTX_CAMERA="${AGENTX_CAMERA:-0}"
AGENTX_NARRATE_VERBOSITY="${AGENTX_NARRATE_VERBOSITY:-full}"
PICOVOICE_ACCESS_KEY="${PICOVOICE_ACCESS_KEY:-}"

# ── OpenRouter shorthand ──────────────────────────────────────────────────────
if [[ "${AGENTX_PROVIDER}" == "open_router" && -n "${OPENROUTER_API_KEY:-}" ]]; then
    EXTRA_ARGS="--openrouter-key ${OPENROUTER_API_KEY}"
else
    EXTRA_ARGS=""
fi

# ── Build picovoice arg ───────────────────────────────────────────────────────
PICOVOICE_ARG=""
if [[ -n "$PICOVOICE_ACCESS_KEY" ]]; then
    PICOVOICE_ARG="--picovoice-key ${PICOVOICE_ACCESS_KEY}"
fi

# ── One-shot task from CLI arg ────────────────────────────────────────────────
TASK_ARG=""
if [[ $# -gt 0 ]]; then
    TASK_ARG="--task $*"
fi

# ── Print startup banner ──────────────────────────────────────────────────────
echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║            AgentX — Ambient Mode             ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
echo "  Model   : ${AGENTX_PROVIDER} / ${AGENTX_MODEL}"
echo "  Grounding: ${AGENTX_GROUND_MODEL} @ ${AGENTX_GROUND_URL}"
echo "  Wake word: \"${AGENTX_WAKE_PHRASE}\""
echo "  Camera   : /dev/video${AGENTX_CAMERA}"
echo ""
echo "  Features active:"
echo "    🎤  Wake word     — say \"${AGENTX_WAKE_PHRASE}\" to give a task"
echo "    👁️  Vision narrate — LLM describes each step from screenshots"
echo "    📋  Task chaining  — say 'first X, then Y, then Z' for multi-task"
echo "    ✋  Gestures      — open palm=PAUSE | fist=RESUME | 👍=APPROVE | 👎=CANCEL"
echo ""
echo "  Press Ctrl+C to quit."
echo ""

# ── Launch ────────────────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"

# shellcheck disable=SC2086
python -m gui_agents.s3.cli_app \
    --provider        "${AGENTX_PROVIDER}"        \
    --model           "${AGENTX_MODEL}"            \
    --ground_provider "${AGENTX_GROUND_PROVIDER}"  \
    --ground_url      "${AGENTX_GROUND_URL}"       \
    --ground_model    "${AGENTX_GROUND_MODEL}"     \
    --ground_api_key  "${AGENTX_GROUND_API_KEY}"   \
    --grounding_width  "${AGENTX_GROUNDING_WIDTH}" \
    --grounding_height "${AGENTX_GROUNDING_HEIGHT}"\
    --wake-word                                         \
    --wake-phrase       "${AGENTX_WAKE_PHRASE}"        \
    --vision-narrate                                    \
    --narrate-verbosity "${AGENTX_NARRATE_VERBOSITY}"  \
    --tts                                               \
    --gesture                                           \
    --camera          "${AGENTX_CAMERA}"           \
    --enable_reflection                             \
    ${PICOVOICE_ARG}                                \
    ${EXTRA_ARGS}                                   \
    ${TASK_ARG}
