// SPDX-License-Identifier: MPL-2.0
const $ = (id) => document.getElementById(id);

const video = $('camera');
const canvas = $('captureCanvas');
const ctx = canvas.getContext('2d');
const boardPill = $('boardPill');
const cameraState = $('cameraState');
const fpsValue = $('fpsValue');
const gestureBadge = $('gestureBadge');
const gestureText = $('gestureText');
const gestureConfidence = $('gestureConfidence');
const confidenceFill = $('confidenceFill');
const countdownOverlay = $('countdownOverlay');
const countdownValue = $('countdownValue');
const playerScore = $('playerScore');
const aiScore = $('aiScore');
const playerIcon = $('playerIcon');
const playerName = $('playerName');
const aiIcon = $('aiIcon');
const aiName = $('aiName');
const playerMoveCard = $('playerMove');
const aiMoveCard = $('aiMove');
const resultTitle = $('resultTitle');
const resultSub = $('resultSub');
const gamePanel = $('gamePanel');
const startCameraButton = $('startCameraButton');
const cameraHelp = $('cameraHelp');
const resetScore = $('resetScore');
const roundTag = $('roundTag');
const pauseButton = $('pauseButton');
const externalFeed = $('externalFeed');
const query = new URLSearchParams(window.location.search);
const externalCameraMode = true; // PC sender owns the only WebSocketCamera connection

const MOVES = {
  rock: { icon: '✊', name: '石頭' },
  paper: { icon: '✋', name: '布' },
  scissors: { icon: '✌️', name: '剪刀' },
};

const ui = new WebUI();
let boardConnected = false;
let stream = null;
let captureTimer = null;
let fpsTimer = null;
let frameSocket = null;
let retryTimer = null;
let frameSocketOpen = false;
let sendingFrame = false;
let framesSent = 0;
let cameraReady = false;
let isPaused = false;

ui.on_connect(() => {
  boardConnected = true;
  updateConnectionPill();
});
ui.on_disconnect(() => {
  boardConnected = false;
  updateConnectionPill();
});
ui.on_message('game_state', onGameState);
ui.on_message('gesture_preview', onGesturePreview);
ui.on_message('camera_status', () => {
  if (externalCameraMode) cameraHelp.textContent = '外部鏡頭已連到 UNO Q，AI 正在辨識手勢。';
});

resetScore.addEventListener('click', () => ui.send_message('reset_game', {}));
pauseButton.addEventListener('click', () => {
  ui.send_message('set_paused', { paused: !isPaused });
});
startCameraButton.addEventListener('click', startCamera);

document.addEventListener('keydown', (event) => {
  if (event.repeat) return;
  const key = event.key.toLowerCase();
  const move = key === 'r' ? 'rock' : key === 'p' ? 'paper' : key === 's' ? 'scissors' : null;
  if (move) ui.send_message('manual_move', { move });
});

function updateConnectionPill() {
  if (boardConnected && (externalCameraMode || frameSocketOpen)) {
    boardPill.textContent = 'UNO Q · AI 連線中';
    boardPill.className = 'connection-pill online';
  } else if (boardConnected) {
    boardPill.textContent = 'UNO Q · 等待鏡頭';
    boardPill.className = 'connection-pill waiting';
  } else {
    boardPill.textContent = 'UNO Q · 連線中';
    boardPill.className = 'connection-pill offline';
  }
}

async function startCamera() {
  if (stream) {
    if (!frameSocketOpen) openFrameSocket();
    return;
  }

  startCameraButton.hidden = true;
  cameraHelp.textContent = '請允許瀏覽器使用鏡頭…';
  cameraState.textContent = '請求鏡頭權限中…';

  try {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('getUserMedia unavailable');
    }

    const timeout = new Promise((_, reject) => {
      setTimeout(() => reject(new Error('camera timeout')), 8000);
    });
    const mediaRequest = navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { width: { ideal: 640 }, height: { ideal: 480 } },
    });
    let initialStream = await Promise.race([mediaRequest, timeout]);

    // Prefer a virtual phone camera if one is present.
    const devices = await navigator.mediaDevices.enumerateDevices();
    const preferred = devices.find((device) =>
      device.kind === 'videoinput' && /droidcam|iriun|obs virtual|virtual camera/i.test(device.label || ''),
    );
    const activeId = initialStream.getVideoTracks()[0]?.getSettings().deviceId;
    if (preferred && preferred.deviceId !== activeId) {
      initialStream.getTracks().forEach((track) => track.stop());
      initialStream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { deviceId: { exact: preferred.deviceId }, width: { ideal: 640 }, height: { ideal: 480 } },
      });
    }
    stream = initialStream;

    video.srcObject = stream;
    await new Promise((resolve) => {
      if (video.readyState >= 2) return resolve();
      video.onloadedmetadata = () => resolve();
    });
    await video.play();

    cameraReady = true;
    cameraState.textContent = '鏡頭已開啟';
    cameraHelp.textContent = '比出石頭、布或剪刀，手勢穩定約 0.5 秒就會自動出拳。';
    $('cameraPlaceholder').hidden = true;
    openFrameSocket();

    if (captureTimer) clearInterval(captureTimer);
    captureTimer = setInterval(captureFrame, 1000 / 12);

    if (!fpsTimer) {
      fpsTimer = setInterval(() => {
        fpsValue.textContent = framesSent + ' FPS';
        framesSent = 0;
      }, 1000);
    }
  } catch (error) {
    console.error('camera error', error);
    stream = null;
    cameraReady = false;
    cameraState.textContent = '鏡頭未開啟';
    cameraHelp.textContent = '沒有偵測到可用鏡頭。接上 USB 攝影機或手機虛擬鏡頭後，按下方按鈕重新開啟。';
    startCameraButton.hidden = false;
  }
}

function openFrameSocket() {
  if (frameSocket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(frameSocket.readyState)) return;
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }

  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const host = window.location.hostname || 'localhost';
  const params = new URLSearchParams(window.location.search);
  const cameraPort = params.get('cameraPort') || '8080';
  const url = protocol + '://' + host + ':' + cameraPort + '?raw=true&client_name=unoq_gesture_rps';
  cameraState.textContent = '連接 UNO Q AI…';

  try {
    frameSocket = new WebSocket(url);
    frameSocket.binaryType = 'arraybuffer';
    frameSocket.onopen = () => {
      frameSocketOpen = true;
      cameraState.textContent = 'AI 已連上，開始辨識';
      updateConnectionPill();
    };
    frameSocket.onclose = () => {
      frameSocketOpen = false;
      updateConnectionPill();
      if (cameraReady) {
        cameraState.textContent = 'AI 串流中斷，重連中…';
        retryTimer = setTimeout(openFrameSocket, 1800);
      }
    };
    frameSocket.onerror = () => { frameSocketOpen = false; updateConnectionPill(); };
  } catch (error) {
    console.error('websocket error', error);
    retryTimer = setTimeout(openFrameSocket, 1800);
  }
}

function captureFrame() {
  if (!cameraReady || !stream || sendingFrame) return;
  if (!frameSocket || frameSocket.readyState !== WebSocket.OPEN) return;
  if (video.videoWidth === 0 || video.videoHeight === 0) return;
  sendingFrame = true;

  try {
    const targetAspect = 320 / 240;
    const sourceAspect = video.videoWidth / video.videoHeight;
    let sx = 0, sy = 0, sw = video.videoWidth, sh = video.videoHeight;
    if (sourceAspect > targetAspect) {
      sw = video.videoHeight * targetAspect;
      sx = (video.videoWidth - sw) / 2;
    } else {
      sh = video.videoWidth / targetAspect;
      sy = (video.videoHeight - sh) / 2;
    }
    ctx.drawImage(video, sx, sy, sw, sh, 0, 0, 320, 240);

    canvas.toBlob(async (blob) => {
      try {
        if (!blob || !frameSocket || frameSocket.readyState !== WebSocket.OPEN) return;
        const bytes = await blob.arrayBuffer();
        if (!frameSocket || frameSocket.readyState !== WebSocket.OPEN) return;
        frameSocket.send(bytes);
        framesSent += 1;
      } catch (error) {
        console.error('send error', error);
      } finally {
        sendingFrame = false;
      }
    }, 'image/jpeg', 0.62);
  } catch (error) {
    sendingFrame = false;
    console.error('capture error', error);
  }
}

function moveData(move) {
  return MOVES[move] || { icon: '—', name: '等待' };
}

function setMove(card, icon, name, move, placeholder) {
  if (!move) {
    icon.textContent = placeholder || '—';
    name.textContent = '等待';
    card.classList.remove('revealed');
    return;
  }
  const data = moveData(move);
  icon.textContent = data.icon;
  name.textContent = data.name;
  card.classList.add('revealed');
}

function onGameState(state) {
  if (!state || typeof state !== 'object') return;
  isPaused = Boolean(state.paused);
  pauseButton.textContent = isPaused ? '▶ 繼續' : '⏸ 暫停';
  pauseButton.classList.toggle('active', isPaused);
  gamePanel.classList.toggle('paused', isPaused);

  if (state.score) {
    playerScore.textContent = state.score.player || 0;
    aiScore.textContent = state.score.ai || 0;
  }
  if (state.round) roundTag.textContent = 'ROUND ' + state.round;

  if (state.player) {
    setMove(playerMoveCard, playerIcon, playerName, state.player, '✊');
  }

  if (state.status === 'paused') {
    countdownOverlay.hidden = true;
    resultTitle.className = 'result-title paused';
    resultTitle.textContent = '遊戲已暫停';
    resultSub.textContent = state.message || '按右上角「繼續」回到遊戲';
    aiIcon.textContent = '🤖';
    aiName.textContent = '暫停';
    aiMoveCard.classList.remove('revealed');
  } else if (state.status === 'countdown') {
    gamePanel.classList.remove('result-win', 'result-lose', 'result-draw');
    countdownOverlay.hidden = false;
    countdownValue.textContent = /^[123]$/.test(String(state.message)) ? state.message : '…';
    countdownValue.classList.remove('pop');
    void countdownValue.offsetWidth;
    countdownValue.classList.add('pop');
    playerMoveCard.classList.remove('pulse');
    void playerMoveCard.offsetWidth;
    playerMoveCard.classList.add('pulse');
    resultTitle.className = 'result-title';
    resultTitle.textContent = /^[123]$/.test(String(state.message)) ? '準備出拳…' : state.message;
    resultSub.textContent = 'AI 將在同一時間選拳';
    aiIcon.textContent = '🤖';
    aiName.textContent = '思考中';
    aiMoveCard.classList.remove('revealed');
  } else if (state.status === 'result') {
    countdownOverlay.hidden = true;
    setMove(aiMoveCard, aiIcon, aiName, state.ai, '🤖');
    gamePanel.classList.remove('result-win', 'result-lose', 'result-draw');
    if (state.result) gamePanel.classList.add('result-' + state.result);
    resultTitle.className = 'result-title ' + (state.result || '');
    resultTitle.textContent = state.message || '';
    resultSub.textContent = state.result === 'win'
      ? '漂亮！這一局是你的'
      : state.result === 'lose'
        ? '再觀察一下手勢，下一局扳回來'
        : '英雄所見略同';
    if (state.result === 'win') bumpScore(playerScore);
    if (state.result === 'lose') bumpScore(aiScore);
  } else if (state.status === 'idle') {
    countdownOverlay.hidden = true;
    if (!state.player) {
      setMove(playerMoveCard, playerIcon, playerName, null, '✊');
      setMove(aiMoveCard, aiIcon, aiName, null, '🤖');
      resultTitle.className = 'result-title';
      resultTitle.textContent = '舉起手，開始下一局';
      resultSub.textContent = state.message || '比出石頭、布或剪刀';
      gamePanel.classList.remove('result-win', 'result-lose', 'result-draw');
    }
  }
}

function bumpScore(element) {
  element.classList.remove('score-bump');
  void element.offsetWidth;
  element.classList.add('score-bump');
}

async function pollExternalGesture() {
  try {
    const response = await fetch('http://127.0.0.1:8090/status', { cache: 'no-store' });
    if (!response.ok) return;
    const status = await response.json();
    const names = { rock: '石頭', paper: '布', scissors: '剪刀' };
    const name = names[status.gesture] || '';
    onGesturePreview({
      label: status.gesture || null,
      label_zh: name,
      move: status.gesture || null,
      move_zh: name,
      confidence: status.gesture ? 1.0 : 0.0,
      stable: status.stable || 0,
      needed: status.needed || 4,
    });
  } catch (error) {
    // The PC preview bridge is restarted independently; keep the UI alive.
  }
}

function onGesturePreview(preview) {
  if (!preview) return;
  const confidence = Math.round((preview.confidence || 0) * 100);
  const label = preview.label_zh || preview.label || '';
  const stable = preview.stable || 0;
  const needed = preview.needed || 4;

  if (label) {
    gestureText.textContent = stable >= needed && preview.move_zh
      ? '鎖定 ' + preview.move_zh
      : '看到 ' + label;
    gestureConfidence.textContent = confidence + '%';
    gestureBadge.classList.add('active');
  } else {
    gestureText.textContent = '請把手放進畫面';
    gestureConfidence.textContent = '—';
    gestureBadge.classList.remove('active', 'locked');
  }

  const progress = preview.move ? Math.min(100, (stable / Math.max(1, needed)) * 100) : 0;
  confidenceFill.style.width = progress + '%';
  confidenceFill.classList.toggle('ready', Boolean(preview.move && stable >= needed));
  gestureBadge.classList.toggle('locked', Boolean(preview.move && stable >= needed));
}

window.addEventListener('beforeunload', () => {
  if (frameSocket) frameSocket.close();
  if (captureTimer) clearInterval(captureTimer);
  if (stream) stream.getTracks().forEach((track) => track.stop());
});

if (externalCameraMode) {
  cameraState.textContent = 'DroidCam 外部鏡頭';
  cameraHelp.textContent = '手機鏡頭由電腦傳送到 UNO Q；請把手放進畫面。';
  fpsValue.textContent = 'DroidCam';
  startCameraButton.hidden = true;
  $('cameraPlaceholder').hidden = true;
  $('cameraPlaceholder').style.display = 'none';
  const previewUrl = query.get('previewUrl') || 'http://127.0.0.1:8090/video';
  externalFeed.src = previewUrl;
  externalFeed.hidden = false;
  setInterval(pollExternalGesture, 250);
  pollExternalGesture();
  updateConnectionPill();
} else {
  updateConnectionPill();
  startCamera();
}

