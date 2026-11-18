# SPDX-License-Identifier: MPL-2.0
"""UNO Q Gesture RPS.

The browser sends webcam JPEG frames to WebSocketCamera on port 8080.
VideoObjectDetection runs the on-board `hand-gestures` model.
Python owns the game state and sends small state packets to the browser.
The 8x13 LED matrix is driven through Arduino Router Bridge.

Model labels:
    neut  -> rock
    five  -> paper
    peace -> scissors
    good  -> ignored (thumbs-up / ready gesture)
"""

from __future__ import annotations

import random
import threading
import time
from datetime import UTC, datetime

import numpy as np
from fastapi import Request

from arduino.app_bricks.video_objectdetection import VideoObjectDetection
from arduino.app_bricks.web_ui import WebUI
from arduino.app_peripherals.camera import WebSocketCamera
from arduino.app_utils import App, Bridge, Frame

# ----------------------------- configuration -----------------------------

MOVE_LABELS = {
    "neut": "rock",
    "neutral": "rock",
    "fist": "rock",
    "closed_fist": "rock",
    "five": "paper",
    "open_palm": "paper",
    "paper": "paper",
    "peace": "scissors",
    "victory": "scissors",
    "scissors": "scissors",
}

LABEL_ZH = {
    "neut": "石頭",
    "five": "布",
    "peace": "剪刀",
    "good": "讚",
}

MOVE_ZH = {"rock": "石頭", "paper": "布", "scissors": "剪刀"}

MOVES = ("rock", "paper", "scissors")
CONFIDENCE_THRESHOLD = 0.30
STABLE_FRAMES = 4
COUNTDOWN_SECONDS = 0.42
RESULT_SECONDS = 2.2
RELEASE_SECONDS = 0.65

# ----------------------------- app objects -------------------------------

ui = WebUI()

# The browser is the camera.  No USB camera or CSI camera is required.
camera = WebSocketCamera(
    port=8080,
    resolution=(320, 240),
    fps=14,
    secret=None,
    encrypt=False,
)

detection = VideoObjectDetection(
    camera,
    confidence=CONFIDENCE_THRESHOLD,
    debounce_sec=0.0,
)

# ----------------------------- game state --------------------------------

state_lock = threading.RLock()

score = {"player": 0, "ai": 0}
game_locked = False
paused = False
pause_version = 0
active_round = 0
gesture_released = True
stable_move: str | None = None
stable_count = 0
last_valid_at = 0.0
last_preview_at = 0.0
last_preview_key: tuple | None = None
last_debug_label: str | None = None
last_debug_at = 0.0
round_number = 0
current_label: str | None = None
current_confidence = 0.0

matrix_failures = 0
matrix_disabled = False

# --------------------------- LED matrix frames ----------------------------
# The sketch accepts a 104-byte 8x13 matrix with brightness values 0..7.

FONT = {
    "A": ["010", "101", "111", "101", "101"],
    "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "110", "100", "111"],
    "I": ["111", "010", "010", "010", "111"],
    "L": ["100", "100", "100", "100", "111"],
    "N": ["101", "111", "111", "101", "101"],
    "O": ["111", "101", "101", "101", "111"],
    "R": ["110", "101", "110", "101", "101"],
    "S": ["111", "100", "111", "001", "111"],
    "W": ["101", "101", "111", "111", "101"],
    "X": ["101", "101", "010", "101", "101"],
}

DIGITS = {
    1: ["010", "110", "010", "010", "111"],
    2: ["111", "001", "111", "100", "111"],
    3: ["111", "001", "111", "001", "111"],
}


def _blank() -> list[list[int]]:
    return [[0 for _ in range(13)] for _ in range(8)]


def _stamp(frame: list[list[int]], glyph: list[str], left: int, top: int) -> None:
    for r, row in enumerate(glyph):
        for c, value in enumerate(row):
            if value == "1" and 0 <= top + r < 8 and 0 <= left + c < 13:
                frame[top + r][left + c] = 7


def _text_frame(text: str) -> list[list[int]]:
    frame = _blank()
    width = len(text) * 3 + max(0, len(text) - 1)
    left = max(0, (13 - width) // 2)
    for i, char in enumerate(text):
        glyph = FONT.get(char)
        if glyph:
            _stamp(frame, glyph, left + i * 4, 1)
    return frame


def idle_frame() -> list[list[int]]:
    # Friendly pair of eyes and a small smile.
    return [
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 1, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0],
        [0, 0, 1, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    ]


def paused_frame() -> list[list[int]]:
    frame = _blank()
    for row in range(1, 7):
        frame[row][5] = 7
        frame[row][7] = 7
    return frame


def result_frame(result: str) -> list[list[int]]:
    if result == "win":
        return [
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        ]
    if result == "lose":
        return [
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0],
            [0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0],
            [0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0],
            [0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0],
            [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0],
        ]
    return [
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    ]


def countdown_frame(number: int) -> list[list[int]]:
    frame = _blank()
    _stamp(frame, DIGITS[number], 5, 1)
    return frame


def show_frame(frame: list[list[int]]) -> None:
    global matrix_failures, matrix_disabled
    if matrix_disabled:
        return
    try:
        array = np.array(frame, dtype=np.uint8)
        Bridge.call("draw", Frame(array).to_board_bytes())
        matrix_failures = 0
    except Exception as exc:
        matrix_failures += 1
        if matrix_failures in (1, 5, 20):
            print(f"matrix bridge unavailable ({matrix_failures}): {exc}", flush=True)
        if matrix_failures >= 20:
            matrix_disabled = True


# ----------------------------- game helpers -------------------------------

def judge(player: str, ai: str) -> str:
    if player == ai:
        return "draw"
    if (
        (player == "rock" and ai == "scissors")
        or (player == "paper" and ai == "rock")
        or (player == "scissors" and ai == "paper")
    ):
        return "win"
    return "lose"


def _public_state(
    status: str,
    *,
    player: str | None = None,
    ai: str | None = None,
    result: str | None = None,
    message: str = "",
    round_id: int | None = None,
) -> dict:
    with state_lock:
        snapshot = {
            "status": status,
            "player": player,
            "ai": ai,
            "result": result,
            "score": dict(score),
            "message": message,
            "round": round_id,
            "paused": paused,
        }
    return snapshot


def send_state(
    status: str,
    *,
    player: str | None = None,
    ai: str | None = None,
    result: str | None = None,
    message: str = "",
    round_id: int | None = None,
) -> None:
    try:
        ui.send_message(
            "game_state",
            _public_state(
                status,
                player=player,
                ai=ai,
                result=result,
                message=message,
                round_id=round_id,
            ),
        )
    except Exception as exc:
        print(f"ui game_state error: {exc}", flush=True)


def send_preview(label: str | None, confidence: float, stable: int) -> None:
    global last_preview_at, last_preview_key
    now = time.monotonic()
    key = (label, stable)
    if key == last_preview_key and now - last_preview_at < 0.20:
        return
    last_preview_at = now
    last_preview_key = key
    move = MOVE_LABELS.get(label) if label else None
    try:
        ui.send_message(
            "gesture_preview",
            {
                "label": label,
                "label_zh": LABEL_ZH.get(label or "", label or ""),
                "move": move,
                "move_zh": MOVE_ZH.get(move or "", ""),
                "confidence": round(float(confidence), 3),
                "stable": stable,
                "needed": STABLE_FRAMES,
            },
        )
    except Exception as exc:
        print(f"ui preview error: {exc}", flush=True)


def _round_cancelled(this_round: int, version: int) -> bool:
    with state_lock:
        return paused or pause_version != version or active_round != this_round


def _cancel_round(this_round: int) -> None:
    global game_locked, active_round, stable_move, stable_count
    with state_lock:
        if active_round == this_round:
            active_round = 0
        game_locked = False
        stable_move = None
        stable_count = 0
        is_paused = paused
    if is_paused:
        show_frame(paused_frame())
        send_state("paused", message="遊戲已暫停")
    else:
        show_frame(idle_frame())
        send_state("idle", message="本局已取消，比出手勢再開始")


def _round_worker(player_move: str) -> None:
    global score, game_locked, round_number, stable_move, stable_count
    global active_round

    with state_lock:
        round_number += 1
        this_round = round_number
        active_round = this_round
        version = pause_version
        player_name = MOVE_ZH[player_move]

    send_state(
        "countdown",
        player=player_move,
        message=f"你出{player_name}，準備…",
        round_id=this_round,
    )

    for number in (3, 2, 1):
        if _round_cancelled(this_round, version):
            _cancel_round(this_round)
            return
        show_frame(countdown_frame(number))
        send_state(
            "countdown",
            player=player_move,
            message=str(number),
            round_id=this_round,
        )
        for _ in range(4):
            time.sleep(COUNTDOWN_SECONDS / 4)
            if _round_cancelled(this_round, version):
                _cancel_round(this_round)
                return

    if _round_cancelled(this_round, version):
        _cancel_round(this_round)
        return

    ai_move = random.choice(MOVES)
    result = judge(player_move, ai_move)
    with state_lock:
        if result == "win":
            score["player"] += 1
        elif result == "lose":
            score["ai"] += 1

    result_text = {
        "win": "你贏了！",
        "lose": "電腦贏了",
        "draw": "平手，再來一次",
    }[result]

    show_frame(result_frame(result))
    send_state(
        "result",
        player=player_move,
        ai=ai_move,
        result=result,
        message=result_text,
        round_id=this_round,
    )

    for _ in range(22):
        time.sleep(RESULT_SECONDS / 22)
        if _round_cancelled(this_round, version):
            _cancel_round(this_round)
            return

    show_frame(idle_frame())
    send_state("idle", message="再比一次手勢")
    with state_lock:
        game_locked = False
        active_round = 0
        stable_move = None
        stable_count = 0

def start_round(player_move: str) -> None:
    threading.Thread(target=_round_worker, args=(player_move,), daemon=True).start()


# ----------------------------- AI callbacks -------------------------------

def _normalize_label(label: str) -> str:
    return label.strip().lower().replace("-", "_").replace(" ", "_")


def _best_detection(detections: dict) -> tuple[str, float] | None:
    best: tuple[str, float] | None = None
    if not isinstance(detections, dict):
        return None
    for raw_label, entries in detections.items():
        label = _normalize_label(str(raw_label))
        if not isinstance(entries, list):
            entries = [entries]
        for entry in entries:
            if isinstance(entry, dict):
                confidence = float(entry.get("confidence", 0.0) or 0.0)
            else:
                try:
                    confidence = float(entry)
                except (TypeError, ValueError):
                    confidence = 0.0
            if confidence >= CONFIDENCE_THRESHOLD and (best is None or confidence > best[1]):
                best = (label, confidence)
    return best


def report_camera_status(event_type: str, data: dict) -> None:
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")
    print(f"camera:{event_type} {data}", flush=True)
    try:
        ui.send_message(
            "camera_status",
            {"event": event_type, "data": data, "timestamp": timestamp},
        )
    except Exception:
        pass


def on_detections(detections: dict) -> None:
    global game_locked, gesture_released, stable_move, stable_count
    global last_valid_at, current_label, current_confidence

    best = _best_detection(detections)
    now = time.monotonic()

    if best is None:
        with state_lock:
            stable_move = None
            stable_count = 0
            current_label = None
            current_confidence = 0.0
            if not game_locked:
                gesture_released = True
        send_preview(None, 0.0, 0)
        return

    label, confidence = best
    move = MOVE_LABELS.get(label)
    global last_debug_label, last_debug_at
    if label != last_debug_label or now - last_debug_at >= 1.5:
        last_debug_label = label
        last_debug_at = now
        print(f"detection: label={label} confidence={confidence:.2f} move={move}", flush=True)
    should_start = False
    with state_lock:
        current_label = label
        current_confidence = confidence
        last_valid_at = now

        if move is None:
            stable_move = None
            stable_count = 0
            if not game_locked:
                gesture_released = True
        else:
            if move == stable_move:
                stable_count += 1
            else:
                stable_move = move
                stable_count = 1

            should_start = (
                not paused
                and not game_locked
                and gesture_released
                and stable_count >= STABLE_FRAMES
            )
            if should_start:
                game_locked = True
                gesture_released = False
                stable_count = 0

    preview_stable = 0
    with state_lock:
        if stable_move is not None:
            preview_stable = stable_count
    send_preview(label, confidence, preview_stable)

    if should_start:
        print(
            f"gesture stable: {label} -> {move} ({confidence:.2f})",
            flush=True,
        )
        start_round(move)


def _release_watchdog() -> None:
    global gesture_released
    while True:
        time.sleep(0.10)
        with state_lock:
            if not game_locked and time.monotonic() - last_valid_at > RELEASE_SECONDS:
                gesture_released = True


# ----------------------------- UI callbacks --------------------------------

def on_ui_connect(_sid: str) -> None:
    print("browser connected", flush=True)
    with state_lock:
        is_paused = paused
    if is_paused:
        show_frame(paused_frame())
        send_state("paused", message="遊戲已暫停")
    else:
        show_frame(idle_frame())
        send_state("idle", message="比出手勢，開始第一局")
    send_preview(None, 0.0, 0)


def on_reset_game(_sid: str, _payload) -> None:
    global score, game_locked, gesture_released, stable_move, stable_count
    with state_lock:
        score = {"player": 0, "ai": 0}
        game_locked = False
        gesture_released = True
        stable_move = None
        stable_count = 0
    with state_lock:
        is_paused = paused
    if is_paused:
        show_frame(paused_frame())
        send_state("paused", message="分數已重設，遊戲仍在暫停")
    else:
        show_frame(idle_frame())
        send_state("idle", message="分數已重設，比出手勢開始")
    send_preview(None, 0.0, 0)


def set_paused(value: bool) -> None:
    global paused, pause_version, game_locked, active_round, stable_move, stable_count
    with state_lock:
        paused = bool(value)
        pause_version += 1
        game_locked = False
        active_round = 0
        stable_move = None
        stable_count = 0
    if paused:
        print("game paused", flush=True)
        show_frame(paused_frame())
        send_state("paused", message="遊戲已暫停")
    else:
        print("game resumed", flush=True)
        show_frame(idle_frame())
        send_state("idle", message="遊戲繼續，比出手勢開始")


def on_set_paused(_sid: str, payload) -> None:
    if isinstance(payload, dict):
        set_paused(bool(payload.get("paused", False)))


def trigger_move(move: str) -> bool:
    """Start a round from an external gesture classifier if the board is free."""
    global game_locked, gesture_released, stable_move, stable_count
    if move not in MOVES:
        return False
    with state_lock:
        if paused or game_locked:
            return False
        game_locked = True
        gesture_released = False
        stable_move = None
        stable_count = 0
    print(f"external move accepted: {move}", flush=True)
    start_round(move)
    return True


def on_manual_move(_sid: str, payload) -> None:
    """Hidden keyboard fallback: send {'move':'rock'} over WebUI."""
    move = payload.get("move") if isinstance(payload, dict) else None
    if isinstance(move, str):
        trigger_move(move)


async def gesture_api(request: Request):
    """HTTP entry point for the PC-side MediaPipe gesture classifier."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    move = payload.get("move") if isinstance(payload, dict) else None
    if not isinstance(move, str):
        return {"ok": False, "reason": "missing move"}
    with state_lock:
        is_paused = paused
    if is_paused:
        return {"ok": False, "move": move, "reason": "paused"}
    accepted = trigger_move(move)
    return {"ok": accepted, "move": move, "reason": None if accepted else "busy"}


# ----------------------------- wiring --------------------------------------

ui.expose_api("POST", "/gesture", gesture_api)
ui.on_connect(on_ui_connect)
ui.on_message("reset_game", on_reset_game)
ui.on_message("set_paused", on_set_paused)
ui.on_message("manual_move", on_manual_move)

camera.on_status_changed(report_camera_status)
detection.on_detect_all(on_detections)

threading.Thread(target=_release_watchdog, daemon=True).start()

print("UNO Q Gesture RPS starting", flush=True)
App.run()
