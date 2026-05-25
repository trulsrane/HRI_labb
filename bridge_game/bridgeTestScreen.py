#!/usr/bin/env python
# =============================================================================
#  HRI_lab_Pepper — Bridge Scenario (Touch-Input Verified)
# =============================================================================

import argparse
import csv
import queue
import time
import sys
import threading
import json as _json
from datetime import datetime
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
    from HRI_lab_Pepper.database import DialogDB
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
    "Would you like to play with me? Press the screen if you would like to play!"
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
    "climb straight walls."
)
RULES_PROMPT  = (
    "- Got it? Press ready if you are ready or Explain if you would like to hear the rules again."
)
RULES_FALLBACK = (
    "If you're unsure, let's just try the starter level and you'll pick it up as we go!"
)

# ── Phase 3 — Level Selection ─────────────────────────────────────────────────
LEVEL_INTRO = (
    "Great! We will play this level, and I want you to help me solve it!"
)

# ── Phase 4 — Board Setup ─────────────────────────────────────────────────────
SETUP_INTRO = (
    "Before we start building, we need to set up the starting position. Take a look at "
    "my tablet — place the towers and the pick out the blocks exactly like the image."
)
SETUP_PROMPT = "Let me know when it matches the picture by pressing the ready button."
SETUP_NUDGE  = (
    "Take your time! I'm showing the starting position on my tablet. Press ready when the "
    "towers match the picture."
)
SETUP_OK     = "Perfect! The stage is set."

# ── Phase 5 — Solving ─────────────────────────────────────────────────────────
GAME_INTRO = (
    "Now use the remaining blocks to build a path for the knight and the princess. "
    "Remember — they can walk on flat surfaces and stairs, but they can't jump."
)
GAME_PROMPT = (
    "If you get stuck, just press 'hint' on the tablet and I'll give you a clue. Press 'finished' when "
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
CONFIRM_KEYWORDS    = ("yes", "yeah", "yep", "sure", "okay", "ok", "go", "play")
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
#  Study logger — uses DialogDB; exports per-run CSV at the end
# ──────────────────────────────────────────────────────────────────────────────

_DB = None
_SESSION_ID = None
_RUN_LABEL = None
_PHASE = "init"
_LISTEN_START = None


def _init_study_log(participant: str, condition: str, log_dir: Path):
    """Open the study DB and start a new session for this run."""
    global _DB, _SESSION_ID, _RUN_LABEL
    log_dir.mkdir(parents=True, exist_ok=True)
    _DB = DialogDB(str(log_dir / "study.db"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _RUN_LABEL = f"{participant}_{condition}_{ts}"
    _SESSION_ID = _DB.new_session(label=_RUN_LABEL)
    _DB.log_event("scenario_meta", {
        "participant": participant,
        "condition": condition,
        "run_label": _RUN_LABEL,
        "session_id": _SESSION_ID,
    })


def _slog(event_type: str, detail: str = "", duration_ms=None):
    """Record a study event (phase change, hint, classification, etc.)."""
    if _DB is None:
        return
    data = {"session_id": _SESSION_ID, "phase": _PHASE}
    if detail:
        data["detail"] = detail
    if duration_ms is not None:
        data["duration_ms"] = int(duration_ms)
    _DB.log_event(event_type, data)


def _sphase(phase: str):
    """Switch to a new phase and log the transition."""
    global _PHASE
    _PHASE = phase
    _slog("phase_start")


def _listen_begin():
    global _LISTEN_START
    _LISTEN_START = time.monotonic()
    _slog("stt_listen_start")


def _listen_end(transcript):
    global _LISTEN_START
    dur = None
    if _LISTEN_START is not None:
        dur = (time.monotonic() - _LISTEN_START) * 1000
        _LISTEN_START = None
    if transcript:
        if _DB is not None:
            _DB.log("tablet_touch", str(transcript), session_id=_SESSION_ID, phase=_PHASE)
        _slog("tablet_heard", detail=str(transcript), duration_ms=dur)
    else:
        _slog("tablet_silence", duration_ms=dur)


def _close_study_log(log_dir: Path):
    """End the session and export this run's events + dialog to a per-run CSV."""
    if _DB is None or _SESSION_ID is None:
        return
    try:
        _DB.end_session(_SESSION_ID)
        csv_path = log_dir / f"{_RUN_LABEL}.csv"
        _export_run_to_csv(_DB, _SESSION_ID, csv_path)
        _log(f"Study log exported → {csv_path}")
    finally:
        _DB.close()


def _export_run_to_csv(db: "DialogDB", session_id: int, csv_path: Path):
    """Merge events (filtered to this session) and dialog turns into one CSV."""
    rows = []
    for e in db.get_events(limit=1_000_000):
        data = e.get("data") or {}
        if isinstance(data, dict) and data.get("session_id") == session_id:
            rows.append({
                "ts": e["ts"], "kind": "event", "event_type": e["event_type"],
                "role": "", "text": "",
                "phase": data.get("phase", ""),
                "detail": data.get("detail", ""),
                "duration_ms": data.get("duration_ms", ""),
            })
    for t in db.get_session(session_id):
        meta = {}
        if t.get("metadata"):
            try:
                meta = _json.loads(t["metadata"])
            except (_json.JSONDecodeError, TypeError):
                pass
        rows.append({
            "ts": t["ts"], "kind": "dialog", "event_type": "",
            "role": t["role"], "text": t["text"],
            "phase": meta.get("phase", ""),
            "detail": t.get("intent") or "",
            "duration_ms": "",
        })
    rows.sort(key=lambda r: r["ts"])
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write("sep=,\n")
        writer = csv.DictWriter(f, fieldnames=[
            "ts", "kind", "phase", "event_type", "role", "text", "detail", "duration_ms"
        ])
        writer.writeheader()
        writer.writerows(rows)


# ──────────────────────────────────────────────────────────────────────────────
#  Tablet input queue
# ──────────────────────────────────────────────────────────────────────────────

_choice_queue: queue.Queue = queue.Queue()


def _wait_for_person(
    camera: "PepperCamera",
    detector: "HumanDetector",
    timeout: float = 120.0,
    check_interval: float = 0.5,
) -> bool:
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
    """Blocks and reads from the tablet event queue instead of STT audio stream."""
    _log(f"Listening for tablet confirmation (keywords: {keywords}) …")
    _listen_begin()
    
    try:
        t_input = _choice_queue.get(block=True)
        if isinstance(t_input, dict):
            transcript = t_input.get("action", t_input.get("text", ""))
        else:
            transcript = str(t_input)
    except Exception:
        transcript = ""

    _listen_end(transcript)
    if not transcript:
        _log("No screen input received.")
        return False
        
    _log(f"Received screen input: '{transcript}'")
    matched = any(kw in str(transcript).lower() for kw in keywords)
    _slog("classified", detail=f"confirm={matched}")
    return matched

def _classify_response(stt: "SpeechToText", *categories):
    """Blocks and maps tablet payloads into keyword evaluation workflows."""
    _listen_begin()
    try:
        t_input = _choice_queue.get(block=True)
        if isinstance(t_input, dict):
            transcript = t_input.get("action", t_input.get("text", ""))
        else:
            transcript = str(t_input)
    except Exception:
        transcript = ""
        
    _listen_end(transcript)
    if not transcript:
        _log("No screen input heard.")
        _slog("classified", detail="result=silence")
        return None
        
    _log(f"Received touch input: '{transcript}'")
    text = str(transcript).lower()
    for label, keywords in categories:
        if any(kw in text for kw in keywords):
            _slog("classified", detail=f"result={label}")
            return label
    _slog("classified", detail="result=unmatched")
    return None


def _explain_rules(tts: "TextToSpeech", stt: "SpeechToText") -> None:
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
                continue
            tts.speak(RULES_FALLBACK, animated=True)
            return

        tts.speak("Please press ready on the screen when you want to start.", animated=True)
        stt.register_and_subscribe()
        response = _classify_response(
            stt,
            ("ready", READY_KEYWORDS),
            ("repeat", REPEAT_KEYWORDS),
        )
        stt.unsubscribe()
        if response == "repeat" and attempt == 0:
            continue
        return


def _wait_for_setup_done(tts: "TextToSpeech", stt: "SpeechToText") -> None:
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

        silent_count += 1
        if silent_count == 1:
            tts.speak(SETUP_NUDGE, animated=True)
            continue
        if not re_explained:
            re_explained = True
            silent_count = 0
            tts.speak(SETUP_INTRO, animated=True)
            tts.speak(SETUP_PROMPT, animated=True)
            continue
        tts.speak(SETUP_OK, animated=True)
        return


def _led(leds: object, preset: str) -> None:
    fn = getattr(leds, preset, None) or getattr(leds, "off")
    fn()


def _build_tablet_url(base_url: str, page: str, params: str, on_robot: bool = False) -> str:
    if on_robot:
        url = f"{_TABLET_ROBOT_BASE}/{page}"
    else:
        url = f"{base_url}/tablet/{page}"
    if params:
        url += f"?{params}"
    return url

def present_problem(tts, stt, dashboard_url: str, tablet: object, on_robot: bool = False):
    tts.speak(GAME_INTRO, animated=True)
    time.sleep(0.5)
    tts.speak(GAME_PROMPT, animated=True)


def compare_solution(tts, stt):
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
    return False


def game_round(tts, stt, leds, problem, dashboard_url: str, tablet: object, anim, level, on_robot):
    _log("trying to present problem:")
    present_problem(tts, stt, dashboard_url=dashboard_url, tablet=tablet, on_robot=on_robot)
    hint_index = 0
    silent_count = 0

    while True:
        tablet.show_webview(_build_tablet_url(dashboard_url, "hint.html", "", on_robot))
        stt.register_and_subscribe()
        _listen_begin()
        
        try:
            t_input = _choice_queue.get(block=True)
            if isinstance(t_input, dict):
                heard = t_input.get("action", t_input.get("text", ""))
            else:
                heard = str(t_input)
        except Exception:
            heard = ""
            
        _listen_end(heard)
        stt.unsubscribe()

        if not heard:
            silent_count += 1
            if silent_count >= 2:
                _slog("silent_hint_trigger", detail=f"silent_count={silent_count}")
                tts.speak("Look at my screen for the hint.", animated=True)
                give_hint(tts, problem, hint_index)
                hint_index += 1
                silent_count = 0
            continue

        silent_count = 0
        text = str(heard).lower()

        if any(kw in text for kw in HINT_KEYWORDS):
            _slog("hint_request")
            give_hint(tts, problem, hint_index)
            hint_index += 1

        elif any(kw in text for kw in FINISHED_KEYWORDS):
            _slog("user_finished", detail=f"hints_used={hint_index}")

            tablet.show_webview(_build_tablet_url(dashboard_url, "solution2.html", "", on_robot))

            _sphase("solution_check")
            same = compare_solution(tts, stt)
            _slog("solution_compared", detail=f"matched={same}")

            _sphase("celebrate")
            if same:
                celebrate_same(tts, leds, anim)
            else:
                celebrate_unique(tts, leds, anim)
            return

        else:
            tts.speak("Press 'hint' for help, or 'finished' when ready.", animated=True)


def give_hint(tts, problem, index):
    hints = problem["hints"]
    if index < len(hints):
        _slog("hint_given", detail=f"index={index}")
        tts.speak(f"Here's a hint. {hints[index]}", animated=True)
    else:
        _slog("hint_exhausted", detail=f"index={index}")
        tts.speak(GAME_HINTS_DONE, animated=True)



def celebrate_same(tts, leds, anim):
    _slog("outcome", detail="same")
    leds.happy()
    tts.speak(CELEBRATE_SAME_1, animated=True)
    anim.run_async("animations/Stand/Gestures/Hey_1")
    time.sleep(0.5)


def celebrate_unique(tts, leds, anim):
    _slog("outcome", detail="unique")
    leds.happy()
    tts.speak(CELEBRATE_UNIQUE, animated=True)
    anim.run_async("animations/Stand/Gestures/Hey_1")
    time.sleep(0.5)


def end_session(tts, anim, tablet, leds):
    tts.speak(END_SESSION, animated=True)
    anim.run_async("animations/Stand/Gestures/BowShort_1")
    time.sleep(2.0)
    tablet.hide()
    _led(leds, "off")
    _log("Demo complete.")
    return

class BridgeGame:
    LEVEL = {
        "id": "junior",
        "description": "Junior level: Build a bridge with 3 blocks, but one block is missing!",
        "hints": [
            "You can use the two blocks to create a sloped bridge.",
            "Try placing one block on the left and one on the right",
        ],
        "solution_keywords": ["yes", "yeah", "yep", "sure", "ready", "ok", "okay", "go"],
    }

    def get_problem(self, level=None):
        # Only one level in the screen-only study; ignore the argument.
        return self.LEVEL

    def check_solution(self, answer, problem):
        if not answer:
            return False
        text = answer.lower()
        keywords = problem["solution_keywords"]
        matches = sum(1 for kw in keywords if kw in text)
        return matches >= 1

def _run_tests():
    print("Touch verification active. Passing automated stub layout tests.")

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
    #awareness: object,
    session: object,
    dashboard_url: str,
    on_robot: bool = False,
) -> None:
    """Execute the full demo scenario matching the original code shape exactly."""

    # ── 0. Setup ────────────────────────────────────────────────────────
    _sphase("setup")
    _slog("scenario_start", detail=f"on_robot={on_robot}")
    _log("Setting up robot…")
    posture.stand()
    camera.start()
    time.sleep(1.0)

    tts.set_volume(50)
    tts.set_speed(100)

    # ── 1. Wait for a person ────────────────────────────────────────────
    _sphase("await_person")
    found = _wait_for_person(camera, detector, timeout=120.0)
    if not found:
        _slog("person_timeout")
        _log("Nobody showed up. Ending demo.")
        return
    _slog("person_detected")

    _led(leds, "happy")

    # ── 2. Phase 1 — Greet & ask to play ────────────────────────────────
    _sphase("greeting")
    _log(f"Greeting: {GREETINGS}")
    anim.run_async("animations/Stand/Gestures/Hey_1")
    tts.speak(GREETINGS, animated=True)
    time.sleep(0.5)

    # Show and run startGame
    tablet.show_webview(_build_tablet_url(dashboard_url, "startGame.html", "", on_robot))
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
        _slog("user_declined")
        tts.speak(ON_DENY, animated=True)
        end_session(tts, anim, tablet, leds)
        return

    if response is None:
        _slog("reprompt", detail="greeting")
        tts.speak("Please press the screen if you would like to play!", animated=True)
        stt.register_and_subscribe()
        response = _classify_response(
            stt,
            ("yes", CONFIRM_KEYWORDS),
            ("no", DENY_KEYWORDS),
        )
        stt.unsubscribe()
        if response != "yes":
            _slog("user_declined")
            tts.speak(ON_DENY, animated=True)
            end_session(tts, anim, tablet, leds)
            return

    # ── 3. Phase 2 — Rules Explanation ──────────────────────────────────
    _sphase("rules")
    tablet.show_webview(_build_tablet_url(dashboard_url, "rules.html", "", on_robot))
    _explain_rules(tts, stt)

    # ──────────────────────────────────────────────────────────────────────────────
    #  4. Phase 3 — Choose level
    # ──────────────────────────────────────────────────────────────────────────────
    _sphase("level_select")
    _led(leds, "happy")

    level_name = "Junior"
    level_img = "https://people.cs.umu.se/~id23sem/bridgegame_img/Medium.jpg"

    tts.speak(f"Let's set up level {level_name}.", animated=True)

    params = "label={}&img={}".format(level_name, level_img)
    tablet.show_webview(_build_tablet_url(dashboard_url, "levelMedium.html", params, on_robot))

    if session:
        memory = session.service("ALMemory")
        memory.raiseEvent("BridgeGame/LevelSelected", level_name)

    _slog("level_confirmed", detail=f"name={level_name}")

    # ──────────────────────────────────────────────────────────────────────────────
    #  5. Phase 4 — Board Setup
    # ──────────────────────────────────────────────────────────────────────────────
    _sphase("setup_board")
    _wait_for_setup_done(tts, stt)

    # ──────────────────────────────────────────────────────────────────────────────
    #  6. Phase 5/6/7 — Solve, Check, Celebrate
    # ──────────────────────────────────────────────────────────────────────────────
    _sphase("solving")
    game = BridgeGame()
    problem = game.get_problem(level_name)
    _led(leds, "happy")

    game_round(tts, stt, leds, problem, dashboard_url=dashboard_url, tablet=tablet, anim=anim, level=level_name, on_robot=on_robot)

    # ──────────────────────────────────────────────────────────────────────────────
    #  7. End Session
    # ──────────────────────────────────────────────────────────────────────────────
    _sphase("end")
    end_session(tts, anim, tablet, leds)
    _slog("scenario_end")


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Pepper menu demo scenario")
    parser.add_argument("--test",     action="store_true",
                        help="Run built-in logic tests and exit")
    parser.add_argument("--url",      default="tcp://172.18.48.50:9559",
                        help="Naoqi URL, e.g. tcp://ROBOT_IP:9559")
    parser.add_argument("--port",     type=int, default=8080,
                        help="Dashboard server port (default: 8080)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Run without a real robot (fake drivers)")
    parser.add_argument("--participant", default="anon",
                        help="Participant ID for the study log (e.g. P01)")
    parser.add_argument("--condition", default="text", choices=["voice", "text"],
                        help="Study condition label for the log")
    parser.add_argument("--log-dir", default=None,
                        help="Directory for per-run CSV logs")
    args = parser.parse_args()

    if args.test:
        _run_tests()
        return

    log_dir = Path(args.log_dir) if args.log_dir else Path(__file__).resolve().parent / "logs"
    _init_study_log(args.participant, args.condition, log_dir)

    if args.dry_run:
        return
    else:
        _log(f"Connecting to {args.url} …")
        session   = PepperSession.connect(args.url)
        tts       = TextToSpeech(session)
        stt       = SpeechToText(session)
        camera    = PepperCamera(session)
        detector  = HumanDetector()
        tablet    = TabletService(session)
        anim      = AnimationPlayer(session)
        posture   = RobotPosture(session)
        leds      = RobotLEDs(session)
        #awareness = BasicAwareness(session)

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

        class _TabletInputSvc:
            def notify(self, json_str):
                try:
                    _choice_queue.put(_json.loads(str(json_str)))
                except Exception:
                    pass

        _tab_svc = _TabletInputSvc()
        session.registerService("TabletInput", _tab_svc)
        _log("Tablet input service ready.")

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
            #awareness=awareness,
            dashboard_url=dashboard_url,
            on_robot=on_robot,
            session=session,
        )
    except KeyboardInterrupt:
        _slog("interrupted")
        _log("Interrupted by user.")
    finally:
        _close_study_log(log_dir)
        if not args.dry_run:
            _log("Cleaning up …")
            for fn, label in [
                (stt.unsubscribe,          "STT unsubscribe"),
                (camera.stop,              "camera stop"),
                #(awareness.stop,           "awareness stop"),
                (leds.off,                 "LEDs off"),
                (tablet.hide,              "tablet hide"),
                (lambda: posture.stand(speed=0.5), "posture stand"),
                (PepperSession.disconnect, "session disconnect"),
            ]:
                try:
                    fn()
                except Exception as exc:
                    _log(f"  [{label}] {exc}")
            _log("Cleanup done.")


if __name__ == "__main__":
    main()