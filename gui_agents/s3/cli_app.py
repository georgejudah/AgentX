import argparse
import datetime
import io
import logging
import os
import platform
import pyautogui
import signal
import sys
import time
from typing import Optional

from PIL import Image

from gui_agents.s3.agents.grounding import OSWorldACI
from gui_agents.s3.agents.agent_s import AgentS3
from gui_agents.s3.utils.local_env import LocalEnv

current_platform = platform.system().lower()

# Global flag to track pause state for debugging
paused = False

# Optional voice/gesture/narration controllers (set in main() when flags are passed)
_voice_input = None
_gesture_ctrl = None
_narrator = None
_vision_narrator = None
_wake_listener = None
_task_queue = None

# Queue for tasks issued via wake word
import queue as _queue
_wake_task_queue: _queue.Queue = _queue.Queue()


def get_char():
    """Get a single character from stdin without pressing Enter"""
    try:
        # Import termios and tty on Unix-like systems
        if platform.system() in ["Darwin", "Linux"]:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(sys.stdin.fileno())
                ch = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            return ch
        else:
            # Windows fallback
            import msvcrt

            return msvcrt.getch().decode("utf-8", errors="ignore")
    except:
        return input()  # Fallback for non-terminal environments


def signal_handler(signum, frame):
    """Handle Ctrl+C signal for debugging during agent execution"""
    global paused

    if not paused:
        print("\n\n🔸 Agent-S Workflow Paused 🔸")
        print("=" * 50)
        print("Options:")
        print("  • Press Ctrl+C again to quit")
        print("  • Press Esc to resume workflow")
        print("=" * 50)

        paused = True

        while paused:
            try:
                print("\n[PAUSED] Waiting for input... ", end="", flush=True)
                char = get_char()

                if ord(char) == 3:  # Ctrl+C
                    print("\n\n🛑 Exiting Agent-S...")
                    sys.exit(0)
                elif ord(char) == 27:  # Esc
                    print("\n\n▶️  Resuming Agent-S workflow...")
                    paused = False
                    break
                else:
                    print(f"\n   Unknown command: '{char}' (ord: {ord(char)})")

            except KeyboardInterrupt:
                print("\n\n🛑 Exiting Agent-S...")
                sys.exit(0)
    else:
        # Already paused, second Ctrl+C means quit
        print("\n\n🛑 Exiting Agent-S...")
        sys.exit(0)


# Set up signal handler for Ctrl+C
signal.signal(signal.SIGINT, signal_handler)

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

datetime_str: str = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")

log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)

file_handler = logging.FileHandler(
    os.path.join("logs", "normal-{:}.log".format(datetime_str)), encoding="utf-8"
)
debug_handler = logging.FileHandler(
    os.path.join("logs", "debug-{:}.log".format(datetime_str)), encoding="utf-8"
)
stdout_handler = logging.StreamHandler(sys.stdout)
sdebug_handler = logging.FileHandler(
    os.path.join("logs", "sdebug-{:}.log".format(datetime_str)), encoding="utf-8"
)

file_handler.setLevel(logging.INFO)
debug_handler.setLevel(logging.DEBUG)
stdout_handler.setLevel(logging.INFO)
sdebug_handler.setLevel(logging.DEBUG)

formatter = logging.Formatter(
    fmt="\x1b[1;33m[%(asctime)s \x1b[31m%(levelname)s \x1b[32m%(module)s/%(lineno)d-%(processName)s\x1b[1;33m] \x1b[0m%(message)s"
)
file_handler.setFormatter(formatter)
debug_handler.setFormatter(formatter)
stdout_handler.setFormatter(formatter)
sdebug_handler.setFormatter(formatter)

stdout_handler.addFilter(logging.Filter("desktopenv"))
sdebug_handler.addFilter(logging.Filter("desktopenv"))

logger.addHandler(file_handler)
logger.addHandler(debug_handler)
logger.addHandler(stdout_handler)
logger.addHandler(sdebug_handler)

platform_os = platform.system()


def show_permission_dialog(code: str, action_description: str):
    """Show a platform-specific permission dialog and return True if approved."""
    if platform.system() == "Darwin":
        result = os.system(
            f'osascript -e \'display dialog "Do you want to execute this action?\n\n{code} which will try to {action_description}" with title "Action Permission" buttons {{"Cancel", "OK"}} default button "OK" cancel button "Cancel"\''
        )
        return result == 0
    elif platform.system() == "Linux":
        result = os.system(
            f'zenity --question --title="Action Permission" --text="Do you want to execute this action?\n\n{code}" --width=400 --height=200'
        )
        return result == 0
    return False


def scale_screen_dimensions(width: int, height: int, max_dim_size: int):
    scale_factor = min(max_dim_size / width, max_dim_size / height, 1)
    safe_width = int(width * scale_factor)
    safe_height = int(height * scale_factor)
    return safe_width, safe_height


def _handle_gesture_commands():
    """Poll gesture queue and apply any pending gesture commands."""
    global paused, _gesture_ctrl, _narrator
    if _gesture_ctrl is None:
        return
    from gui_agents.voice_gesture.gesture_control import GestureCommand

    cmd = _gesture_ctrl.get_command(timeout=0)
    while cmd is not None:
        if cmd == GestureCommand.PAUSE_AGENT:
            if not paused:
                print("\n✋ Gesture: PAUSED")
                paused = True
                if _narrator:
                    _narrator.announce_pause()
        elif cmd == GestureCommand.RESUME_AGENT:
            if paused:
                print("\n✊ Gesture: RESUMED")
                paused = False
        elif cmd == GestureCommand.APPROVE_ACTION:
            print("\n👍 Gesture: APPROVE (continuing…)")
        elif cmd == GestureCommand.CANCEL_TASK:
            print("\n👎 Gesture: CANCEL — stopping task.")
            if _narrator:
                _narrator.announce_cancel()
            raise KeyboardInterrupt("Gesture cancel")
        elif cmd == GestureCommand.NEXT_STEP:
            print("\n☝️  Gesture: NEXT STEP")
        elif cmd == GestureCommand.SCREENSHOT:
            print("\n✌️  Gesture: SCREENSHOT requested")
        cmd = _gesture_ctrl.get_command(timeout=0)


def run_agent(
    agent,
    instruction: str,
    scaled_width: int,
    scaled_height: int,
    task_index: int = 1,
    task_total: int = 1,
):
    global paused, _narrator, _vision_narrator
    obs = {}
    traj = "Task:\n" + instruction
    subtask_traj = ""

    # Announce start, with task position if chaining
    if task_total > 1:
        prefix = f"Task {task_index} of {task_total}: "
        print(f"\n📌  {prefix}{instruction}")
        if _narrator:
            _narrator._say(f"{prefix}{instruction}")
        elif _vision_narrator:
            _vision_narrator._voice.speak(f"{prefix}{instruction}")
    elif _narrator:
        _narrator.announce_start(instruction)
    elif _vision_narrator:
        _vision_narrator._voice.speak(f"Starting: {instruction}")

    for step in range(15):
        # Check gesture queue each step
        _handle_gesture_commands()
        # Check if we're in paused state and wait
        while paused:
            _handle_gesture_commands()
            time.sleep(0.1)
        # Get screen shot using pyautogui
        screenshot = pyautogui.screenshot()
        screenshot = screenshot.resize((scaled_width, scaled_height), Image.LANCZOS)

        # Save the screenshot to a BytesIO object
        buffered = io.BytesIO()
        screenshot.save(buffered, format="PNG")

        # Get the byte value of the screenshot
        screenshot_bytes = buffered.getvalue()
        # Convert to base64 string.
        obs["screenshot"] = screenshot_bytes

        # Vision narrator: capture before-screenshot
        if _vision_narrator:
            _vision_narrator.set_before(screenshot_bytes)

        # Check again for pause state before prediction
        while paused:
            time.sleep(0.1)

        print(f"\n🔄 Step {step + 1}/15: Getting next action from agent...")

        # Get next action code from the agent
        info, code = agent.predict(instruction=instruction, observation=obs)

        # Regex narrator (immediate, no latency)
        if _narrator:
            _narrator.announce_step(step, info, code)

        if "done" in code[0].lower() or "fail" in code[0].lower():
            if _narrator:
                if "fail" in code[0].lower():
                    _narrator.announce_fail()
                else:
                    _narrator.announce_done(instruction)
            elif _vision_narrator:
                if "fail" in code[0].lower():
                    _vision_narrator.narrate_fail()
                else:
                    _vision_narrator.narrate_done()
            if platform.system() == "Darwin":
                os.system(
                    f'osascript -e \'display dialog "Task Completed" with title "OpenACI Agent" buttons "OK" default button "OK"\''
                )
            elif platform.system() == "Linux":
                os.system(
                    f'zenity --info --title="OpenACI Agent" --text="Task Completed" --width=200 --height=100'
                )

            break

        if "next" in code[0].lower():
            continue

        if "wait" in code[0].lower():
            print("⏳ Agent requested wait...")
            if _narrator:
                _narrator.announce_wait()
            time.sleep(5)
            continue

        else:
            time.sleep(1.0)
            print("EXECUTING CODE:", code[0])

            # Check for pause state before execution
            while paused:
                time.sleep(0.1)

            # Execute action
            exec(code[0])
            time.sleep(1.0)

            # Vision narrator: capture after-screenshot and enqueue narration job
            if _vision_narrator:
                after_shot = pyautogui.screenshot()
                after_shot = after_shot.resize((scaled_width, scaled_height), Image.LANCZOS)
                after_buf = io.BytesIO()
                after_shot.save(after_buf, format="PNG")
                _vision_narrator.narrate_step(step, code, after_buf.getvalue())

            # Update task and subtask trajectories
            if "reflection" in info and "executor_plan" in info:
                traj += (
                    "\n\nReflection:\n"
                    + str(info["reflection"])
                    + "\n\n----------------------\n\nPlan:\n"
                    + info["executor_plan"]
                )


def main():
    parser = argparse.ArgumentParser(description="Run AgentS3 with specified model.")
    parser.add_argument(
        "--provider",
        type=str,
        default="openai",
        help="Specify the provider to use (e.g., openai, anthropic, etc.)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5-2025-08-07",
        help="Specify the model to use (e.g., gpt-5-2025-08-07)",
    )
    parser.add_argument(
        "--model_url",
        type=str,
        default="",
        help="The URL of the main generation model API.",
    )
    parser.add_argument(
        "--model_api_key",
        type=str,
        default="",
        help="The API key of the main generation model.",
    )
    parser.add_argument(
        "--model_temperature",
        type=float,
        default=None,
        help="Temperature to fix the generation model at (e.g. o3 can only be run with 1.0)",
    )

    # Grounding model config: Self-hosted endpoint based (required)
    parser.add_argument(
        "--ground_provider",
        type=str,
        required=True,
        help="The provider for the grounding model",
    )
    parser.add_argument(
        "--ground_url",
        type=str,
        required=True,
        help="The URL of the grounding model",
    )
    parser.add_argument(
        "--ground_api_key",
        type=str,
        default="",
        help="The API key of the grounding model.",
    )
    parser.add_argument(
        "--ground_model",
        type=str,
        required=True,
        help="The model name for the grounding model",
    )
    parser.add_argument(
        "--grounding_width",
        type=int,
        required=True,
        help="Width of screenshot image after processor rescaling",
    )
    parser.add_argument(
        "--grounding_height",
        type=int,
        required=True,
        help="Height of screenshot image after processor rescaling",
    )

    # AgentS3 specific arguments
    parser.add_argument(
        "--max_trajectory_length",
        type=int,
        default=8,
        help="Maximum number of image turns to keep in trajectory",
    )
    parser.add_argument(
        "--enable_reflection",
        action="store_true",
        default=True,
        help="Enable reflection agent to assist the worker agent",
    )
    parser.add_argument(
        "--enable_local_env",
        action="store_true",
        default=False,
        help="Enable local coding environment for code execution (WARNING: Executes arbitrary code locally)",
    )
    parser.add_argument(
        "--task",
        type=str,
        help="The task instruction for Agent-S3 to perform.",
    )

    # ---- Voice mode ----
    parser.add_argument(
        "--voice",
        action="store_true",
        default=False,
        help="Enable voice input (microphone) instead of keyboard for task queries.",
    )
    parser.add_argument(
        "--tts",
        action="store_true",
        default=False,
        help="Enable text-to-speech feedback when voice mode is active.",
    )
    parser.add_argument(
        "--narrate",
        action="store_true",
        default=False,
        help="Speak every step the agent takes aloud using fast regex narration (requires --tts).",
    )
    parser.add_argument(
        "--narrate-verbosity",
        type=str,
        default="full",
        dest="narrate_verbosity",
        choices=["full", "action", "minimal"],
        help="Narration detail level: full (reflection+action), action, or minimal.",
    )
    parser.add_argument(
        "--vision-narrate",
        action="store_true",
        default=False,
        dest="vision_narrate",
        help=(
            "Use the LLM to describe what visually changed on screen after each step "
            "(richer than --narrate; runs async so it never blocks; requires --tts)."
        ),
    )
    parser.add_argument(
        "--whisper-model",
        type=str,
        default="base",
        dest="whisper_model",
        help="Whisper model size for offline STT fallback (tiny/base/small/medium/large).",
    )

    # ---- Wake word ----
    parser.add_argument(
        "--wake-word",
        action="store_true",
        default=False,
        dest="wake_word",
        help="Enable always-on wake word listener ('hey agent' by default).",
    )
    parser.add_argument(
        "--wake-phrase",
        type=str,
        default="hey agent",
        dest="wake_phrase",
        help="The wake phrase to listen for (default: 'hey agent').",
    )
    parser.add_argument(
        "--picovoice-key",
        type=str,
        default="",
        dest="picovoice_key",
        help="Picovoice access key for porcupine wake word (get free at picovoice.ai/console).",
    )

    # ---- Gesture mode ----
    parser.add_argument(
        "--gesture",
        action="store_true",
        default=False,
        help="Enable webcam gesture control (MediaPipe Hands).",
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=0,
        dest="camera_index",
        help="Webcam device index for gesture control (default 0).",
    )
    parser.add_argument(
        "--gesture-preview",
        action="store_true",
        default=False,
        dest="gesture_preview",
        help="Show live webcam preview window with gesture overlay.",
    )

    # ---- OpenRouter convenience ----
    parser.add_argument(
        "--openrouter-key",
        type=str,
        default="",
        dest="openrouter_key",
        help="OpenRouter API key (shorthand; sets --provider open_router and fills --model_api_key).",
    )

    args = parser.parse_args()

    # OpenRouter shorthand: set provider + key automatically
    if args.openrouter_key:
        if not args.model_url:
            args.model_url = "https://openrouter.ai/api/v1"
        args.provider = "open_router"
        if not args.model_api_key:
            args.model_api_key = args.openrouter_key

    # Re-scales screenshot size to ensure it fits in UI-TARS context limit
    screen_width, screen_height = pyautogui.size()
    scaled_width, scaled_height = scale_screen_dimensions(
        screen_width, screen_height, max_dim_size=2400
    )

    # Load the general engine params
    engine_params = {
        "engine_type": args.provider,
        "model": args.model,
        "base_url": args.model_url,
        "api_key": args.model_api_key,
        "temperature": getattr(args, "model_temperature", None),
    }

    # Load the grounding engine from a custom endpoint
    engine_params_for_grounding = {
        "engine_type": args.ground_provider,
        "model": args.ground_model,
        "base_url": args.ground_url,
        "api_key": args.ground_api_key,
        "grounding_width": args.grounding_width,
        "grounding_height": args.grounding_height,
    }

    # Initialize environment based on user preference
    local_env = None
    if args.enable_local_env:
        print(
            "⚠️  WARNING: Local coding environment enabled. This will execute arbitrary code locally!"
        )
        local_env = LocalEnv()

    grounding_agent = OSWorldACI(
        env=local_env,
        platform=current_platform,
        engine_params_for_generation=engine_params,
        engine_params_for_grounding=engine_params_for_grounding,
        width=screen_width,
        height=screen_height,
    )

    agent = AgentS3(
        engine_params,
        grounding_agent,
        platform=current_platform,
        max_trajectory_length=args.max_trajectory_length,
        enable_reflection=args.enable_reflection,
    )

    # ---- Voice input setup ----
    global _voice_input, _narrator, _vision_narrator, _wake_listener, _task_queue
    need_tts = args.tts or args.narrate or args.vision_narrate or args.wake_word
    if args.voice or args.wake_word or args.narrate or args.vision_narrate:
        from gui_agents.voice_gesture.voice_input import VoiceInput
        _voice_input = VoiceInput(
            tts=need_tts,
            whisper_model=args.whisper_model,
        )
        if args.voice:
            print("🎙️  Voice mode enabled. Speak your task when prompted.")
        if need_tts:
            print("🔊  Text-to-speech enabled.")

    # ---- Regex narrator setup ----
    if args.narrate and _voice_input:
        from gui_agents.voice_gesture.narrator import Narrator
        _narrator = Narrator(_voice_input, verbosity=args.narrate_verbosity)
        print(f"📢  Narration enabled (verbosity: {args.narrate_verbosity}).")

    # ---- Vision narrator setup ----
    if args.vision_narrate and _voice_input:
        from gui_agents.voice_gesture.vision_narrator import VisionNarrator
        _vision_narrator = VisionNarrator(
            engine_params=engine_params,
            voice=_voice_input,
            fallback_narrator=_narrator,  # speaks regex description immediately while vision catches up
        )
        _vision_narrator.start()
        print("👁️  Vision narration enabled — LLM describes each step from screenshots.")

    # ---- Task queue setup ----
    _task_queue = None
    from gui_agents.voice_gesture.task_queue import TaskQueue
    _task_queue = TaskQueue(engine_params=engine_params)

    # ---- Gesture control setup ----
    global _gesture_ctrl
    if args.gesture:
        from gui_agents.voice_gesture.gesture_control import GestureControl
        _gesture_ctrl = GestureControl(
            camera_index=args.camera_index,
            show_preview=args.gesture_preview,
        )
        _gesture_ctrl.start()
        print("✋  Gesture control enabled.")
        print("    Open palm=PAUSE | Fist=RESUME | 👍=APPROVE | 👎=CANCEL | ✌️=SCREENSHOT | ☝️=NEXT")

    task = args.task

    # ---- Wake word setup ----
    if args.wake_word:
        from gui_agents.voice_gesture.wake_word import WakeWordListener

        def _on_wake():
            if _voice_input:
                _voice_input.speak("I'm listening.")
            spoken = _voice_input.listen(prompt="Speak your task now…") if _voice_input else None
            if spoken:
                print(f"\n🗣  Wake task: {spoken}")
                _wake_task_queue.put(spoken)

        _wake_listener = WakeWordListener(
            on_wake=_on_wake,
            phrase=args.wake_phrase,
            access_key=args.picovoice_key,
        )
        _wake_listener.start()
        print(f'🎤  Wake word active — say "{args.wake_phrase}" anytime to give a task.')

    def _run_task_queue():
        """Drain _task_queue, running each task with position announcements."""
        total = _task_queue.size()
        while not _task_queue.empty():
            task_text, idx, full_total = _task_queue.next()
            agent.reset()
            run_agent(
                agent, task_text, scaled_width, scaled_height,
                task_index=idx, task_total=full_total,
            )
        # After all tasks speak a summary if chaining
        if total > 1:
            msg = f"All {total} tasks complete."
            print(f"\n✅  {msg}")
            if _voice_input:
                _voice_input.speak(msg)

    try:
        # ---- Single task from --task flag ----
        if isinstance(task, str) and task.strip():
            _task_queue.enqueue(task)
            _run_task_queue()
            if not args.wake_word:
                return

        # ---- Main interaction loop ----
        while True:
            # Priority: wake-word queue → voice listen → keyboard
            if args.wake_word:
                print("\n⏳  Waiting for wake word or Ctrl+C to quit…")
                try:
                    while True:
                        try:
                            query = _wake_task_queue.get(timeout=0.5)
                            break
                        except _queue.Empty:
                            continue
                except KeyboardInterrupt:
                    break
            elif args.voice and _voice_input:
                query = _voice_input.listen(prompt="Say your task…")
                if not query:
                    print("No speech detected, try again.")
                    continue
            else:
                query = input("Query: ")

            # Parse for multi-task chaining, then run queue
            _task_queue.enqueue(query)
            _run_task_queue()

            # In wake-word mode loop forever; otherwise ask to continue
            if not args.wake_word:
                if _voice_input and args.voice:
                    _voice_input.speak("Done. Say another task or say quit.")
                    cont = _voice_input.listen(prompt="Say 'yes' to continue or 'quit' to stop…")
                    if cont and "quit" in cont.lower():
                        break
                else:
                    response = input("Would you like to provide another query? (y/n): ")
                    if response.lower() != "y":
                        break

    finally:
        if _wake_listener:
            _wake_listener.stop()
        if _vision_narrator:
            _vision_narrator.stop()
        if _gesture_ctrl:
            _gesture_ctrl.stop()
        if _voice_input:
            _voice_input.close()


if __name__ == "__main__":
    main()
