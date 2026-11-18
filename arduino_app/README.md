# UNO Q Gesture RPS

Interactive rock-paper-scissors with:

- Phone camera via DroidCam on the PC
- PC-side MediaPipe HandLandmarker (21 hand landmarks)
- Rock / paper / scissors from finger-extension geometry
- HTTP `POST /gesture` to the UNO Q WebUI
- UNO Q dual-brain game logic + 8x13 LED matrix feedback
- Browser UI with camera preview, score, countdown, pause/resume

## Runtime split

| Component | Responsibility |
|---|---|
| Phone + DroidCam | Camera source |
| PC + OpenCV | Capture frames |
| PC + MediaPipe | Hand landmarks and gesture classification |
| PC preview server | MJPEG preview on port 8090 |
| UNO Q Linux | WebUI, game state, score, pause, HTTP `/gesture` |
| UNO Q MCU | 8x13 LED frames through Bridge/RPC |
| Browser | Preview + scoreboard + pause button |

## Gesture mapping

| Fingers extended | Gesture |
|---|---|
| 0 | Rock |
| Index + middle | Scissors |
| 4 | Paper |

## PC bridge

Run:

```bash
python pc_mediapipe_gesture.py
```

The script serves the preview at `http://127.0.0.1:8090/video` and POSTs classified moves to `http://127.0.0.1:17000/gesture`.

## UNO Q app

```bash
arduino-app-cli app start /home/arduino/ArduinoApps/unoq_gesture_rps
adb -s <board-serial> forward tcp:17000 tcp:7000
adb -s <board-serial> forward tcp:18080 tcp:8080
```

Open `http://127.0.0.1:17000/?externalCamera=1&previewUrl=http://127.0.0.1:8090/video`.

## Pause

The pause button cancels the active round, rejects new gestures with
`{"ok":false,"reason":"paused"}`, and keeps the LED matrix in a paused state.
Press again to resume at READY.
