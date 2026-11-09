"""
Screen narration — speaks what the agent is doing at each step.

Hooks into the agent run loop and converts the agent's reflection, plan,
and action code into plain spoken English so you never need to look at
the terminal while the agent works.

Usage:
    narrator = Narrator(voice_input)   # pass existing VoiceInput for TTS
    narrator.announce_step(step, info, code)
    narrator.announce_done(instruction)
    narrator.announce_fail(reason)
"""

import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from gui_agents.voice_gesture.voice_input import VoiceInput

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action code → human sentence
# ---------------------------------------------------------------------------

_ACTION_PATTERNS = [
    # pyautogui click
    (r"pyautogui\.click\(", "Clicking the screen"),
    (r"pyautogui\.doubleClick\(", "Double-clicking"),
    (r"pyautogui\.rightClick\(", "Right-clicking"),
    # pyautogui type/write
    (r'pyautogui\.write\(["\'](.{1,60})["\']', lambda m: f'Typing "{m.group(1)}"'),
    (r'pyautogui\.typewrite\(["\'](.{1,60})["\']', lambda m: f'Typing "{m.group(1)}"'),
    # pyautogui hotkey
    (r"pyautogui\.hotkey\((.+?)\)", lambda m: "Pressing " + m.group(1).replace(",", "+").replace("'", "").replace(" ", "")),
    # pyautogui scroll
    (r"pyautogui\.scroll\((-?\d+)\)", lambda m: f"Scrolling {'down' if int(m.group(1)) < 0 else 'up'}"),
    # pyautogui drag
    (r"pyautogui\.drag", "Dragging on screen"),
    # screenshot / observe only
    (r"^(done|fail|next|wait)\b", lambda m: ""),
]


def _code_to_speech(code: str) -> str:
    code = code.strip()
    for pattern, replacement in _ACTION_PATTERNS:
        m = re.search(pattern, code, re.IGNORECASE)
        if m:
            if callable(replacement):
                return replacement(m)
            return replacement
    # Generic fallback: strip Python noise, truncate
    clean = re.sub(r"[^a-zA-Z0-9 _.'\"-]", " ", code)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:80] if clean else "Taking action"


def _reflection_to_speech(reflection: str) -> str:
    """Extract the first sentence from a reflection string, capped at 120 chars."""
    if not reflection:
        return ""
    # First sentence or first 120 chars
    sentences = re.split(r"(?<=[.!?])\s+", reflection.strip())
    first = sentences[0] if sentences else reflection
    return first[:120]


# ---------------------------------------------------------------------------
# Narrator class
# ---------------------------------------------------------------------------

class Narrator:
    """
    Wraps a VoiceInput instance and provides high-level narration calls
    for the agent run loop.

    Args:
        voice: VoiceInput instance (must have tts_enabled=True to speak).
        verbosity: "full" speaks reflection + action; "action" speaks action
                   only; "minimal" speaks only task start/end.
    """

    def __init__(self, voice: "VoiceInput", verbosity: str = "full"):
        self._voice = voice
        self._verbosity = verbosity

    def _say(self, text: str):
        if text:
            self._voice.speak(text)

    def announce_start(self, instruction: str):
        short = instruction[:100] + ("…" if len(instruction) > 100 else "")
        self._say(f"Starting task: {short}")

    def announce_step(self, step: int, info: dict, code: list):
        """Call after agent.predict() with the returned info and code."""
        if not code:
            return

        action_text = _code_to_speech(code[0])

        if self._verbosity == "minimal":
            if step == 0:
                self._say("Working on it.")
            return

        if self._verbosity in ("action", "full"):
            if action_text:
                self._say(f"Step {step + 1}: {action_text}.")

        if self._verbosity == "full":
            reflection = info.get("reflection", "")
            snippet = _reflection_to_speech(str(reflection))
            if snippet and snippet.lower() != action_text.lower():
                self._say(snippet)

    def announce_wait(self):
        self._say("Waiting a moment.")

    def announce_done(self, instruction: str = ""):
        self._say("Task complete.")

    def announce_fail(self, reason: str = ""):
        msg = "The task could not be completed."
        if reason:
            msg += f" Reason: {reason[:80]}"
        self._say(msg)

    def announce_pause(self):
        self._say("Paused. Say 'resume' or use a resume gesture to continue.")

    def announce_cancel(self):
        self._say("Task cancelled.")
