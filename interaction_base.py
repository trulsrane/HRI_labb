#!/usr/bin/env python
# =============================================================================
#  HRI_lab_Pepper — Bridge-Building Game · State-Machine Base
# =============================================================================
"""
A Pepper robot guides a person through solving bridge-building puzzles,
giving hints when asked (or when the user goes quiet), and checking solutions.

Architecture
------------
    Context       — shared blackboard (robot services + BridgeGame instance)
    BridgeGame    — pure game logic: problems, hints, solution checking
    State         — base class with on_enter / update / on_exit
    StateMachine  — tick loop + transitions

Two-layer design — the outer loop is the robot's session lifecycle, the
inner loop is the game itself:

    OUTER (session lifecycle):
        Idle → WaitForPerson → Intro → [ GAME STATES ] → Farewell → (Idle)

    INNER (game loop, inside the brackets above):
        PresentProblem → WaitForUserAction ──┬── "hint"  ──→ GiveHint ──┐
                                             │                           │
                                             ├── "done"  ──→ CheckSolution
                                             │                  │     │
                                             │            wrong │     │ correct
                                             │                  │     │
                                             └── silence ──→ GiveHint  ↓
                                                                   Celebrate
                                                                       │
                                                               (→ Farewell)

Usage
-----
    python interaction_base.py --dry-run
    python interaction_base.py --url tcp://ROBOT_IP:9559
"""

from __future__ import annotations

import argparse
import json as _json
import queue
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── Robot drivers ─────────────────────────────────────────────────────────────
try:
    from HRI_lab_Pepper.session import PepperSession
    from HRI_lab_Pepper.speech.tts import TextToSpeech
    from HRI_lab_Pepper.speech.stt import SpeechToText
    from HRI_lab_Pepper.vision.camera import PepperCamera
    from HRI_lab_Pepper.vision.human_detection import HumanDetector
    from HRI_lab_Pepper.tablet import TabletService
    from HRI_lab_Pepper.motion.posture import RobotPosture
    from HRI_lab_Pepper.motion.leds import RobotLEDs
    from HRI_lab_Pepper.motion.animation_player import AnimationPlayer
except ImportError as exc:
    print(f"[SM] Driver import warning: {exc}  (dry-run will still work)")


# ──────────────────────────────────────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[SM] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
#  BridgeGame — pure game logic, no robot code
# ══════════════════════════════════════════════════════════════════════════════

class BridgeGame:
    """
    Holds the problem database, tracks hint level, and checks solutions.

    Kept deliberately separate from the states so you can unit-test the rules
    without a robot, and so changing the game data never touches the dialog.
    """

    # TODO: expand the database — these are just starter examples.
    PROBLEMS: List[Dict] = [
        {
            "id": "arch_bridge",
            "description": (
                "Here is your first challenge. "
                "Build an arch bridge using five wooden blocks "
                "that spans a gap of about twenty centimetres."
            ),
            # The user's spoken/typed solution has to contain at least
            # len(solution_keywords) - 1 of these to count as 'correct'.
            "solution_keywords": ["arch", "keystone"],
            "hints": [
                "Think about how the weight of the blocks transfers through the structure.",
                "The block at the very top of the arch is special — it's called the keystone.",
                "Arrange the other blocks so they lean inward, pushing against each other.",
                "An arch works by turning downward force into sideways compression.",
            ],
        },
        {
            "id": "suspension_bridge",
            "description": (
                "Next challenge. "
                "Design a suspension bridge using two towers and some string. "
                "How do you keep the deck level?"
            ),
            "solution_keywords": ["cable", "tower", "suspend"],
            "hints": [
                "Think about what holds the deck up — the support comes from above, not below.",
                "Main cables drape between the two towers, forming a gentle curve.",
                "Smaller vertical cables hang down from the main ones to hold up the deck.",
            ],
        },
    ]

    def __init__(self) -> None:
        self._current: Optional[Dict] = None
        self._hint_level: int = 0
        self._attempts: int = 0
        self._problem_index: int = 0   # plays problems in order

    # ── Problem management ────────────────────────────────────────────────

    def start_new_problem(self) -> str:
        """Pick the next problem and return its description for TTS."""
        self._current = self.PROBLEMS[self._problem_index % len(self.PROBLEMS)]
        self._problem_index += 1
        self._hint_level = 0
        self._attempts = 0
        log(f"[GAME] starting problem: {self._current['id']}")
        return self._current["description"]

    def has_more_problems(self) -> bool:
        return self._problem_index < len(self.PROBLEMS)

    # ── Hints ─────────────────────────────────────────────────────────────

    def get_next_hint(self) -> str:
        hints = self._current["hints"]
        if self._hint_level < len(hints):
            hint = hints[self._hint_level]
            self._hint_level += 1
            return hint
        return "I've given you every hint I have — you're on your own now!"

    def hints_exhausted(self) -> bool:
        return self._hint_level >= len(self._current["hints"])

    # ── Solution checking ─────────────────────────────────────────────────

    def check_solution(self, answer: str) -> str:
        """
        Returns one of 'correct', 'partial', 'wrong'.

        Simple keyword-based check — replace with something smarter
        (e.g. fuzzy matching or an LLM classifier) if you want.
        """
        self._attempts += 1
        if not answer:
            return "wrong"
        text = answer.lower()
        keywords = self._current["solution_keywords"]
        matches = sum(1 for kw in keywords if kw in text)
        if matches >= len(keywords) - (1 if len(keywords) > 1 else 0):
            return "correct"
        if matches >= 1:
            return "partial"
        return "wrong"

    @property
    def current_id(self) -> str:
        return self._current["id"] if self._current else ""

    @property
    def hint_level(self) -> int:
        return self._hint_level

    @property
    def attempts(self) -> int:
        return self._attempts


# ══════════════════════════════════════════════════════════════════════════════
#  Context — shared blackboard
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Context:
    """
    Shared state passed to every State. Holds robot services + session data.

        Access services:     ctx.tts.speak(...), ctx.camera.get_frame(), ...
        Get game object:     ctx.blackboard["game"]
        Async events:        ctx.events.get(timeout=...)  (tablet taps, etc.)
        Free-form memory:    ctx.blackboard["last_utterance"] = "..."
    """

    tts:      object = None
    stt:      object = None
    camera:   object = None
    detector: object = None
    tablet:   object = None
    anim:     object = None
    posture:  object = None
    leds:     object = None

    events: queue.Queue = field(default_factory=queue.Queue)
    blackboard: Dict[str, object] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
#  State base class + StateMachine
# ══════════════════════════════════════════════════════════════════════════════

class State:
    """Base class. Override `name`, `on_enter`, `update`, `on_exit`."""

    name: str = "UnnamedState"

    def on_enter(self, ctx: Context) -> None:
        pass

    def update(self, ctx: Context) -> Optional[str]:
        """Return next state name, or None to stay in this state."""
        return None

    def on_exit(self, ctx: Context) -> None:
        pass


class StateMachine:
    def __init__(self, ctx: Context, tick_hz: float = 10.0) -> None:
        self.ctx = ctx
        self._states: Dict[str, State] = {}
        self._current: Optional[State] = None
        self._tick = 1.0 / max(tick_hz, 0.1)
        self._stop = False

    def register(self, state: State) -> None:
        if not state.name or state.name == "UnnamedState":
            raise ValueError(f"State {type(state).__name__} must set a unique `name`")
        self._states[state.name] = state

    def stop(self) -> None:
        self._stop = True

    def run(self, initial: str) -> None:
        if initial not in self._states:
            raise KeyError(f"No state registered with name '{initial}'")

        self._current = self._states[initial]
        log(f"▶ start  →  {self._current.name}")
        self._current.on_enter(self.ctx)

        try:
            while not self._stop:
                next_name = self._current.update(self.ctx)
                if next_name is not None:
                    if next_name not in self._states:
                        log(f"Unknown next state '{next_name}'. Stopping.")
                        break
                    self._transition(next_name)
                time.sleep(self._tick)
        finally:
            if self._current is not None:
                self._current.on_exit(self.ctx)
            log("■ state machine stopped")

    def _transition(self, new_name: str) -> None:
        log(f"  {self._current.name}  →  {new_name}")
        self._current.on_exit(self.ctx)
        self._current = self._states[new_name]
        self._current.on_enter(self.ctx)


# ══════════════════════════════════════════════════════════════════════════════
#  Outer-loop states — session lifecycle
# ══════════════════════════════════════════════════════════════════════════════

class Idle(State):
    """Reset everything and wait before starting a new session."""

    name = "Idle"

    def on_enter(self, ctx: Context) -> None:
        ctx.posture.stand()
        ctx.leds.off()
        ctx.tablet.hide()
        ctx.blackboard.clear()
        # Fresh game instance per session so hint/attempt counters reset
        ctx.blackboard["game"] = BridgeGame()
        self._settled_at = time.time() + 1.0

    def update(self, ctx: Context) -> Optional[str]:
        if time.time() >= self._settled_at:
            return "WaitForPerson"
        return None


class WaitForPerson(State):
    """Poll the camera until someone is detected, or time out."""

    name = "WaitForPerson"

    TIMEOUT_SEC = 120.0

    def on_enter(self, ctx: Context) -> None:
        log("Waiting for a person to appear…")
        ctx.leds.thinking()
        self._deadline = time.time() + self.TIMEOUT_SEC

    def update(self, ctx: Context) -> Optional[str]:
        if time.time() > self._deadline:
            log("Timeout — nobody showed up.")
            return "Idle"

        frame = ctx.camera.get_frame()
        if frame is None:
            return None

        if ctx.detector.detect(frame):
            log("Person detected!")
            return "Intro"
        return None


class Intro(State):
    """Greet the user and explain the game."""

    name = "Intro"

    GREETINGS = [
        "Hi there! I'm Pepper.",
        "Hello! Great to see you.",
        "Hey! Welcome.",
    ]

    def on_enter(self, ctx: Context) -> None:
        ctx.leds.happy()
        ctx.anim.run_async("animations/Stand/Gestures/Hey_1")
        ctx.tts.speak(random.choice(self.GREETINGS), animated=True)
        ctx.tts.speak(
            "Today we're going to play a bridge-building game. "
            "I'll give you a problem to solve. "
            "Ask me for a hint whenever you get stuck, "
            "or say 'done' when you think you've solved it.",
            animated=True,
        )
        self._done_at = time.time() + 0.5

    def update(self, ctx: Context) -> Optional[str]:
        if time.time() >= self._done_at:
            return "PresentProblem"
        return None


class Farewell(State):
    """Closing greeting, then loop back to Idle."""

    name = "Farewell"

    def on_enter(self, ctx: Context) -> None:
        ctx.leds.happy()
        ctx.anim.run_async("animations/Stand/Gestures/BowShort_1")
        ctx.tts.speak(
            "Thanks for playing! Come back any time.",
            animated=True,
        )
        self._done_at = time.time() + 2.0

    def update(self, ctx: Context) -> Optional[str]:
        if time.time() >= self._done_at:
            return "Idle"
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  Inner-loop states — the actual game
# ══════════════════════════════════════════════════════════════════════════════

class PresentProblem(State):
    """Pull the next problem from BridgeGame and narrate it."""

    name = "PresentProblem"

    def on_enter(self, ctx: Context) -> None:
        game: BridgeGame = ctx.blackboard["game"]
        description = game.start_new_problem()
        ctx.leds.thinking()
        ctx.tts.speak(description, animated=True)
        ctx.tts.speak(
            "Take a moment to think. Say 'hint' if you need help, "
            "or 'done' when you're ready to give me your answer.",
            animated=True,
        )
        self._done_at = time.time() + 0.5

    def update(self, ctx: Context) -> Optional[str]:
        if time.time() >= self._done_at:
            return "WaitForUserAction"
        return None


class WaitForUserAction(State):
    """
    The hub of the game loop. Listens for what the user wants next
    and branches accordingly.
    """

    name = "WaitForUserAction"

    HINT_KEYWORDS = ("hint", "help", "clue", "stuck", "don't know", "dont know")
    DONE_KEYWORDS = ("done", "finished", "ready", "check", "answer", "solved")
    MAX_SILENCE_BEFORE_NUDGE = 2   # unanswered listens before a proactive hint

    def on_enter(self, ctx: Context) -> None:
        ctx.leds.thinking()
        self._silence_count = 0

    def update(self, ctx: Context) -> Optional[str]:
        heard = ctx.stt.listen()

        # --- Case 1: silence ----------------------------------------------
        if not heard:
            self._silence_count += 1
            log(f"(silence {self._silence_count}/{self.MAX_SILENCE_BEFORE_NUDGE})")
            if self._silence_count >= self.MAX_SILENCE_BEFORE_NUDGE:
                ctx.tts.speak(
                    "You've been quiet for a while — let me give you a hint.",
                    animated=True,
                )
                return "GiveHint"
            return None

        # --- Case 2: got something ----------------------------------------
        self._silence_count = 0
        ctx.blackboard["last_utterance"] = heard
        log(f"User said: {heard!r}")
        heard_lower = heard.lower()

        if any(kw in heard_lower for kw in self.HINT_KEYWORDS):
            return "GiveHint"
        if any(kw in heard_lower for kw in self.DONE_KEYWORDS):
            return "CheckSolution"

        # --- Case 3: didn't understand ------------------------------------
        ctx.tts.speak(
            "I didn't quite follow. Say 'hint' if you need help, "
            "or 'done' when you have an answer.",
            animated=True,
        )
        return None


class GiveHint(State):
    """Deliver the next hint from BridgeGame, then return to waiting."""

    name = "GiveHint"

    def on_enter(self, ctx: Context) -> None:
        game: BridgeGame = ctx.blackboard["game"]
        hint = game.get_next_hint()
        ctx.leds.happy()
        ctx.tts.speak(f"Here's a hint. {hint}", animated=True)
        if game.hints_exhausted():
            ctx.tts.speak(
                "That was my last hint — see what you can figure out!",
                animated=True,
            )
        self._done_at = time.time() + 0.5

    def update(self, ctx: Context) -> Optional[str]:
        if time.time() >= self._done_at:
            return "WaitForUserAction"
        return None


class CheckSolution(State):
    """Ask the user to describe their solution, then check it."""

    name = "CheckSolution"

    def on_enter(self, ctx: Context) -> None:
        ctx.leds.thinking()
        ctx.tts.speak(
            "Great! Tell me about your solution. How did you build it?",
            animated=True,
        )

    def update(self, ctx: Context) -> Optional[str]:
        answer = ctx.stt.listen()

        if not answer:
            ctx.tts.speak(
                "I didn't hear your answer. Let's keep going — "
                "say 'done' again when you're ready.",
                animated=True,
            )
            return "WaitForUserAction"

        game: BridgeGame = ctx.blackboard["game"]
        result = game.check_solution(answer)
        ctx.blackboard["last_answer"] = answer
        ctx.blackboard["last_result"] = result
        log(f"Solution check: {result!r}  (answer: {answer!r})")

        if result == "correct":
            return "Celebrate"
        if result == "partial":
            ctx.tts.speak(
                "You're on the right track, but not quite there yet. "
                "Keep going — ask for a hint if you need one.",
                animated=True,
            )
        else:
            ctx.tts.speak(
                "That's not quite it. Want me to give you a hint?",
                animated=True,
            )
        return "WaitForUserAction"


class Celebrate(State):
    """Correct solution — celebrate, then end (could also chain more problems)."""

    name = "Celebrate"

    def on_enter(self, ctx: Context) -> None:
        ctx.leds.happy()
        ctx.anim.run_async("animations/Stand/Emotions/Positive/Happy_1")
        game: BridgeGame = ctx.blackboard["game"]
        ctx.tts.speak(
            f"Excellent! You solved it in {game.attempts} attempt(s). Great work!",
            animated=True,
        )
        self._done_at = time.time() + 2.0

    def update(self, ctx: Context) -> Optional[str]:
        if time.time() >= self._done_at:
            # TODO: if you want to chain problems, branch here based on
            # ctx.blackboard["game"].has_more_problems() and route back
            # to PresentProblem instead of Farewell.
            return "Farewell"
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  Dry-run fake drivers
# ══════════════════════════════════════════════════════════════════════════════

class _FakeTTS:
    def speak(self, text, animated=True): log(f"[TTS] {text}")
    def set_volume(self, v): pass
    def set_speed(self, v):  pass

class _FakeSTT:
    """Plays a scripted conversation so dry-run exercises the whole game loop."""

    _SCRIPT = [
        "hint",                                   # → GiveHint (1st)
        "hint",                                   # → GiveHint (2nd)
        "done",                                   # → CheckSolution
        "I built an arch with a keystone on top", # → correct → Celebrate
    ]

    def __init__(self):
        self._i = 0

    def register_and_subscribe(self): pass

    def listen(self):
        time.sleep(0.3)
        if self._i < len(self._SCRIPT):
            reply = self._SCRIPT[self._i]
            self._i += 1
            return reply
        return ""   # silence after script exhausted

    def unsubscribe(self): pass

class _FakeCamera:
    def start(self): log("[CAMERA] started")
    def stop(self):  log("[CAMERA] stopped")
    def get_frame(self):
        import numpy as np
        return np.zeros((240, 320, 3), dtype="uint8")

class _FakeDetector:
    def detect(self, frame): return [{"bbox": [0, 0, 1, 1], "confidence": 0.9}]
    def is_someone_present(self, frame): return True

class _FakeTablet:
    def show_webview(self, url): log(f"[TABLET] show  {url}")
    def show_image(self, url):   log(f"[TABLET] image {url}")
    def hide(self):              log("[TABLET] hide")

class _FakeAnim:
    def run_async(self, path): log(f"[ANIM] {path}")

class _FakePosture:
    def stand(self, speed=None): pass
    def stand_init(self): pass

class _FakeLEDs:
    def happy(self):    log("[LED] happy")
    def thinking(self): log("[LED] thinking")
    def sad(self):      log("[LED] sad")
    def error(self):    log("[LED] error")
    def off(self):      log("[LED] off")


# ══════════════════════════════════════════════════════════════════════════════
#  Context builders
# ══════════════════════════════════════════════════════════════════════════════

def build_fake_context() -> Context:
    ctx = Context(
        tts      = _FakeTTS(),
        stt      = _FakeSTT(),
        camera   = _FakeCamera(),
        detector = _FakeDetector(),
        tablet   = _FakeTablet(),
        anim     = _FakeAnim(),
        posture  = _FakePosture(),
        leds     = _FakeLEDs(),
    )
    ctx.camera.start()
    return ctx


def build_real_context(url: str) -> Context:
    session = PepperSession.connect(url)
    PepperSession.disable_autonomous_life()

    ctx = Context(
        tts      = TextToSpeech(session),
        stt      = SpeechToText(session),
        camera   = PepperCamera(session),
        detector = HumanDetector(),
        tablet   = TabletService(session),
        anim     = AnimationPlayer(session),
        posture  = RobotPosture(session),
        leds     = RobotLEDs(session),
    )

    ctx.camera.start()
    ctx.stt.register_and_subscribe()

    # Tablet JS → service('TabletInput').notify(json) → ctx.events
    class _TabletInputSvc:
        def notify(_self, json_str):
            try:
                ctx.events.put(_json.loads(str(json_str)))
            except Exception as e:
                log(f"[TabletInput] bad payload: {e}")

    session.registerService("TabletInput", _TabletInputSvc())

    def _on_touch(x, y):
        ctx.events.put({"action": "touch", "x": x, "y": y})
    try:
        ctx.tablet.set_on_touch_callback(_on_touch)
    except Exception:
        pass

    return ctx


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Pepper bridge-game state machine")
    parser.add_argument("--url", default="tcp://172.18.48.50:9559",
                        help="Naoqi URL, e.g. tcp://ROBOT_IP:9559")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run without a real robot (fake drivers)")
    parser.add_argument("--tick-hz", type=float, default=10.0,
                        help="State-machine tick rate (default: 10 Hz)")
    args = parser.parse_args()

    if args.dry_run:
        log("=== DRY-RUN MODE (no robot) ===")
        ctx = build_fake_context()
    else:
        log(f"Connecting to {args.url} …")
        ctx = build_real_context(args.url)

    sm = StateMachine(ctx, tick_hz=args.tick_hz)

    # Outer lifecycle
    sm.register(Idle())
    sm.register(WaitForPerson())
    sm.register(Intro())
    sm.register(Farewell())

    # Game loop
    sm.register(PresentProblem())
    sm.register(WaitForUserAction())
    sm.register(GiveHint())
    sm.register(CheckSolution())
    sm.register(Celebrate())

    try:
        sm.run(initial="Idle")
    except KeyboardInterrupt:
        log("Interrupted by user.")
        sm.stop()
    finally:
        if not args.dry_run:
            log("Cleaning up …")
            for fn, label in [
                (ctx.stt.unsubscribe,                    "STT unsubscribe"),
                (ctx.camera.stop,                        "camera stop"),
                (ctx.leds.off,                           "LEDs off"),
                (ctx.tablet.hide,                        "tablet hide"),
                (lambda: ctx.posture.stand(speed=0.5),   "posture stand"),
                (PepperSession.disconnect,               "session disconnect"),
            ]:
                try:
                    fn()
                except Exception as exc:
                    log(f"  [{label}] {exc}")
        log("Done.")


if __name__ == "__main__":
    main()
