import json
import math
import os
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = str(BASE_DIR / 'hand_landmarker.task')
API_URL = os.getenv('UNOQ_GESTURE_API', 'http://127.0.0.1:17000/gesture')
PREVIEW_HOST = os.getenv('GESTURE_PREVIEW_HOST', '127.0.0.1')
PREVIEW_PORT = int(os.getenv('GESTURE_PREVIEW_PORT', '8090'))
PREFERRED_CAMERA = int(os.getenv('CAMERA_INDEX', '1'))
CAMERA_SCAN_LIMIT = 6
SEND_FPS = 12
STABLE_FRAMES = 4
COOLDOWN_SEC = 3.6
LOG_PATH = str(BASE_DIR / 'pc_mediapipe.log')

latest_jpeg = None
latest_lock = threading.Lock()
latest_status = {'gesture': None, 'stable': 0, 'needed': STABLE_FRAMES}
status_lock = threading.Lock()
stop_event = threading.Event()

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]


def log(message):
    line = f'[{time.strftime("%H:%M:%S")}] {message}'
    print(line, flush=True)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def distance(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def classify_hand(landmarks):
    """Return rock/paper/scissors from four non-thumb fingers."""
    wrist = landmarks[0]
    checks = {
        'index': (8, 6),
        'middle': (12, 10),
        'ring': (16, 14),
        'pinky': (20, 18),
    }
    extended = {}
    for name, (tip_idx, pip_idx) in checks.items():
        tip_distance = distance(landmarks[tip_idx], wrist)
        pip_distance = distance(landmarks[pip_idx], wrist)
        extended[name] = tip_distance > pip_distance * 1.12

    count = sum(extended.values())
    if count >= 4:
        gesture = 'paper'
    elif extended['index'] and extended['middle'] and not extended['ring'] and not extended['pinky']:
        gesture = 'scissors'
    elif count == 0:
        gesture = 'rock'
    else:
        gesture = None
    return gesture, extended


def open_camera():
    # DroidCam is normally index 1 on this PC. Try it first, then fall back.
    candidates = [PREFERRED_CAMERA] + [i for i in range(CAMERA_SCAN_LIMIT) if i != PREFERRED_CAMERA]
    best = None
    best_score = -1.0
    for index in candidates:
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            continue
        frame = None
        for _ in range(4):
            ok, candidate = cap.read()
            if ok and candidate is not None:
                frame = candidate
            time.sleep(0.06)
        cap.release()
        if frame is None:
            continue
        score = float(frame.std())
        log(f'camera {index}: shape={frame.shape} std={score:.1f}')
        if score > best_score:
            best_score = score
            best = index
    if best is None:
        return None
    log(f'using camera index {best}')
    return best


def post_move(move):
    payload = json.dumps({'move': move}).encode('utf-8')
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            body = response.read().decode('utf-8', errors='replace')
            log(f'board response: {body}')
            return response.status == 200
    except Exception as exc:
        log(f'board POST failed: {exc}')
        return False


def draw_preview(frame, landmarks, gesture, extended):
    h, w = frame.shape[:2]
    if landmarks:
        points = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]
        for a, b in HAND_CONNECTIONS:
            cv2.line(frame, points[a], points[b], (74, 222, 128), 2, cv2.LINE_AA)
        for point in points:
            cv2.circle(frame, point, 4, (76, 224, 230), -1, cv2.LINE_AA)

    label = (gesture or 'NO MOVE').upper()
    color = {'ROCK': (80, 200, 255), 'PAPER': (100, 255, 120), 'SCISSORS': (255, 140, 220)}.get(label, (180, 180, 180))
    cv2.rectangle(frame, (8, 8), (260, 52), (8, 14, 24), -1)
    cv2.putText(frame, label, (16, 41), cv2.FONT_HERSHEY_SIMPLEX, 1.05, color, 2, cv2.LINE_AA)
    if landmarks:
        finger_text = 'I{} M{} R{} P{}'.format(
            int(extended.get('index', False)),
            int(extended.get('middle', False)),
            int(extended.get('ring', False)),
            int(extended.get('pinky', False)),
        )
        cv2.putText(frame, finger_text, (150, 37), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220, 230, 240), 1, cv2.LINE_AA)
    return frame


def capture_loop():
    global latest_jpeg
    landmarker = vision.HandLandmarker.create_from_options(
        vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
    )
    log('MediaPipe HandLandmarker ready')

    camera_index = open_camera()
    if camera_index is None:
        log('no camera found')
        return
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if not cap.isOpened():
        log('camera reopen failed')
        return

    candidate = None
    stable = 0
    armed = True
    last_trigger = 0.0
    frame_count = 0
    fps_at = time.monotonic()

    while not stop_event.is_set():
        ok, frame = cap.read()
        if not ok or frame is None:
            log('camera read failed')
            time.sleep(0.25)
            continue

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        timestamp_ms = int(time.monotonic() * 1000)
        result = landmarker.detect_for_video(mp_image, timestamp_ms)

        gesture = None
        extended = {}
        landmarks = None
        if result.hand_landmarks:
            landmarks = result.hand_landmarks[0]
            gesture, extended = classify_hand(landmarks)

        now = time.monotonic()
        if gesture is not None:
            if gesture == candidate:
                stable += 1
            else:
                candidate = gesture
                stable = 1
            if armed and stable >= STABLE_FRAMES and now - last_trigger >= COOLDOWN_SEC:
                log(f'stable gesture: {gesture} fingers={extended}')
                accepted = post_move(gesture)
                last_trigger = now
                armed = False
                stable = 0
                candidate = None
                if not accepted:
                    log('board was busy; waiting for next gesture')
        else:
            candidate = None
            stable = 0
            if not armed and now - last_trigger >= 3.4:
                armed = True
                log('gesture channel re-armed')

        with status_lock:
            latest_status.update({
                'gesture': gesture,
                'stable': stable,
                'needed': STABLE_FRAMES,
                'armed': armed,
            })

        preview = draw_preview(frame.copy(), landmarks, gesture, extended)
        preview = cv2.resize(preview, (480, 360), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode('.jpg', preview, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok:
            with latest_lock:
                latest_jpeg = encoded.tobytes()

        frame_count += 1
        if now - fps_at >= 2.0:
            log(f'loop {frame_count / (now - fps_at):.1f} FPS')
            frame_count = 0
            fps_at = now

        time.sleep(max(0.0, (1.0 / SEND_FPS) - (time.monotonic() - now)))

    cap.release()
    landmarker.close()


class PreviewHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path == '/status':
            with status_lock:
                body = json.dumps(latest_status).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path not in ('/', '/video'):
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Connection', 'close')
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        try:
            while not stop_event.is_set():
                with latest_lock:
                    jpeg = latest_jpeg
                if jpeg is None:
                    time.sleep(0.05)
                    continue
                self.wfile.write(b'--frame\r\n')
                self.wfile.write(b'Content-Type: image/jpeg\r\n')
                self.wfile.write(f'Content-Length: {len(jpeg)}\r\n\r\n'.encode())
                self.wfile.write(jpeg)
                self.wfile.write(b'\r\n')
                self.wfile.flush()
                time.sleep(0.07)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return


def main():
    log('PC MediaPipe gesture bridge starting')
    threading.Thread(target=capture_loop, name='capture', daemon=True).start()
    server = ThreadingHTTPServer((PREVIEW_HOST, PREVIEW_PORT), PreviewHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name='preview', daemon=True).start()
    log(f'preview server: http://{PREVIEW_HOST}:{PREVIEW_PORT}/video')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()
        server.shutdown()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log('FATAL: ' + traceback.format_exc())
        raise
