#!/usr/bin/env python
# =============================================================================
#  HRI_lab_Pepper — Bridge Scenario
# =============================================================================
"""
Multi-modal demo that chains:
  0. Person detection — Pepper waits until someone stands in front of it.
  1. Greeting         — Pepper introduces herself and asks if the user wants to play.
  2. Rules            — Pepper explains the puzzle and listens for 'ready' / 'explain'.
  3. Level selection  — Pepper asks which level to play, checks STT for keywords.
  4. Board setup      — User mirrors the starting position; Pepper waits for 'done'.
  5. Solving          — User builds the path; hints are offered on request or silence.
  6. Solution check   — Pepper reveals her solution and asks if it matches the user's.
  7. Celebrate & end  — Same → Phase 7A; Different → Phase 7B; both end the session.

Usage
-----
    # Start the dashboard first in another terminal:
    python -m HRI_lab_Pepper.dashboard --url tcp://ROBOT_IP:9559

    # Then run this script:
    python bridge_game/bridge.py --url tcp://ROBOT_IP:9559 [--port 8080]

    # Or run without a live robot (dry-run mode):s
    python bridge_game/bridge.py --dry-run
"""

import argparse
import queue
import time
import sys
import threading
import json as _json
from pathlib import Path

# ── Robot drivers ──────────────────────────────────────────────────────────────
try:
    from HRI_lab_Pepper.session import PepperSession
    from HRI_lab_Pepper.speech.tts import TextToSpeech
    from HRI_lab_Pepper.speech.stt import SpeechToText
    from HRI_lab_Pepper.vision.camera import PepperCamera
    from HRI_lab_Pepper.vision.human_detection import HumanDetector
    from HRI_lab_Pepper.tablet import TabletService, deploy_tablet_pages, TABLET_ROBOT_BASE as _TABLET_ROBOT_BASE
    from HRI_lab_Pepper.interaction.awareness import BasicAwareness
    from HRI_lab_Pepper.motion.posture import RobotPosture
    from HRI_lab_Pepper.motion.leds import RobotLEDs
    from HRI_lab_Pepper.motion.animation_player import AnimationPlayer
except ImportError as e:
    print(f"[DEMO] Import error: {e}")
    print("[DEMO] Is the package installed? Run: pip install -e .")
    sys.exit(1)

_TABLET_SRC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static" / "tablet"


# ──────────────────────────────────────────────────────────────────────────────
#  Scenario content
# ──────────────────────────────────────────────────────────────────────────────

# ── Phase 1 — Greeting ────────────────────────────────────────────────────────
GREETINGS = (
    "Hello there! My name is Pepper. I've been working on this logic puzzle called "
    "Camelot Junior. A knight and a princess are stuck in a castle, and I need help "
    "building them a path to meet."
)

READY_QUESTION = (
    "Would you like to play with me? Just say yes if you'd like to!"
)

ON_DENY = (
    "No problem! I'll be here if you change your mind."
)

# ── Phase 2 — Rules ───────────────────────────────────────────────────────────
RULES_INTRO   = "Great! Here's how it works."
RULES_BODY_1  = (
    "The knight and the princess are standing on towers on opposite sides of the board. "
    "- Your job is to use the spare blocks to build a path so they can walk to each other."
)
RULES_BODY_2  = (
    "They can walk along flat surfaces and up or down stairs — but they can't jump or "
    "climb straight walls. Take a look at my tablet while I explain."
)
RULES_PROMPT  = (
    "- Got it? Say 'ready' when you want to start, or 'explain' if you'd like to hear that again."
)
RULES_FALLBACK = (
    "If you're unsure, let's just try the starter level and you'll pick it up as we go!"
)

# ── Phase 3 — Level Selection ─────────────────────────────────────────────────
LEVEL_INTRO = (
    "Great! - There are four difficulty levels — Starter, Junior, Expert, and Master. "
    "If this is your first time, I'd recommend Starter. Which level would you like?"
)
LEVEL_FALLBACK = "I didn't catch that — I'll start us on Starter."

# ── Phase 4 — Board Setup ─────────────────────────────────────────────────────
SETUP_INTRO = (
    "Before we start building, we need to set up the starting position. Take a look at "
    "my tablet — place the towers and figurines exactly like the image."
)
SETUP_PROMPT = "Let me know when it matches the picture by saying 'finished'."
SETUP_NUDGE  = (
    "Take your time! I'm showing the starting position on my tablet. Say 'finished' when the "
    "towers match the picture."
)
SETUP_OK     = "Perfect! The stage is set."

# ── Phase 5 — Solving ─────────────────────────────────────────────────────────
GAME_INTRO = (
    "Now use the remaining blocks to build a path for the knight and the princess. "
    "Remember — they can walk on flat surfaces and stairs, but they can't jump."
)
GAME_PROMPT = (
    "If you get stuck, just say 'hint' and I'll give you a clue. Say 'finished' when "
    "you think they can reach each other."
)
GAME_SILENT_HINT = "You've been quiet for a while — let me give you a small clue."
GAME_HINTS_DONE  = "That's all the hints I've got — you're almost there, I can feel it!"

# ── Phase 6 — Solution Check ──────────────────────────────────────────────────
SOLUTION_REVEAL  = "Exciting! Let me show you the solution I had in mind."
SOLUTION_QUERY   = "Does your path look the same as mine?"
SOLUTION_REPROMPT = "Does your bridge look like the one on my tablet?"

# ── Phase 7A / 7B — Celebrate ─────────────────────────────────────────────────
CELEBRATE_SAME_1   = "Wohoo! That's awesome! — the knight and princess can finally meet."
CELEBRATE_UNIQUE = (
    "Oh, interesting — your path looks different from mine, but if the knight and "
    "princess can reach each other, that counts!"
)

# ── End Session ───────────────────────────────────────────────────────────────
END_SESSION = "Come back anytime — I've got 48 puzzles to work through and I could use the help."

# ── Keyword master list ───────────────────────────────────────────────────────
CONFIRM_KEYWORDS    = ("yes", "yeah", "yep", "sure", "okay", "ok", "go")
DENY_KEYWORDS       = ("no", "nope", "not really", "no thanks")
READY_KEYWORDS      = ("ready", "yes", "yeah", "start", "go", "let's go")
REPEAT_KEYWORDS     = ("again", "explain", "repeat", "what")
STARTER_KEYWORDS    = ("starter", "1", "one", "green", "easy")
JUNIOR_KEYWORDS     = ("junior",  "2", "two", "yellow", "jr")
EXPERT_KEYWORDS     = ("expert",  "3", "three", "red")
MASTER_KEYWORDS     = ("master",  "4", "four", "purple", "hard")
SETUP_DONE_KEYWORDS = ("done", "ready", "finished", "set")
HELP_KEYWORDS       = ("help", "again", "repeat", "what", "confused")
HINT_KEYWORDS       = ("hint", "help", "stuck", "clue")
FINISHED_KEYWORDS   = ("finished", "done", "finish")
MATCH_KEYWORDS      = ("yes", "yeah", "same", "matches", "identical", "yep")
DIFF_KEYWORDS       = ("no", "different", "nope", "not the same", "not quite")

# ──────────────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[DEMO] {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
#  Tablet input queue
# ──────────────────────────────────────────────────────────────────────────────

# Single-item queue: ALMemory subscriber puts here; _wait_for_menu_choice reads.
_choice_queue: queue.Queue = queue.Queue()


def _wait_for_person(
    camera: "PepperCamera",
    detector: "HumanDetector",
    timeout: float = 120.0,
    check_interval: float = 0.5,
) -> bool:
    """
    Block until at least one person is detected in the camera frame,
    or *timeout* seconds elapse.

    Returns True if a person was found, False on timeout.
    """
    _log("Waiting for a person to appear…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = camera.get_frame()
        if frame is not None:
            detections = detector.detect(frame)
            if detections:
                _log(f"Person detected! ({len(detections)} detection(s))")
                return True
        time.sleep(check_interval)
    _log("Timeout: no person detected.")
    return False


def _wait_for_confirmation(
    stt: "SpeechToText",
    keywords: tuple = CONFIRM_KEYWORDS,
) -> bool:
    """
    Listen for a yes-like utterance.
    Returns True if a matching keyword was heard.
    """
    _log(f"Listening for confirmation (keywords: {keywords}) …")
    transcript = stt.listen()
    if not transcript:
        _log("No speech heard.")
        return False
    _log(f"Heard: '{transcript}'")
    return any(kw in transcript.lower() for kw in keywords)


def _wait_for_level_select(
    stt: "SpeechToText",
    keywords_1: tuple = STARTER_KEYWORDS,
    keywords_2: tuple = JUNIOR_KEYWORDS,
    keywords_3: tuple = EXPERT_KEYWORDS,
    keywords_4: tuple = MASTER_KEYWORDS,
) -> int:
    """
    Listen for keywords associated with a specific level or input on touchscreen.
    Returns the level number of the matched keyword, and 0 if keyword doesnt match.
    returns -1 if no speech is heard in time.
    """
    _log(f"Listening for confirmation (keywords: {keywords_1, keywords_2, keywords_3, keywords_4}) or touchscreen input…")
    # Check for touchscreen input during intro
    try:
        t_input = _choice_queue.get_nowait()
        _log(f"Tablet input received before listening: {t_input}")
        return int(t_input.get("index")) + 1
    except (queue.Empty, ValueError, TypeError):
        pass

    # Check voice input 
    transcript = stt.listen()
    
    # Check tablet input during session
    if not _choice_queue.empty():
        try:
            t_input = _choice_queue.get_nowait()
            _log(f"Tablet input received during/after listening: {t_input}")
            return int(t_input.get("index")) + 1
        except (queue.Empty, ValueError, TypeError):
            pass

    if not transcript:
        _log("No speech heard.")
        return -1

    # Process voice transcript
    _log(f"Heard: '{transcript}'")
    text = transcript.lower()
    if any(kw in text for kw in keywords_1): return 1
    if any(kw in text for kw in keywords_2): return 2
    if any(kw in text for kw in keywords_3): return 3
    if any(kw in text for kw in keywords_4): return 4
    
    return 0
    
def _classify_response(stt: "SpeechToText", *categories):
    """
    Listen once and classify the transcript into one of several keyword categories.
    *categories* is a sequence of (label, keywords) tuples — checked in order.
    Returns the label of the first matching category, or None on silence / no match.
    """
    transcript = stt.listen()
    if not transcript:
        _log("No speech heard.")
        return None
    _log(f"Heard: '{transcript}'")
    text = transcript.lower()
    for label, keywords in categories:
        if any(kw in text for kw in keywords):
            return label
    return None


def _explain_rules(tts: "TextToSpeech", stt: "SpeechToText") -> None:
    """
    Phase 2 — Rules Explanation.
    Speaks the rules, listens for ready/repeat, loops at most once on 'repeat'.
    Falls through to Phase 3 on silence or after a single repeat.
    """
    for attempt in range(2):
        tts.speak(RULES_INTRO, animated=True)
        time.sleep(0.5)
        tts.speak(RULES_BODY_1, animated=True)
        time.sleep(0.5)
        tts.speak(RULES_BODY_2, animated=True)
        tts.speak(RULES_PROMPT, animated=True)

        stt.register_and_subscribe()
        response = _classify_response(
            stt,
            ("ready", READY_KEYWORDS),
            ("repeat", REPEAT_KEYWORDS),
        )
        stt.unsubscribe()

        if response == "ready":
            return
        if response == "repeat":
            if attempt == 0:
                continue  # repeat the rules once
            tts.speak(RULES_FALLBACK, animated=True)
            return

        # silence / not understood — re-prompt and listen once more
        tts.speak("Just say 'ready' when you want to start.", animated=True)
        stt.register_and_subscribe()
        response = _classify_response(
            stt,
            ("ready", READY_KEYWORDS),
            ("repeat", REPEAT_KEYWORDS),
        )
        stt.unsubscribe()
        if response == "repeat" and attempt == 0:
            continue
        # ready, silence again, or repeat-after-repeat → proceed to Phase 3
        return


def _wait_for_setup_done(tts: "TextToSpeech", stt: "SpeechToText") -> None:
    """
    Phase 4 — Board Setup.
    Asks the user to set up the towers + figurines, waits for 'done'.
    Re-explains once on help / two silences, then proceeds regardless.
    """
    tts.speak(SETUP_INTRO, animated=True)
    time.sleep(0.5)
    tts.speak(SETUP_PROMPT, animated=True)

    silent_count = 0
    re_explained = False
    while True:
        stt.register_and_subscribe()
        response = _classify_response(
            stt,
            ("done", SETUP_DONE_KEYWORDS),
            ("help", HELP_KEYWORDS),
        )
        stt.unsubscribe()

        if response == "done":
            tts.speak(SETUP_OK, animated=True)
            return

        if response == "help":
            if re_explained:
                tts.speak(SETUP_OK, animated=True)
                return
            re_explained = True
            silent_count = 0
            tts.speak(SETUP_INTRO, animated=True)
            tts.speak(SETUP_PROMPT, animated=True)
            continue

        # silence / not understood
        silent_count += 1
        if silent_count == 1:
            tts.speak(SETUP_NUDGE, animated=True)
            continue
        # 2nd silence — try once more after re-explaining, then give up
        if not re_explained:
            re_explained = True
            silent_count = 0
            tts.speak(SETUP_INTRO, animated=True)
            tts.speak(SETUP_PROMPT, animated=True)
            continue
        tts.speak(SETUP_OK, animated=True)
        return


def _led(leds: object, preset: str) -> None:
    """Dispatch a preset name string to the matching RobotLEDs method."""
    fn = getattr(leds, preset, None) or getattr(leds, "off")
    fn()


def _build_tablet_url(base_url: str, page: str, params: str, on_robot: bool = False) -> str:
    """Build a tablet URL — uses the robot-internal bridge when on_robot=True."""
    if on_robot:
        url = f"{_TABLET_ROBOT_BASE}/{page}"
    else:
        url = f"{base_url}/tablet/{page}"
    if params:
        url += f"?{params}"
    return url

def present_problem(tts, stt, dashboard_url: str,
    tablet: object, on_robot: bool = False):
    """Phase 5 intro — speak the gameplay instructions."""
    tts.speak(GAME_INTRO, animated=True)
    time.sleep(0.5)
    tts.speak(GAME_PROMPT, animated=True)


def compare_solution(tts, stt):
    """
    Phase 6 — Solution Check.
    Returns True on 'same' (→ 7A), False on 'different' or repeated silence (→ 7B).
    """
    tts.speak(SOLUTION_REVEAL, animated=True)
    time.sleep(1.0)
    tts.speak(SOLUTION_QUERY, animated=True)

    stt.register_and_subscribe()
    response = _classify_response(
        stt,
        ("match", MATCH_KEYWORDS),
        ("diff", DIFF_KEYWORDS),
    )
    stt.unsubscribe()

    if response == "match":
        return True
    if response == "diff":
        return False

    # silence / not understood — re-ask once
    tts.speak(SOLUTION_REPROMPT, animated=True)
    stt.register_and_subscribe()
    response = _classify_response(
        stt,
        ("match", MATCH_KEYWORDS),
        ("diff", DIFF_KEYWORDS),
    )
    stt.unsubscribe()

    if response == "match":
        return True
    # diff or silence again → Phase 7B
    return False


def game_round(tts, stt, leds, problem, dashboard_url: str,
    tablet: object, anim, level, on_robot):
    """
    Phase 5 — Solving game loop.
    On 'finished', runs Phase 6 then Phase 7A or 7B.
    """
    _log("trying to present problem:")
    present_problem(tts, stt, dashboard_url=dashboard_url, tablet=tablet)
    hint_index = 0
    silent_count = 0

    while True:
        stt.register_and_subscribe()
        heard = stt.listen()
        stt.unsubscribe()

        if not heard:
            silent_count += 1
            if silent_count >= 2:
                tts.speak(GAME_SILENT_HINT, animated=True)
                give_hint(tts, problem, hint_index)
                hint_index += 1
                silent_count = 0
            continue

        silent_count = 0
        text = heard.lower()

        if any(kw in text for kw in HINT_KEYWORDS):
            give_hint(tts, problem, hint_index)
            hint_index += 1

        elif any(kw in text for kw in FINISHED_KEYWORDS):

            # Phase 6 — Solution Check
            level_names = ["Starter", "Junior", "Expert", "Master"]
            level_img = [
                "https://people.cs.umu.se/~id23sem/bridgegame_img/Beginner_sol.jpg",
                "https://people.cs.umu.se/~id23sem/bridgegame_img/Medium_sol.jpg",
                "https://people.cs.umu.se/~id23sem/bridgegame_img/Hard_sol.jpg",
                "https://people.cs.umu.se/~id23sem/bridgegame_img/Master_sol.jpg"
            ]
            selected_name_sol = level_names[level-1]
            selected_img_sol = level_img[level-1]

            # Build the parameters for picked level page
            params = "label={}&img={}".format(selected_name_sol, selected_img_sol)
            tablet.show_webview(_build_tablet_url(dashboard_url, "solution.html", params, on_robot))

            same = compare_solution(tts, stt)

            # Phase 7 — Celebrate (both paths end the session)
            if same:
                celebrate_same(tts, leds, anim)
            else:
                celebrate_unique(tts, leds, anim)
            return

        else:
            tts.speak("Say 'hint' for help, or 'finished' when ready.", animated=True)


def give_hint(tts, problem, index):
    hints = problem["hints"]
    if index < len(hints):
        tts.speak(f"Here's a hint. {hints[index]}", animated=True)
    else:
        tts.speak(GAME_HINTS_DONE, animated=True)


def celebrate_same(tts, leds, anim):
    """Phase 7A — User's solution matches Pepper's."""
    leds.happy()
    tts.speak(CELEBRATE_SAME_1, animated=True)
    anim.run_async("animations/Stand/Gestures/Hey_1")
    time.sleep(0.5)


def celebrate_unique(tts, leds, anim):
    """Phase 7B — User's solution differs, but is still valid."""
    leds.happy()
    tts.speak(CELEBRATE_UNIQUE, animated=True)
    anim.run_async("animations/Stand/Gestures/Hey_1")
    time.sleep(0.5)


def end_session(tts, anim, tablet, leds):
    """End the session with a goodbye message."""
    tts.speak(END_SESSION, animated=True)
    anim.run_async("animations/Stand/Gestures/BowShort_1")

    time.sleep(2.0)

    tablet.hide()
    _led(leds, "off")
    _log("Demo complete.")
    return

class BridgeGame:
    #TODO move this to a json? perhaps
    PROBLEMS = [
        {
            "id": "starter",
            "description": "Starter level: Build a simple bridge with 2 blocks.",
            "hints": [
                "Try placing one block on the left and one on the right, leaving a gap in the middle.",
                "Make sure the blocks are close enough to each other to connect!",
            ],
            "solution_keywords": ["yes", "yeah", "yep", "sure", "ready", "ok", "okay", "go"]
        },
        {
            "id": "junior",
            "description": "Junior level: Build a bridge with 3 blocks, but one block is missing!",
            "hints": [
                "You can use the two blocks to create a sloped bridge.",
                "Try placing one block on the left and one on the right, then balance the third block on top to connect them!",
            ],
            "solution_keywords": ["yes", "yeah", "yep", "sure", "ready", "ok", "okay", "go"]
        },
        {
            "id": "expert",
            "description": "Expert level: Build a bridge with 4 blocks, but two blocks are missing!",
            "hints": [
                "You can create a zig-zag pattern to connect the pieces.",
                "Try placing two blocks on the left and two on the right, then balance the remaining blocks on top to connect them!",
            ],
            "solution_keywords": ["yes", "yeah", "yep", "sure", "ready", "ok", "okay", "go"]
        },
        {
            "id": "master",
            "description": "Master level: Build a bridge with 5 blocks, but three blocks are missing!",
            "hints": [
                "This one is tricky! You will need to create a more complex structure to connect all the pieces.",
                "Try placing three blocks on the left and two on the right, then balance the remaining blocks on top to connect them all together!",
            ],
            "solution_keywords": ["yes", "yeah", "yep", "sure", "ready", "ok", "okay", "go"]
        }
    ]
    def get_problem(self, level): return self.PROBLEMS[level-1]

    def check_solution(self, answer, problem):
        if not answer:
            return False
        text = answer.lower()
        keywords = problem["solution_keywords"]
        matches = sum(1 for kw in keywords if kw in text)
        return matches >= 1

def _run_tests():
    game = BridgeGame()

    # get_problem returns the right level
    assert game.get_problem(1)["id"] == "starter"
    assert game.get_problem(4)["id"] == "master"
    print("  get_problem      OK")

    # check_solution: keyword matching
    p1 = game.get_problem(1)
    assert game.check_solution("yes", p1)         # pass
    assert not game.check_solution("no clue", p1)            # no keywords → fail
    assert not game.check_solution(None, p1)                  # None → fail
    p3 = game.get_problem(3)
    assert game.check_solution("yep", p3)    # pass
    assert not game.check_solution("no", p3)       # fail
    print("  check_solution   OK")

    # _wait_for_confirmation keyword matching
    class _Fake:
        def __init__(self, t): self._t = t
        def listen(self): return self._t
    assert _wait_for_confirmation(_Fake("yes"))
    assert not _wait_for_confirmation(_Fake("nope"))
    assert not _wait_for_confirmation(_Fake(None))
    print("  confirmation     OK")

    # _wait_for_level_select keyword matching
    assert _wait_for_level_select(_Fake("green"))   == 1
    assert _wait_for_level_select(_Fake("junior"))  == 2
    assert _wait_for_level_select(_Fake("3"))       == 3
    assert _wait_for_level_select(_Fake("master"))  == 4
    assert _wait_for_level_select(_Fake("banana"))  == 0
    print("  level select     OK")

    print("All tests passed.")


# ──────────────────────────────────────────────────────────────────────────────
#  Dry-run mode  (no real robot)
# ──────────────────────────────────────────────────────────────────────────────

class _FakeTTS:
    def speak(self, text, animated=True): _log(f"[TTS] {text}")
    def set_volume(self, v): pass
    def set_speed(self, s): pass

class _FakeSTT:
    def register_and_subscribe(self): pass
    def listen(self): time.sleep(1); return "yes"
    def unsubscribe(self): pass

class _FakeCamera:
    def get_frame(self):
        import numpy as np
        return np.zeros((240, 320, 3), dtype="uint8")
    def start(self): _log("[CAMERA] Started")
    def stop(self): _log("[CAMERA] Stopped")

class _FakeDetector:
    def detect(self, frame): return [{"bbox": [0,0,1,1], "confidence": 0.9}]

class _FakeTablet:
    def __init__(self, dashboard_url: str):
        self._dashboard_url = dashboard_url
    def show_webview(self, url):
        _log(f"[TABLET] show: {url}")
        # Simulate a card tap 2 s after the menu page is shown
        if "menu_demo" in url:
            def _inject():
                time.sleep(2.0)
                _choice_queue.put({"action": "card_choice", "value": "Joke", "index": 1})
                _log("[FAKE] Injected card_choice: Joke")
            threading.Thread(target=_inject, daemon=True).start()
    def hide(self): _log("[TABLET] hide")

class _FakeAnim:
    def run_async(self, path): _log(f"[ANIM] {path}")

class _FakePosture:
    def stand(self, speed=None): pass
    def stand_init(self): pass

class _FakeLEDs:
    def happy(self):    _log("[LED] happy")
    def thinking(self): _log("[LED] thinking")
    def sad(self):      _log("[LED] sad")
    def error(self):    _log("[LED] error")
    def off(self):      _log("[LED] off")

class _FakeAwareness:
    def start(self): pass
    def stop(self): pass


# ──────────────────────────────────────────────────────────────────────────────
#  Main scenario
# ──────────────────────────────────────────────────────────────────────────────

def run_scenario(
    tts: object,
    stt: object,
    camera: object,
    detector: object,
    tablet: object,
    anim: object,
    posture: object,
    leds: object,
    awareness: object,
    session: object,
    dashboard_url: str,
    on_robot: bool = False,
) -> None:
    """Execute the full demo scenario."""

    # ── 0. Setup ────────────────────────────────────────────────────────
    _log("Setting up robot…")
    posture.stand()
    # awareness.start()
    camera.start()
    time.sleep(1.0)

    # setup the speech volume and speed
    tts.set_volume(50)
    tts.set_speed(100)

    # ── 1. Wait for a person ────────────────────────────────────────────
    found = _wait_for_person(camera, detector, timeout=120.0) #Fastnar här ibland och måste starta om roboten
    if not found:
        _log("Nobody showed up. Ending demo.")
        return

    #Blink LEDs to signal detection
    _led(leds, "happy")

    # ── 2. Phase 1 — Greet & ask to play ────────────────────────────────
    _log(f"Greeting: {GREETINGS}")
    anim.run_async("animations/Stand/Gestures/Hey_1")
    tts.speak(GREETINGS, animated=True)
    time.sleep(0.5)

    # Show listening page on tablet
    tablet.show_webview(_build_tablet_url(dashboard_url, "listening.html", "prompt=Listening...", on_robot))
    _led(leds, "thinking")

    tts.speak(READY_QUESTION, animated=True)

    stt.register_and_subscribe()
    response = _classify_response(
        stt,
        ("yes", CONFIRM_KEYWORDS),
        ("no", DENY_KEYWORDS),
    )
    stt.unsubscribe()

    if response == "no":
        tts.speak(ON_DENY, animated=True)
        end_session(tts, anim, tablet, leds)
        return

    if response is None:
        # Silence / not understood — re-ask once
        tts.speak("I didn't catch that — just say yes if you'd like to play!", animated=True)
        stt.register_and_subscribe()
        response = _classify_response(
            stt,
            ("yes", CONFIRM_KEYWORDS),
            ("no", DENY_KEYWORDS),
        )
        stt.unsubscribe()
        if response != "yes":
            tts.speak(ON_DENY, animated=True)
            end_session(tts, anim, tablet, leds)
            return

    # ── 3. Phase 2 — Rules Explanation ──────────────────────────────────
    _explain_rules(tts, stt)

    # ──────────────────────────────────────────────────────────────────────────────
    #  4. Phase 3 — Choose level
    # ──────────────────────────────────────────────────────────────────────────────
    _led(leds, "happy")

    tablet.show_webview(_build_tablet_url(dashboard_url, "levels.html", "", on_robot))
    tts.speak(LEVEL_INTRO, animated=True)

    stt.register_and_subscribe()
    level = 0
    while level <= 0:
        level = _wait_for_level_select(stt)
        if level <= 0:
            tts.speak("Sorry, I didn't catch that. Please say Starter, Junior, Expert, or Master.", animated=True)
    stt.unsubscribe()

    # Switch to pickedLevel.html
    level_names = ["Starter", "Junior", "Expert", "Master"]
    level_imgs = [
        "https://people.cs.umu.se/~id23sem/bridgegame_img/Beginner.jpg",
        "https://people.cs.umu.se/~id23sem/bridgegame_img/Medium.jpg",
        "https://people.cs.umu.se/~id23sem/bridgegame_img/Hard.jpg",
        "https://people.cs.umu.se/~id23sem/bridgegame_img/Master.jpg"
    ]

    selected_name = level_names[level-1]
    selected_img = level_imgs[level-1]

    tts.speak(f"Good choice! Let's set up level {selected_name}.", animated=True)

    # Build the parameters for picked level page
    params = "label={}&img={}".format(selected_name, selected_img)
    tablet.show_webview(_build_tablet_url(dashboard_url, "pickedLevel.html", params, on_robot))

    if session:
        memory = session.service("ALMemory")
       # subtract 1 because Python levels are 1,2,3,4 but JS arrays are 0,1,2,3
        memory.raiseEvent("BridgeGame/LevelSelected", level - 1)

    # ──────────────────────────────────────────────────────────────────────────────
    #  5. Phase 4 — Board Setup
    # ──────────────────────────────────────────────────────────────────────────────
    _wait_for_setup_done(tts, stt)

    # ──────────────────────────────────────────────────────────────────────────────
    #  6. Phase 5/6/7 — Solve, Check, Celebrate
    # ──────────────────────────────────────────────────────────────────────────────
    game = BridgeGame()
    problem = game.get_problem(level)
    _led(leds, "happy")

    game_round(tts, stt, leds, problem, dashboard_url=dashboard_url, tablet=tablet, anim=anim, level=level, on_robot=on_robot)

    # ──────────────────────────────────────────────────────────────────────────────
    #  7. End Session
    # ──────────────────────────────────────────────────────────────────────────────
    end_session(tts, anim, tablet, leds)


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Pepper menu demo scenario")
    parser.add_argument("--test",     action="store_true",
                        help="Run built-in logic tests and exit")
    parser.add_argument("--url",      default="tcp://172.18.48.50:9559",
                        help="Naoqi URL, e.g. tcp://ROBOT_IP:9559")
    parser.add_argument("--port",     type=int, default=8080,
                        help="Dashboard server port (default: 8080)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Run without a real robot (fake drivers)")
    args = parser.parse_args()

    if args.test:
        _run_tests()
        return

    if args.dry_run:
        _log("=== DRY-RUN MODE (no robot) ===")
        dashboard_url = f"http://localhost:{args.port}"
        on_robot  = False
        tts       = _FakeTTS()
        stt       = _FakeSTT()
        camera    = _FakeCamera()
        detector  = _FakeDetector()
        tablet    = _FakeTablet(dashboard_url)
        anim      = _FakeAnim()
        posture   = _FakePosture()
        leds      = _FakeLEDs()
        awareness = _FakeAwareness()
    else:
        _log(f"Connecting to {args.url} …")
        session   = PepperSession.connect(args.url)
        #PepperSession.disable_autonomous_life()
        tts       = TextToSpeech(session)
        stt       = SpeechToText(session)
        camera    = PepperCamera(session)
        detector  = HumanDetector()
        tablet    = TabletService(session)
        anim      = AnimationPlayer(session)
        posture   = RobotPosture(session)
        leds      = RobotLEDs(session)
        awareness = BasicAwareness(session)

        # Derive dashboard URL reachable by the robot's tablet browser.
        _robot_host = args.url.split("://")[-1].split(":")[0]
        import socket as _socket
        try:
            _s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            _s.connect((_robot_host, 9559))
            _local_ip = _s.getsockname()[0]
            _s.close()
        except Exception:
            _local_ip = "localhost"
        dashboard_url = f"http://{_local_ip}:{args.port}"
        _log(f"Dashboard URL: {dashboard_url}")

        # Register a qi service 'TabletInput' with a notify() method.
        # The tablet JS calls QiSession → service('TabletInput') → notify(json),
        # routed back through the existing SSH reverse tunnel.
        class _TabletInputSvc:
            def notify(self, json_str):
                try:
                    _choice_queue.put(_json.loads(str(json_str)))
                except Exception:
                    pass

        _tab_svc = _TabletInputSvc()
        session.registerService("TabletInput", _tab_svc)
        _log("Tablet input service ready.")

        # Deploy tablet pages directly to the robot via SSH
        on_robot = deploy_tablet_pages(
            robot_ip=_robot_host,
            src_dir=_TABLET_SRC_DIR,
        )
        if not on_robot:
            _log("Tablet pages not deployed — falling back to laptop URLs.")

    try:
        run_scenario(
            tts=tts,
            stt=stt,
            camera=camera,
            detector=detector,
            tablet=tablet,
            anim=anim,
            posture=posture,
            leds=leds,
            awareness=awareness,
            dashboard_url=dashboard_url,
            on_robot=on_robot,
            session=session,
        )
    except KeyboardInterrupt:
        _log("Interrupted by user.")
    finally:
        if not args.dry_run:
            _log("Cleaning up …")
            for fn, label in [
                (stt.unsubscribe,          "STT unsubscribe"),
                (camera.stop,              "camera stop"),
                (awareness.stop,           "awareness stop"),
                (leds.off,                 "LEDs off"),
                (tablet.hide,              "tablet hide"),
                (lambda: posture.stand(speed=0.5), "posture stand"),
                # (PepperSession.enable_autonomous_life, "autonomous life restore"),
                (PepperSession.disconnect, "session disconnect"),
            ]:
                try:
                    fn()
                except Exception as exc:
                    _log(f"  [{label}] {exc}")
            _log("Cleanup done.")


if __name__ == "__main__":
    main()
