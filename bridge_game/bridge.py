#!/usr/bin/env python
# =============================================================================
#  HRI_lab_Pepper — Menu Demo Scenario
# =============================================================================
"""
Multi-modal demo that chains:
  1. Person detection — Pepper waits until someone stands in front of it.
  2. Greeting        — Pepper greets the person with speech + animation.
  3. Information     — Pepper asks for confirmation ("ready?") and checks STT.
  4. Tablet menu     — Pepper shows a 4-option image-card menu on the tablet.
  5. Reaction        — Pepper reacts differently to each card selection:
       • Weather → speaks a weather forecast and shows info page
       • Joke    → tells a joke with an enthusiastic animation
       • News    → reads a mock headline
       • Dance   → runs a dance animation

Usage
-----
    # Start the dashboard first in another terminal:
    python -m HRI_lab_Pepper.dashboard --url tcp://ROBOT_IP:9559

    # Then run this script:
    python demos/menu_demo.py --url tcp://ROBOT_IP:9559 [--port 8080]

    # Or run without a live robot (dry-run mode):
    python demos/menu_demo.py --dry-run
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

GREETINGS = [
    #"Hi there! I'm Pepper, your personal assistant robot. So glad to see you!",
    #"Hello! I spotted you — I'm Pepper. Let me help you today!",
    #"Great, a visitor! I am Pepper. Welcome!",
    "Oh boy, I sure love playing with these blocks!"
]

READY_QUESTION = (
    "Do you want to play this with me jesper senpai?"
    "Say yes if so!"
)

CONFIRM_KEYWORDS = ("yes", "yeah", "yep", "sure", "ready", "ok", "okay", "go")

DENY_KEYWORDS = ("no", "nope")

FINISHED_KEYWORDS = ("finish", "finished", "done")

GAME_INTRO = (
    "Perfect! This game is about making a bridge to connect the princess and the knight. "
    "Unfortunately, my arms are only for waving, so you would have to move the blocks. "
    "Which level do you want to play? "
    "Starter, junior, expert, or master. Say which one you want! 1, 2, 3, 4, or the color works too!"
)

GAME_INTRO_ALT = (
    "Perfect! This game is about making a bridge to connect the princess and the knight. "
    "Unfortunately, my arms are only for waving, so you would have to move the blocks. "
    "Are you ready? "
)

ON_DENY = (
    "Alright, let me know if you change your mind."
)

STARTER_KEYWORDS = ("1", "one", "green","starter")
JUNIOR_KEYWORDS = ("2", "two", "yellow", "junior")
EXPERT_KEYWORDS = ("3", "three", "red", "expert")
MASTER_KEYWORDS = ("4", "four", "purple", "master")


LEVEL_SHOWCASE = (
    "Look at my tablet to see how to set up the level. Let me know when you are done by saying 'done'."
)

REACTIONS = {
    "Weather": {
        "speech": (
            "Great choice! Here is today's weather forecast: "
            "It's a lovely sunny day with temperatures around 20 degrees. "
            "Perfect for a walk outside!"
        ),
        "animation": "animations/Stand/Gestures/ShowSky_2",
        "tablet": ("info.html", "title=Weather+Forecast&result=Sunny+20°C — perfect+for+a+walk!"),
        "led": "happy",
    },
    "Joke": {
        "speech": (
            "Oh, you want a joke! Here you go. "
            "Why don't scientists trust atoms? "
            "Because they make up everything! "
            "Ha! I hope that made you smile!"
        ),
        "animation": "animations/Stand/Emotions/Positive/Hysterical_1",
        "tablet": ("info.html", "title=Joke+Time!&result=Why+don't+scientists+trust+atoms?+%0ABecause+they+make+up+everything!"),
        "led": "happy",
    },
    "News": {
        "speech": (
            "Here is today's top headline. "
            "Researchers develop new robot that can understand human emotions. "
            "Experts say this could revolutionise human-robot interaction. "
            "Sounds like the future is bright for robots like me!"
        ),
        "animation": "animations/Stand/Gestures/Explain_3",
        "tablet": ("info.html", "title=Top+Headline&result=Researchers+develop+robot+that+understands+human+emotions."),
        "led": "thinking",
    },
    "Dance": {
        "speech": (
            "Dance? Oh I love this one! "
            "Watch my moves!"
        ),
        "animation": "animations/Stand/BodyTalk/BodyTalk_5",
        "tablet": ("info.html", "title=Dance+Time!&result=Watch+Pepper+dance! 🕺"),
        "led": "happy",
    },
}


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
    Listen for keywords associated with a specific level.
    Returns the level number of the matched keyword, and 0 if keyword doesnt match.
    """
    _log(f"Listening for confirmation (keywords: {keywords_1, keywords_2, keywords_3, keywords_4}) …")
    transcript = stt.listen()
    if not transcript:
        _log("No speech heard.")
        return 0
    _log(f"Heard: '{transcript}'")


    response = transcript

    # process response …
    if any(kw in transcript.lower() for kw in keywords_1):
        return 1
        
    elif any(kw in transcript.lower() for kw in keywords_2):
        return 2

    elif any(kw in transcript.lower() for kw in keywords_3):
        return 3

    elif any(kw in transcript.lower() for kw in keywords_4):
        return 4
    else:
        return 0



def _wait_for_menu_choice(timeout: float = 30.0) -> dict:
    """
    Block until the broker receives a card_choice POST from the tablet,
    or *timeout* seconds elapse.  Returns the payload dict, or {} on timeout.
    """
    _log("Waiting for menu selection on tablet…")
    try:
        data = _choice_queue.get(timeout=timeout)
        _log(f"Menu choice received: {data.get('value', '?')}")
        return data
    except queue.Empty:
        _log("Menu selection timeout.")
        return {}


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

def present_problem(tts, stt, problem):
    tts.speak(problem["description"], animated=True)
    tts.speak("Say 'hint' if you need help, or 'done' when you are finished.")

def check_solution(tts, stt, game, problem):
    tts.speak("Tell me about your solution.")
    stt.register_and_subscribe()
    answer = stt.listen()
    stt.unsubscribe()

    if not answer:
        tts.speak("I didn't hear that.")
        return False

    if game.check_solution(answer, problem):
        return True

    tts.speak("Not quite — keep going.")
    return False

def game_round(tts, stt, leds, game, problem):
    """One full round of the bridge game. Returns True if solved."""
    present_problem(tts, stt, problem)
    hint_index = 0

    while True:
        stt.register_and_subscribe()
        heard = stt.listen()
        stt.unsubscribe()

        if not heard:
            tts.speak("You've gone quiet — let me give you a hint.")
            give_hint(tts, problem, hint_index)
            hint_index += 1
            continue

        text = heard.lower()

        if any(kw in text for kw in ("hint", "help", "stuck")):
            give_hint(tts, problem, hint_index)
            hint_index += 1

        elif any(kw in text for kw in FINISHED_KEYWORDS):
            if check_solution(tts, stt, game, problem):
                celebrate(tts, leds)
                return True
            # wrong answer — fall through, the while loop continues

        else:
            tts.speak("Say 'hint' for help, or 'done' when ready.")

def give_hint(tts, problem, index):
    hints = problem["hints"]
    if index < len(hints):
        tts.speak(f"Here's a hint. {hints[index]}")
    else:
        tts.speak("That's all the hints I have!")

def celebrate(tts, leds):
    leds.happy()
    tts.speak("Congratulations! You solved the problem!", animated=True)


class BridgeGame:
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
    tts.set_volume(75)
    tts.set_speed(100)

    # ── 1. Wait for a person ────────────────────────────────────────────
    found = _wait_for_person(camera, detector, timeout=120.0)
    if not found:
        _log("Nobody showed up. Ending demo.")
        return

    # Blink LEDs to signal detection
    _led(leds, "happy")

    # ── 2. Greet ────────────────────────────────────────────────────────
    import random
    greeting = random.choice(GREETINGS)
    _log(f"Greeting: {greeting}")
    anim.run_async("animations/Stand/Gestures/Hey_1")
    tts.speak(greeting, animated=True)
    time.sleep(0.5)

    # ── 3. Ask for confirmation via STT ─────────────────────────────────
    # Show listening page on tablet
    tablet.show_webview(_build_tablet_url(dashboard_url, "listening.html", "prompt=Listening...", on_robot))
    _led(leds, "thinking")

    tts.speak(READY_QUESTION, animated=True)

    stt.register_and_subscribe()
    confirmed = _wait_for_confirmation(stt)
    stt.unsubscribe()

    if not confirmed:
        tts.speak(
            "I didn't quite catch that. Let us play anyway!", animated=True
        )

    # ── 4. Choose level ────────────────────────────────────────────────
    _led(leds, "happy")
    tts.speak(GAME_INTRO, animated=True) #Hade kunnat göra denna till en class likt bridgeGame och lägga in tablet logiken

    tablet.show_webview(_build_tablet_url(dashboard_url, "levels.html", "", on_robot))

    stt.register_and_subscribe()
    level = _wait_for_level_select(stt)
    stt.unsubscribe()

    if level == 0:
        tts.speak(
            "I didn't quite catch that - Let's go with starter", animated=True
        )
        level = 1

    # ── 5. Play the game ────────────────────────────────────────────────
    game = BridgeGame()
    problem = game.get_problem(level)
    _led(leds, "happy")
    tts.speak(LEVEL_SHOWCASE, animated=True)

    game_round(tts, stt, leds, game, problem)



#     ### MENU START ###
#     choice_event = _wait_for_menu_choice(timeout=60.0)

#     if not choice_event:
#         tts.speak(
#             "Hmm, it seems you haven't chosen anything. "
#             "That's okay, I'll be here when you're ready!",
#             animated=True,
#         )
#         tablet.hide()
#         _led(leds, "off")
#         return

#     topic = choice_event.get("value", "")
#     reaction = REACTIONS.get(topic)

#     if not reaction:
#         tts.speak(f"Interesting choice: {topic}! I'm not sure how to respond to that one.", animated=True)
#         tablet.hide()
#         return

#     _log(f"Reacting to: {topic}")

#     # Show reaction on tablet first (non-blocking)
#     tab_page, tab_params = reaction["tablet"]
#     tablet.show_webview(_build_tablet_url(dashboard_url, tab_page, tab_params, on_robot))

#     # Set LEDs
#     _led(leds, reaction["led"])

#     # Play animation and speak simultaneously
#     anim.run_async(reaction["animation"])
#     tts.speak(reaction["speech"], animated=True)

#     time.sleep(1.5)
        
    
#    ### MENU END ###

    # ── 6. Closing ────────────────────────────────────────────────────────
    anim.run_async("animations/Stand/Gestures/BodyTalk_5")
    tts.speak(
        "Hooray, we did it!",
        animated=True,
    )
    time.sleep(2.0)    

    tts.speak(
        "Thank you for playing. Come back anytime.",
        animated=True,
    )
    anim.run_async("animations/Stand/Gestures/BowShort_1")
    
    time.sleep(2.0)

    tablet.hide()
    _led(leds, "off")
    _log("Demo complete.")


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
        PepperSession.disable_autonomous_life()
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
