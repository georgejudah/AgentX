"""Thread-safe event bus between the agent loop and the web UI.

The agent publishes events (task started, step, narration, ...) and the UI
server forwards them to connected browsers. The UI sends commands (submit,
pause, resume, cancel) back through a single handler the CLI registers.
Publishing is a cheap no-op when no UI is running.
"""

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

HISTORY_PATH = Path(os.environ.get("AGENTX_HOME", Path.home() / ".agentx")) / "history.jsonl"


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict], None]] = []
        self._command_handler: Optional[Callable[[str, dict], None]] = None
        self.state = {
            "status": "idle",
            "config": {},
            "task": None,
            "queue": [],
            "narration": deque(maxlen=50),
            "gesture": None,
            "voice": {"state": "off", "heard": None},
        }

    def subscribe(self, fn: Callable[[dict], None]):
        with self._lock:
            self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[dict], None]):
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def set_command_handler(self, fn: Callable[[str, dict], None]):
        self._command_handler = fn

    def command(self, name: str, payload: Optional[dict] = None) -> bool:
        if self._command_handler is None:
            return False
        self._command_handler(name, payload or {})
        return True

    def publish(self, type_: str, **data):
        event = {"type": type_, "ts": time.time(), **data}
        with self._lock:
            self._apply(event)
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(event)
            except Exception:
                pass

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self.state)
            s["narration"] = list(self.state["narration"])
            return s

    def _apply(self, e: dict):
        t = e["type"]
        s = self.state
        if t == "status":
            s["status"] = e["state"]
        elif t == "config":
            s["config"] = e["config"]
        elif t == "task_started":
            s["task"] = {k: e[k] for k in ("id", "instruction", "index", "total")} | {
                "step": 0, "max_steps": e.get("max_steps", 15), "action": None, "started": e["ts"],
            }
            s["status"] = "working"
        elif t == "step" and s["task"]:
            s["task"]["step"] = e["step"]
            s["task"]["action"] = e.get("action")
        elif t == "task_finished":
            s["task"] = None
            _append_history(e)
        elif t == "queue":
            s["queue"] = e["items"]
        elif t == "narration":
            s["narration"].append({"text": e["text"], "ts": e["ts"]})
        elif t == "gesture":
            s["gesture"] = {"name": e["name"], "ts": e["ts"]}
        elif t == "voice":
            s["voice"] = {"state": e["state"], "heard": e.get("heard")}


def _append_history(e: dict):
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {k: e.get(k) for k in ("id", "instruction", "result", "steps", "duration", "ts")}
        with HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def read_history(limit: int = 50) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    rows = []
    with HISTORY_PATH.open(encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:][::-1]


bus = EventBus()
