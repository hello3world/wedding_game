// ================================================================
// СВАДЕБНЫЙ ПЕРЕПОЛОХ — Rock-themed wedding team battle engine
// ================================================================

// --- Load game config (title etc.) ---
(async function loadGameConfig() {
    try {
        const res = await fetch('/api/config');
        if (res.ok) {
            const config = await res.json();
            document.title = config.game_title || 'Свадебный Переполох Касьяновых';
            const el = document.getElementById('game-name');
            if (el && config.game_title) {
                // Split by <br> or word wrap for the rock-style title
                if (config.game_title.includes('<br>')) {
                    el.innerHTML = config.game_title;
                } else {
                    el.innerHTML = config.game_title.replace(/\s+/g, '<br>');
                }
            }
        }
    } catch (err) {
        console.warn('Failed to load game config:', err);
    }
})();

// --- Event throttler (batches score updates every 50ms) ---
class EventThrottler {
    constructor(sendCallback, intervalMs = 50) {
        this.sendCallback = sendCallback;
        this.intervalMs = intervalMs;
        this.accumulatedPoints = 0;
        this.lastFlush = Date.now();
        this.timer = null;
    }
    addPoints(points) {
        this.accumulatedPoints += points;
        const now = Date.now();
        const elapsed = now - this.lastFlush;
        if (elapsed >= this.intervalMs) {
            this.flush();
        } else if (!this.timer) {
            this.timer = setTimeout(() => this.flush(), this.intervalMs - elapsed);
        }
    }
    flush() {
        if (this.accumulatedPoints > 0) {
            this.sendCallback(this.accumulatedPoints);
        } else if (this.accumulatedPoints < 0) {
            this.sendCallback(this.accumulatedPoints);
        }
        this.accumulatedPoints = 0;
        this.lastFlush = Date.now();
        if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    }
}

// --- Procedural audio synthesis ---
class WeddingAudioSynthesizer {
    constructor() { this.context = null; }
    lazyInit() {
        if (!this.context) this.context = new (window.AudioContext || window.webkitAudioContext)();
        if (this.context.state === 'suspended') this.context.resume();
    }
    playCollectSound() {
        this.lazyInit();
        const now = this.context.currentTime;
        const osc = this.context.createOscillator();
        const gain = this.context.createGain();
        osc.type = 'triangle';
        osc.frequency.setValueAtTime(523.25, now);
        osc.frequency.setValueAtTime(659.25, now + 0.06);
        gain.gain.setValueAtTime(0.12, now);
        gain.gain.setTargetAtTime(0.001, now, 0.1);
        osc.connect(gain); gain.connect(this.context.destination);
        osc.start(now); osc.stop(now + 0.25);
    }
    playDamageSound() {
        this.lazyInit();
        const now = this.context.currentTime;
        const osc = this.context.createOscillator();
        const filter = this.context.createBiquadFilter();
        const gain = this.context.createGain();
        osc.type = 'sawtooth';
        osc.frequency.setValueAtTime(180, now);
        osc.frequency.setTargetAtTime(45, now, 0.08);
        filter.type = 'lowpass';
        filter.frequency.setValueAtTime(600, now);
        filter.Q.setValueAtTime(8, now);
        gain.gain.setValueAtTime(0.18, now);
        gain.gain.setTargetAtTime(0.001, now, 0.12);
        osc.connect(filter); filter.connect(gain); gain.connect(this.context.destination);
        osc.start(now); osc.stop(now + 0.25);
    }
}
const audio = new WeddingAudioSynthesizer();

// --- Background music hooks ---
// Drop mp3 files into static/audio/ (game-theme.mp3, results-theme.mp3).
// Tracks are created only for files that exist (see initOptionalAssets).
class MusicManager {
    constructor() {
        this.tracks = {};
    }
    enable(name, src) {
        const a = new Audio(src);
        a.loop = true;
        a.volume = 0.5;
        a.preload = 'auto';
        this.tracks[name] = a;
    }
    play(name) {
        this.stopAll();
        const track = this.tracks[name];
        if (track) track.play().catch(() => { /* autoplay blocked */ });
    }
    stopAll() {
        Object.values(this.tracks).forEach(t => { t.pause(); t.currentTime = 0; });
    }
}
const music = new MusicManager();

// --- WebSocket client with exponential backoff ---
class GameWebSocketClient {
    constructor(url, onMessageCallback, onOpenCallback) {
        this.url = url; this.onMessageCallback = onMessageCallback;
        this.onOpenCallback = onOpenCallback;
        this.ws = null; this.reconnectAttempts = 0; this.maxReconnectInterval = 30000;
        this.heartbeatTimer = null;
    }
    connect() {
        this.ws = new WebSocket(this.url);
        this.ws.onopen = () => {
            this.reconnectAttempts = 0;
            console.log("WebSocket connected");
            // App-level heartbeat: keeps mobile NAT/proxies from dropping the
            // idle socket while everyone gathers in the waiting room
            this.startHeartbeat();
            // Fires on every (re)connect so the client can re-join the room
            if (this.onOpenCallback) this.onOpenCallback();
        };
        this.ws.onmessage = (event) => { this.onMessageCallback(JSON.parse(event.data)); };
        this.ws.onclose = () => { this.stopHeartbeat(); this.scheduleReconnect(); };
        this.ws.onerror = (err) => { console.error("Socket error:", err); this.ws.close(); };
    }
    startHeartbeat() {
        this.stopHeartbeat();
        this.heartbeatTimer = setInterval(() => this.send({ type: "ping" }), 25000);
    }
    stopHeartbeat() {
        if (this.heartbeatTimer) { clearInterval(this.heartbeatTimer); this.heartbeatTimer = null; }
    }
    send(data) { if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(data)); }
    // Manual close (leave room): no auto-reconnect, no re-join
    close() {
        this.stopHeartbeat();
        if (this.ws) {
            this.ws.onclose = null;
            this.ws.onerror = null;
            this.ws.close();
            this.ws = null;
        }
    }
    scheduleReconnect() {
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), this.maxReconnectInterval);
        this.reconnectAttempts++;
        setTimeout(() => this.connect(), delay);
    }
}

// --- Game state ---
const canvas = document.getElementById("game-canvas");
const ctx = canvas.getContext("2d");
let animationId = null;
let gameActive = false;
let currentScore = 0;
let fallingObjects = [];
let localGuestId = "";
let clientSocket = null;
let frozenUntil = 0;
let screenShake = 0;
let catchPopups = [];  // floating emoji indicators near score
let selectedTeam = null;   // 'groom' | 'bride' — chosen in the lobby
let veilPhase = 0;         // animation phase for the bride's veil
let isHost = false;        // the first joined player controls the start
let countdownInterval = null;
let pendingRestoredScore = 0; // score returned by the server after a page refresh

// --- Reload recovery: session survives an accidental page refresh ---
// (sessionStorage dies with the tab, so a new tab always starts fresh)
const SESSION_KEY = 'wedding_session';
function saveSession(data) {
    try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(data)); } catch (e) { }
}
function readSession() {
    try { return JSON.parse(sessionStorage.getItem(SESSION_KEY) || 'null'); } catch (e) { return null; }
}
function clearSession() {
    try { sessionStorage.removeItem(SESSION_KEY); } catch (e) { }
}

// --- Character sprites (pre-rendered: groom & bride) ---
const GROOM_W = 64;
const GROOM_H = 85;
// Supersample the character sprites so custom photo heads stay crisp when the
// sprite is upscaled to device pixels on high-DPR screens (dpr is capped at 3).
const SPRITE_SS = 3;
const groomSprite = document.createElement('canvas');
groomSprite.width = GROOM_W * SPRITE_SS;
groomSprite.height = GROOM_H * SPRITE_SS;
const brideSprite = document.createElement('canvas');
brideSprite.width = GROOM_W * SPRITE_SS;
brideSprite.height = GROOM_H * SPRITE_SS;

// Current displayed size (scaled for mobile)
let groomDisplayW = GROOM_W;
let groomDisplayH = GROOM_H;

// --- Custom PNG heads ---
// Drop images into static/img/ (groom-head.png, bride-head.png) and the
// drawn heads are replaced automatically; missing files keep the drawn ones.
const HEAD_IMAGE_SOURCES = {
    groom: '/static/img/groom-head.png',
    bride: '/static/img/bride-head.png',
};
const customHeads = { groom: null, bride: null };
// Victory splash image URL per winning team (groom-victory.png / bride-victory.png)
const winnerSplash = { groom: null, bride: null };
function preloadWinnerSplash(team, url) {
    winnerSplash[team] = url;
    const img = new Image();   // warm the browser cache before game_over
    img.src = url;
}

function drawHeadImage(g, img, cx, cy, radius) {
    g.save();
    g.imageSmoothingEnabled = true;
    g.imageSmoothingQuality = 'high';
    g.beginPath();
    g.arc(cx, cy, radius, 0, Math.PI * 2);
    g.clip();
    g.drawImage(img, cx - radius, cy - radius, radius * 2, radius * 2);
    g.restore();
}

function loadCustomHead(team) {
    const img = new Image();
    img.onload = () => {
        customHeads[team] = img;
        // Re-render the sprite with the new head
        if (team === 'groom') renderGroomSprite(); else renderBrideSprite();
    };
    img.src = HEAD_IMAGE_SOURCES[team];
}

// Ask the server which optional assets are uploaded and load only those —
// avoids 404 requests for heads/music that are not there yet.
async function initOptionalAssets() {
    try {
        const res = await fetch('/api/assets');
        const assets = await res.json();
        if (assets.groom_head) loadCustomHead('groom');
        if (assets.bride_head) loadCustomHead('bride');
        if (assets.game_theme) music.enable('game', '/static/audio/game-theme.mp3');
        if (assets.results_theme) music.enable('results', '/static/audio/results-theme.mp3');
        if (assets.groom_victory) preloadWinnerSplash('groom', '/static/img/groom-victory.png');
        if (assets.bride_victory) preloadWinnerSplash('bride', '/static/img/bride-victory.png');
    } catch (err) { /* offline / older server — drawn heads, no music */ }
}

function renderGroomSprite() {
    const g = groomSprite.getContext('2d');
    const cx = GROOM_W / 2;
    g.setTransform(SPRITE_SS, 0, 0, SPRITE_SS, 0, 0);
    g.clearRect(0, 0, GROOM_W, GROOM_H);

    // Top hat
    g.fillStyle = '#111';
    g.fillRect(cx - 11, 0, 22, 3);
    const hatGrad = g.createLinearGradient(cx - 10, 3, cx + 10, 17);
    hatGrad.addColorStop(0, '#222'); hatGrad.addColorStop(1, '#111');
    g.fillStyle = hatGrad;
    g.fillRect(cx - 10, 3, 20, 14);
    g.fillStyle = '#cc0000';
    g.fillRect(cx - 10, 15, 20, 3);

    if (customHeads.groom) {
        // Custom PNG head (circular crop)
        drawHeadImage(g, customHeads.groom, cx, 28, 13);
    } else {
        // Head
        g.beginPath();
        g.arc(cx, 28, 11, 0, Math.PI * 2);
        g.fillStyle = '#f0c8a0';
        g.fill();

        // Hair sides
        g.fillStyle = '#222';
        g.fillRect(cx - 12, 21, 3, 9);
        g.fillRect(cx + 9, 21, 3, 9);

        // Eyes
        g.fillStyle = '#222';
        g.beginPath(); g.arc(cx - 4, 27, 1.8, 0, Math.PI * 2); g.fill();
        g.beginPath(); g.arc(cx + 4, 27, 1.8, 0, Math.PI * 2); g.fill();

        // Smile
        g.beginPath();
        g.arc(cx, 31, 4, 0.15 * Math.PI, 0.85 * Math.PI);
        g.strokeStyle = '#8b4513'; g.lineWidth = 1.2; g.stroke();
    }

    // Tuxedo body
    g.beginPath();
    g.moveTo(cx - 16, 40);
    g.lineTo(cx + 16, 40);
    g.lineTo(cx + 19, GROOM_H);
    g.lineTo(cx - 19, GROOM_H);
    g.closePath();
    g.fillStyle = '#1a1a1a';
    g.fill();

    // Shirt
    g.beginPath();
    g.moveTo(cx - 5, 40); g.lineTo(cx + 5, 40);
    g.lineTo(cx + 3, GROOM_H - 4); g.lineTo(cx - 3, GROOM_H - 4);
    g.closePath();
    g.fillStyle = '#f0f0f0';
    g.fill();

    // Bow tie
    g.fillStyle = '#cc0000';
    g.beginPath();
    g.moveTo(cx, 41); g.lineTo(cx - 7, 37); g.lineTo(cx - 7, 45); g.closePath(); g.fill();
    g.beginPath();
    g.moveTo(cx, 41); g.lineTo(cx + 7, 37); g.lineTo(cx + 7, 45); g.closePath(); g.fill();

    // Buttons
    g.fillStyle = '#111';
    for (let i = 0; i < 3; i++) {
        g.beginPath(); g.arc(cx, 52 + i * 10, 1.5, 0, Math.PI * 2); g.fill();
    }
}

// Bride: white dress, veil base, tiara (the flowing veil is animated in the game loop)
function renderBrideSprite() {
    const g = brideSprite.getContext('2d');
    const cx = GROOM_W / 2;
    g.setTransform(SPRITE_SS, 0, 0, SPRITE_SS, 0, 0);
    g.clearRect(0, 0, GROOM_W, GROOM_H);

    // Tiara
    g.fillStyle = '#ffd700';
    g.beginPath();
    g.moveTo(cx - 8, 16); g.lineTo(cx - 4, 10); g.lineTo(cx, 15);
    g.lineTo(cx + 4, 10); g.lineTo(cx + 8, 16);
    g.closePath(); g.fill();

    // Veil base behind the head (semi-transparent white)
    g.fillStyle = 'rgba(255, 255, 255, 0.35)';
    g.beginPath();
    g.ellipse(cx, 30, 16, 20, 0, 0, Math.PI * 2);
    g.fill();

    if (customHeads.bride) {
        // Custom PNG head (circular crop)
        drawHeadImage(g, customHeads.bride, cx, 28, 13);
    } else {
        // Head
        g.beginPath();
        g.arc(cx, 28, 11, 0, Math.PI * 2);
        g.fillStyle = '#f5d0b0';
        g.fill();

        // Hair (brown, framing the face)
        g.fillStyle = '#6b3a1f';
        g.beginPath();
        g.arc(cx, 24, 11.5, Math.PI * 0.95, Math.PI * 2.05);
        g.fill();
        g.fillRect(cx - 12, 22, 3, 12);
        g.fillRect(cx + 9, 22, 3, 12);

        // Eyes with lashes
        g.fillStyle = '#222';
        g.beginPath(); g.arc(cx - 4, 28, 1.8, 0, Math.PI * 2); g.fill();
        g.beginPath(); g.arc(cx + 4, 28, 1.8, 0, Math.PI * 2); g.fill();

        // Smile with lipstick
        g.beginPath();
        g.arc(cx, 32, 4, 0.15 * Math.PI, 0.85 * Math.PI);
        g.strokeStyle = '#cc2255'; g.lineWidth = 1.6; g.stroke();
    }

    // White wedding dress (A-line silhouette)
    g.beginPath();
    g.moveTo(cx - 9, 40);
    g.lineTo(cx + 9, 40);
    g.lineTo(cx + 24, GROOM_H);
    g.lineTo(cx - 24, GROOM_H);
    g.closePath();
    const dressGrad = g.createLinearGradient(0, 40, 0, GROOM_H);
    dressGrad.addColorStop(0, '#ffffff');
    dressGrad.addColorStop(1, '#e8e0f0');
    g.fillStyle = dressGrad;
    g.fill();

    // Bodice details
    g.strokeStyle = 'rgba(200, 180, 220, 0.8)';
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(cx - 7, 48); g.lineTo(cx + 7, 48); g.stroke();

    // Bouquet
    g.fillStyle = '#e75480';
    g.beginPath(); g.arc(cx - 12, 55, 4, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#ff8fab';
    g.beginPath(); g.arc(cx - 15, 52, 3, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#2d8a4e';
    g.fillRect(cx - 13, 58, 2, 6);
}

renderGroomSprite();
renderBrideSprite();
initOptionalAssets();

const groom = { x: 0, y: 0 };

// --- Item data (shared bonuses; freeze items depend on the team) ---
const POSITIVE_ITEMS = [
    { emoji: '💍', points: 25, label: 'Украшения' },
    { emoji: '🎁', points: 20, label: 'Подарки' },
    { emoji: '🥂', points: 15, label: 'Веселье' },
    { emoji: '✈️', points: 15, label: 'Путешествие' },
    { emoji: '🥞', points: 15, label: 'Тёщины блины' },
    { emoji: '🍸', points: 10, label: 'Коктейль' },
    { emoji: '🏖️', points: 15, label: 'Пляжный отдых' },
    { emoji: '💐', points: 10, label: 'Цветы' },
    { emoji: '❤️', points: 10, label: 'Любовь' },
];
// Common penalties for both teams (weight < 1 = spawns less often)
const NEGATIVE_ITEMS_COMMON = [
    { emoji: '🧹', points: -15, label: 'Уборка' },
    { emoji: '🧦', points: -15, label: 'Грязные носки' },
    { emoji: '🍽️', points: -10, label: 'Немытая посуда' },
    { emoji: '💼', points: -20, label: 'Работа допоздна' },
    { emoji: '🎤', points: -15, label: 'Тост на 20 минут', freeze: 1.0 },
    { emoji: '💢', points: -30, label: 'Ссоры', weight: 0.3 },
];
// Team-specific freeze hazards: catching one freezes the player for 1 second.
// Groom dreads working in his father-in-law's garden; bride — a shopping trip.
const NEGATIVE_ITEMS_GROOM = [
    { emoji: '🥔', points: -25, label: 'Работать на огороде у тестя', freeze: 1.0 },
];
const NEGATIVE_ITEMS_BRIDE = [
    { emoji: '🛒', points: -25, label: 'Поход по магазинам', freeze: 1.0 },
];

// Active item pool, rebuilt when the team is chosen
let ALL_ITEMS = [...POSITIVE_ITEMS, ...NEGATIVE_ITEMS_COMMON, ...NEGATIVE_ITEMS_GROOM];
function rebuildItemPool() {
    const teamNegatives = selectedTeam === 'bride' ? NEGATIVE_ITEMS_BRIDE : NEGATIVE_ITEMS_GROOM;
    ALL_ITEMS = [...POSITIVE_ITEMS, ...NEGATIVE_ITEMS_COMMON, ...teamNegatives];
}

// Weighted pick: items with weight < 1 (e.g. Ссоры) spawn less often
function pickItemTemplate() {
    const total = ALL_ITEMS.reduce((sum, it) => sum + (it.weight ?? 1), 0);
    let rnd = Math.random() * total;
    for (const it of ALL_ITEMS) {
        rnd -= (it.weight ?? 1);
        if (rnd <= 0) return it;
    }
    return ALL_ITEMS[ALL_ITEMS.length - 1];
}

// --- Score throttler ---
const networkThrottler = new EventThrottler((points) => {
    if (clientSocket) clientSocket.send({ type: "score_increment", points: points });
}, 50);

// --- Canvas setup (with devicePixelRatio for crisp rendering on retina) ---
let viewW = window.innerWidth;
let viewH = window.innerHeight;
function setupCanvasDimensions() {
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    viewW = window.innerWidth;
    viewH = window.innerHeight;
    canvas.width = Math.round(viewW * dpr);
    canvas.height = Math.round(viewH * dpr);
    canvas.style.width = viewW + 'px';
    canvas.style.height = viewH + 'px';
    // All drawing code keeps using CSS pixels
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingQuality = 'high';

    // Scale character for mobile: smaller on narrow screens, keep proportions
    const baseW = 64;
    const scale = Math.min(1.0, viewW / 420);
    groomDisplayW = Math.round(baseW * scale);
    groomDisplayH = Math.round(GROOM_H * scale);

    groom.y = viewH - groomDisplayH - 60 - envSafeInsetBottom();
    groom.x = (viewW - groomDisplayW) / 2;
}
function envSafeInsetBottom() {
    return parseInt(getComputedStyle(document.documentElement).getPropertyValue('--safe-area-inset-bottom') || '0', 10);
}
window.addEventListener("resize", setupCanvasDimensions);

// --- Emoji sprite cache ---
// fillText emoji rendering is inconsistent across platforms: iOS lies in
// measureText for VS16 emoji (✈️ ❤️ 🍽️) — it measures the text glyph but
// draws a color glyph of a different size, so the emoji overflowed circles.
// Fix: render into an offscreen canvas, PIXEL-SCAN the alpha channel to get
// the true ink box, then center/scale that box — immune to font metric lies.
const emojiSpriteCache = {};
function getEmojiSprite(emoji, size) {
    const key = `${emoji}_${size}`;
    if (emojiSpriteCache[key]) return emojiSpriteCache[key];
    const ratio = Math.min(window.devicePixelRatio || 1, 3);
    const fontPx = Math.round(size * ratio);
    // Generous padding: iOS draws some glyphs far outside the em box
    const pad = fontPx;
    const dim = fontPx + pad * 2;
    const off = document.createElement('canvas');
    off.width = dim;
    off.height = dim;
    const g = off.getContext('2d', { willReadFrequently: true });
    g.font = `${fontPx}px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif`;
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText(emoji, dim / 2, dim / 2);
    // Find the real drawn bounds (alpha > threshold)
    const data = g.getImageData(0, 0, dim, dim).data;
    let minX = dim, minY = dim, maxX = -1, maxY = -1;
    for (let y = 0; y < dim; y++) {
        for (let x = 0; x < dim; x++) {
            if (data[(y * dim + x) * 4 + 3] > 8) {
                if (x < minX) minX = x;
                if (x > maxX) maxX = x;
                if (y < minY) minY = y;
                if (y > maxY) maxY = y;
            }
        }
    }
    if (maxX < 0) { minX = 0; minY = 0; maxX = dim - 1; maxY = dim - 1; }
    const sprite = {
        canvas: off, ratio,
        bx: minX, by: minY, bw: maxX - minX + 1, bh: maxY - minY + 1,
    };
    emojiSpriteCache[key] = sprite;
    return sprite;
}
// Draw the emoji's real ink box centered at (cx, cy); if maxDiameter is set,
// shrink so the ink box always fits inside (never overflows the circle)
function drawEmojiCentered(targetCtx, emoji, size, cx, cy, maxDiameter) {
    const s = getEmojiSprite(emoji, size);
    let drawW = s.bw / s.ratio;
    let drawH = s.bh / s.ratio;
    const limit = maxDiameter ?? size;
    const maxInk = Math.max(drawW, drawH);
    if (maxInk > limit) {
        const k = limit / maxInk;
        drawW *= k; drawH *= k;
    }
    targetCtx.drawImage(s.canvas, s.bx, s.by, s.bw, s.bh,
        cx - drawW / 2, cy - drawH / 2, drawW, drawH);
}
// Item icon: emoji drawn centered inside the circle
function drawItemIcon(targetCtx, item, size, cx, cy, maxDiameter) {
    drawEmojiCentered(targetCtx, item.emoji, size, cx, cy, maxDiameter);
}

// --- Touch controls ---
canvas.addEventListener("touchmove", (e) => {
    e.preventDefault();
    if (!gameActive) return;
    if (Date.now() < frozenUntil) return;  // frozen — no movement at all
    const touchX = e.touches[0].clientX;
    groom.x = touchX - groomDisplayW / 2;
    groom.x = Math.max(0, Math.min(groom.x, viewW - groomDisplayW));
}, { passive: false });

// --- Mouse controls (desktop testing) ---
canvas.addEventListener("mousemove", (e) => {
    if (!gameActive) return;
    if (Date.now() < frozenUntil) return;  // frozen — no movement at all
    groom.x = e.clientX - groomDisplayW / 2;
    groom.x = Math.max(0, Math.min(groom.x, viewW - groomDisplayW));
});

// --- Keyboard controls (arrow keys + A/D) ---
const keysPressed = {};
// e.code is the physical key, not the typed character: on a Russian layout
// the A/D keys are the letters Ф/В. While the player is typing a name we must
// not preventDefault() those keystrokes, or part of the name never appears.
function isTypingInField(target) {
    return !!target && (
        target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable
    );
}
window.addEventListener("keydown", (e) => {
    if (isTypingInField(e.target)) return;
    if (['ArrowLeft', 'ArrowRight', 'KeyA', 'KeyD'].includes(e.code)) {
        e.preventDefault();
        keysPressed[e.code] = true;
    }
});
window.addEventListener("keyup", (e) => {
    keysPressed[e.code] = false;
});
// keyup never fires if the key is released outside the window — clear stuck keys
window.addEventListener("blur", () => {
    for (const code in keysPressed) keysPressed[code] = false;
});

// --- Game loop with Delta Time ---
let prevTimestamp = 0;
function runRenderLoop(timestamp) {
    if (!gameActive) return;

    const delta = (timestamp - prevTimestamp) / 1000;
    prevTimestamp = timestamp;
    const isFrozen = Date.now() < frozenUntil;

    // Apply keyboard movement
    if (!isFrozen) {
        const moveSpeed = 500 * delta;
        if (keysPressed['ArrowLeft'] || keysPressed['KeyA']) groom.x -= moveSpeed;
        if (keysPressed['ArrowRight'] || keysPressed['KeyD']) groom.x += moveSpeed;
        groom.x = Math.max(0, Math.min(groom.x, viewW - groomDisplayW));
    }

    // Apply screen shake
    ctx.save();
    if (screenShake > 0) {
        ctx.translate(
            (Math.random() - 0.5) * screenShake * 8,
            (Math.random() - 0.5) * screenShake * 8
        );
        screenShake = Math.max(0, screenShake - delta * 5);
    }

    // Clear with pleasant dark gradient background
    const bgGrad = ctx.createLinearGradient(0, 0, 0, viewH);
    bgGrad.addColorStop(0, '#1a1a3e');
    bgGrad.addColorStop(0.5, '#16213e');
    bgGrad.addColorStop(1, '#0f1123');
    ctx.fillStyle = bgGrad;
    ctx.fillRect(-10, -10, viewW + 20, viewH + 20);

    // Subtle stage floor line
    ctx.fillStyle = 'rgba(100, 80, 200, 0.12)';
    ctx.fillRect(0, viewH - 40 - envSafeInsetBottom(), viewW, 1);

    // Draw active character (groom or bride), dimmed when frozen
    const activeSprite = selectedTeam === 'bride' ? brideSprite : groomSprite;
    ctx.globalAlpha = isFrozen ? 0.4 : 1.0;

    // Animated flowing veil behind the bride (spiral wave)
    if (selectedTeam === 'bride') {
        veilPhase += delta * 3;
        const scale = groomDisplayW / GROOM_W;
        const headX = groom.x + groomDisplayW / 2;
        const headY = groom.y + 26 * scale;
        ctx.save();
        ctx.globalAlpha = (isFrozen ? 0.25 : 0.55);
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 6 * scale;
        ctx.lineCap = 'round';
        // Three wavy strands drifting behind the head
        for (let s = 0; s < 3; s++) {
            ctx.beginPath();
            ctx.moveTo(headX, headY);
            for (let t = 0; t <= 1; t += 0.1) {
                const px = headX - t * 26 * scale * (1 + s * 0.3)
                    - Math.sin(veilPhase + s) * 6 * scale * t;
                const py = headY + t * 40 * scale
                    + Math.sin(veilPhase * 1.4 + t * 6 + s * 2) * 5 * scale * t;
                ctx.lineTo(px, py);
            }
            ctx.stroke();
        }
        ctx.restore();
        ctx.globalAlpha = isFrozen ? 0.4 : 1.0;
    }

    ctx.drawImage(activeSprite, groom.x, groom.y, groomDisplayW, groomDisplayH);

    // Freeze ice overlay
    if (isFrozen) {
        ctx.globalAlpha = 0.25;
        ctx.fillStyle = '#4488ff';
        ctx.fillRect(groom.x - 2, groom.y - 2, groomDisplayW + 4, groomDisplayH + 4);
        ctx.globalAlpha = 0.8;
        ctx.font = `${Math.round(14 * (groomDisplayW / GROOM_W))}px Oswald`;
        ctx.fillStyle = '#88ccff';
        ctx.textAlign = 'center';
        ctx.fillText('❄️ FREEZE', groom.x + groomDisplayW / 2, groom.y - 8);
    }
    ctx.globalAlpha = 1.0;

    // --- Render catch popups (floating emoji + points near score) ---
    const now = Date.now();
    for (let p = catchPopups.length - 1; p >= 0; p--) {
        const popup = catchPopups[p];
        const age = now - popup.born;
        if (age > popup.life) { catchPopups.splice(p, 1); continue; }
        const progress = age / popup.life;
        const alpha = 1 - progress;
        const offsetY = progress * 50;
        ctx.globalAlpha = alpha;
        ctx.shadowBlur = 0;
        drawItemIcon(ctx, popup, 28, popup.x, popup.y - offsetY, 30);
        ctx.font = 'bold 16px Oswald';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = popup.positive ? '#ffd700' : '#ff4444';
        const sign = popup.points > 0 ? '+' : '';
        ctx.fillText(`${sign}${popup.points}`, popup.x, popup.y - offsetY + 22);
    }
    ctx.globalAlpha = 1.0;

    // Spawn falling objects
    if (Math.random() < 0.04) {
        const template = pickItemTemplate();
        const isPositive = POSITIVE_ITEMS.includes(template);
        fallingObjects.push({
            x: 15 + Math.random() * (viewW - 30),
            y: -25,
            size: 28,
            speed: 160 + Math.random() * 120,
            positive: isPositive,
            emoji: template.emoji,
            points: template.points,
            freeze: template.freeze || 0,
        });
    }

    // Update falling objects
    for (let i = fallingObjects.length - 1; i >= 0; i--) {
        const obj = fallingObjects[i];
        obj.y += obj.speed * delta;

        // Collision detection (circle vs scaled rectangle)
        const objCenterX = obj.x + obj.size / 2;
        const objCenterY = obj.y + obj.size / 2;
        const closestX = Math.max(groom.x, Math.min(objCenterX, groom.x + groomDisplayW));
        const closestY = Math.max(groom.y, Math.min(objCenterY, groom.y + groomDisplayH));
        const dx = objCenterX - closestX;
        const dy = objCenterY - closestY;

        if ((dx * dx + dy * dy) < (obj.size * 0.45) * (obj.size * 0.45)) {
            if (obj.positive) {
                currentScore += obj.points;
                audio.playCollectSound();
                networkThrottler.addPoints(obj.points);
            } else {
                currentScore = Math.max(0, currentScore + obj.points);
                audio.playDamageSound();
                networkThrottler.addPoints(obj.points);
                screenShake = 1.0;
                if (obj.freeze > 0) frozenUntil = Date.now() + obj.freeze * 1000;
            }
            // Spawn floating catch indicator
            catchPopups.push({
                emoji: obj.emoji,
                points: obj.points,
                positive: obj.positive,
                x: viewW - 80,
                y: 70,
                born: Date.now(),
                life: 1200,
            });
            document.getElementById("hud-score").innerText = currentScore;
            fallingObjects.splice(i, 1);
            continue;
        }

        if (obj.y > viewH + 30) {
            fallingObjects.splice(i, 1);
            continue;
        }

        // Render falling item — background circle BEHIND emoji
        const cx = obj.x + obj.size / 2;
        const cy = obj.y + obj.size / 2;
        const r = obj.size * 0.72;

        // 1. Draw solid light backdrop (behind emoji, fully opaque)
        ctx.shadowBlur = 0;
        ctx.globalAlpha = 1.0;
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.fillStyle = obj.positive ? '#ffffff' : '#ffcccc';
        ctx.fill();

        // 2. Draw colored border ring
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.strokeStyle = obj.positive ? '#ffd700' : '#ff4444';
        ctx.lineWidth = 2.5;
        ctx.stroke();

        // 3. Draw icon ON TOP of the white backdrop: uploaded photo face for
        // freeze characters, otherwise metric-centered emoji. Ink box capped
        // at 88% of the circle so wide Apple glyphs never overflow on iPhone.
        ctx.globalAlpha = 1.0;
        ctx.shadowBlur = 0;
        drawItemIcon(ctx, obj, obj.size + 8, cx, cy, r * 2 * 0.88);
    }

    ctx.restore();
    animationId = requestAnimationFrame(runRenderLoop);
}

// --- WebSocket message handler ---
function handleIncomingWSMessage(msg) {
    switch (msg.type) {
        case "room_joined":
            localGuestId = msg.guest_id;
            isHost = msg.is_host === true;
            // Server restores the score after a page refresh (0 for new players);
            // applied in game_start, which fires right after for active rooms
            pendingRestoredScore = msg.score ?? 0;
            saveSession({ guest_id: msg.guest_id, nickname: msg.nickname, team: msg.team });
            document.getElementById("lobby-screen").classList.add("hidden");
            // Waiting room first; the game screen appears on game_start
            document.getElementById("waiting-screen").classList.remove("hidden");
            updateHostControls();
            setupCanvasDimensions();
            break;
        case "room_full":
            alert(`Комната заполнена (максимум ${msg.max_players} игроков). Попробуйте позже.`);
            break;
        case "lobby_update":
            renderWaitingRoom(msg);
            break;
        case "countdown":
            startCountdown(msg.seconds);
            break;
        case "game_start":
            stopCountdown();
            document.getElementById("waiting-screen").classList.add("hidden");
            document.getElementById("countdown-overlay").classList.add("hidden");
            document.getElementById("game-screen").classList.remove("hidden");
            currentScore = pendingRestoredScore;
            pendingRestoredScore = 0;
            fallingObjects = [];
            frozenUntil = 0;
            screenShake = 0;
            catchPopups = [];
            document.getElementById("hud-score").innerText = String(currentScore);
            // Late joiners get the remaining time, not the full round
            document.getElementById("hud-timer").innerText = `${Math.ceil(msg.time_left ?? msg.duration)}s`;
            gameActive = true;
            prevTimestamp = performance.now();
            audio.lazyInit();
            music.play('game');
            setupCanvasDimensions();
            animationId = requestAnimationFrame(runRenderLoop);
            break;
        case "leaderboard_update":
            document.getElementById("hud-timer").innerText = `${msg.time_left}s`;
            // Live team battle score in the HUD
            document.getElementById("hud-groom-total").innerText = msg.total_groom_score ?? 0;
            document.getElementById("hud-bride-total").innerText = msg.total_bride_score ?? 0;
            break;
        case "start_denied":
            // Roster check failed: need 2+ players and both teams
            alert(msg.reason || "Нельзя начать игру.");
            break;
        case "game_over":
            gameActive = false;
            cancelAnimationFrame(animationId);
            clearSession(); // battle finished — nothing to restore anymore
            music.play('results');
            showWinnerSplashThenResults(msg);
            break;
    }
}

// --- Waiting room rendering ---
function renderWaitingRoom(msg) {
    const players = msg.players || [];
    document.getElementById("waiting-count").innerText =
        `Игроков: ${players.length} / ${msg.max_players ?? 30}`;
    const list = document.getElementById("waiting-players");
    // Names only — teams are intentionally hidden until the battle starts
    list.innerHTML = players.map(name =>
        `<div class="waiting-player"><span class="dot"></span>${escapeHtml(name)}</div>`
    ).join('') || '<div class="waiting-player" style="color:#666">Пока никого...</div>';
    // Host may change if the previous one dropped
    isHost = msg.host_id === localGuestId;
    updateHostControls();
}

function updateHostControls() {
    document.getElementById("start-game-btn").classList.toggle("hidden", !isHost);
    document.getElementById("waiting-hint").innerText = isHost
        ? "Вы — ведущий. Нажмите «Начать игру», когда все зайдут!"
        : "Ждём, когда ведущий начнёт игру...";
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// --- Countdown overlay (client-side animation, server triggers the start) ---
function startCountdown(seconds) {
    stopCountdown();
    const overlay = document.getElementById("countdown-overlay");
    const numberEl = document.getElementById("countdown-number");
    let remaining = Math.ceil(seconds);
    if (remaining <= 0) return;
    overlay.classList.remove("hidden");
    numberEl.innerText = remaining;
    countdownInterval = setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
            numberEl.innerText = "GO!";
            stopCountdown();
        } else {
            numberEl.innerText = remaining;
        }
    }, 1000);
}
function stopCountdown() {
    if (countdownInterval) { clearInterval(countdownInterval); countdownInterval = null; }
}

// Show the winning team's victory image full-screen, then reveal the detailed
// results — only when the user taps. Falls back straight to the results on a
// tie or when the winner image is not uploaded.
function showWinnerSplashThenResults(msg) {
    const groomTotal = msg.total_groom_score ?? 0;
    const brideTotal = msg.total_bride_score ?? 0;
    const winner = groomTotal > brideTotal ? 'groom'
        : brideTotal > groomTotal ? 'bride' : null;
    const src = winner ? winnerSplash[winner] : null;
    if (!src) { showTeamResults(msg); return; }  // tie or image missing

    const splash = document.getElementById("winner-splash");
    document.getElementById("winner-splash-img").src = src;
    document.getElementById("game-screen").classList.add("hidden");
    splash.classList.remove("hidden");

    let done = false;
    const proceed = () => {
        if (done) return;
        done = true;
        splash.removeEventListener("click", proceed);
        splash.classList.add("hidden");
        showTeamResults(msg);
    };
    splash.addEventListener("click", proceed);
}

// --- Team results display (two-column battle screen) ---
function showTeamResults(msg) {
    document.getElementById("game-screen").classList.add("hidden");
    document.getElementById("leaderboard-screen").classList.remove("hidden");

    const groomTotal = msg.total_groom_score ?? 0;
    const brideTotal = msg.total_bride_score ?? 0;

    // Big battle score: winner gets a pulsing gold frame
    const groomWin = groomTotal > brideTotal;
    const brideWin = brideTotal > groomTotal;
    document.getElementById("team-battle-score").innerHTML =
        `<span class="side groom${groomWin ? ' winner' : ''}">🤵 ЖЕНИХ ${groomTotal}</span>` +
        `<span class="vs-label">VS</span>` +
        `<span class="side bride${brideWin ? ' winner' : ''}">${brideTotal} НЕВЕСТА 👰</span>`;

    // Two parallel columns with each team's best players
    const myNick = document.getElementById("nickname-input").value.trim();
    fillTeamColumn("groom-col", "🤵 За Жениха", "groom", msg.top_groom_players || [], myNick);
    fillTeamColumn("bride-col", "👰 За Невесту", "bride", msg.top_bride_players || [], myNick);
}

function fillTeamColumn(colId, title, teamClass, players, myNick) {
    const col = document.getElementById(colId);
    col.innerHTML = `<div class="team-col-title ${teamClass}">${title}</div>`;
    if (players.length === 0) {
        col.innerHTML += '<div class="leaderboard-row"><span style="color:#666">—</span></div>';
        return;
    }
    players.forEach((item, index) => {
        const row = document.createElement("div");
        row.className = "leaderboard-row";
        if (item.nickname === myNick) row.className += " highlight";
        row.innerHTML = `<span>${index + 1}. ${item.nickname}</span><span>${item.score}</span>`;
        col.appendChild(row);
    });
}

// --- UI event bindings ---
// Team selection: mandatory before joining
const joinBtn = document.getElementById("join-btn");
const teamGroomBtn = document.getElementById("team-groom-btn");
const teamBrideBtn = document.getElementById("team-bride-btn");

function selectTeam(team) {
    selectedTeam = team;
    teamGroomBtn.classList.toggle("selected-groom", team === "groom");
    teamBrideBtn.classList.toggle("selected-bride", team === "bride");
    rebuildItemPool();
    joinBtn.disabled = false;
}
teamGroomBtn.addEventListener("click", () => selectTeam("groom"));
teamBrideBtn.addEventListener("click", () => selectTeam("bride"));

joinBtn.addEventListener("click", () => {
    const nick = document.getElementById("nickname-input").value.trim();
    if (!selectedTeam) {
        alert("Выберите команду!");
        return;
    }
    if (nick) {
        audio.lazyInit();
        connectAndJoin(nick);
    } else {
        alert("Введите имя!");
    }
});

function connectAndJoin(nick) {
    const wsProtocol = window.location.protocol === "https:" ? "wss://" : "ws://";
    const wsUrl = `${wsProtocol}${window.location.host}/ws`;
    // Single shared room — no room codes, everyone plays together.
    // join_room is sent on every (re)connect, so after a server restart
    // the player automatically re-enters the room instead of hanging.
    clientSocket = new GameWebSocketClient(wsUrl, handleIncomingWSMessage, () => {
        const joinPayload = { type: "join_room", room_code: "MAIN", nickname: nick, team: selectedTeam };
        // Ask the server to restore our identity & score after a refresh
        const saved = readSession();
        if (saved && saved.guest_id) joinPayload.resume_id = saved.guest_id;
        clientSocket.send(joinPayload);
    });
    clientSocket.connect();
}

document.getElementById("lobby-btn").addEventListener("click", () => {
    music.stopAll();
    document.getElementById("leaderboard-screen").classList.add("hidden");
    document.getElementById("lobby-screen").classList.remove("hidden");
});

// Host-only: launch the 5-second countdown for everyone
document.getElementById("start-game-btn").addEventListener("click", () => {
    if (clientSocket) clientSocket.send({ type: "start_game" });
});

// Leave the waiting room: close the socket (server drops us from the roster
// and reassigns the host if needed) and go back to the lobby form
document.getElementById("waiting-back-btn").addEventListener("click", () => {
    if (clientSocket) { clientSocket.close(); clientSocket = null; }
    clearSession(); // deliberate leave — don't auto-rejoin on refresh
    stopCountdown();
    isHost = false;
    document.getElementById("waiting-screen").classList.add("hidden");
    document.getElementById("countdown-overlay").classList.add("hidden");
    document.getElementById("lobby-screen").classList.remove("hidden");
});

// In-game exit (top-left): confirm, then end the battle for THIS player only.
// Closes the socket (server drops us from the roster), so the game continues
// for everyone else; we return to the lobby form.
document.getElementById("exit-btn").addEventListener("click", () => {
    if (!confirm("Выйти из игры? Для вас игра завершится.")) return;
    gameActive = false;
    cancelAnimationFrame(animationId);
    if (clientSocket) { clientSocket.close(); clientSocket = null; }
    clearSession(); // deliberate leave — don't auto-rejoin on refresh
    stopCountdown();
    music.stopAll();
    isHost = false;
    document.getElementById("game-screen").classList.add("hidden");
    document.getElementById("countdown-overlay").classList.add("hidden");
    document.getElementById("lobby-screen").classList.remove("hidden");
});

// Rules modal toggle
document.getElementById("rules-btn").addEventListener("click", () => {
    document.getElementById("rules-modal").classList.remove("hidden");
});
document.getElementById("close-rules-btn").addEventListener("click", () => {
    document.getElementById("rules-modal").classList.add("hidden");
});

// Battle results tab: full battle history, newest first, scrollable;
// each battle card expands to show the top players
document.getElementById("results-btn").addEventListener("click", async () => {
    const body = document.getElementById("results-modal-body");
    body.innerHTML = '<p style="color:#888;text-align:center">Загрузка...</p>';
    document.getElementById("results-modal").classList.remove("hidden");
    try {
        const res = await fetch("/api/results/history");
        const data = await res.json();
        const battles = data.battles || [];
        if (!battles.length) {
            body.innerHTML = '<p style="color:#888;text-align:center">Пока нет завершённых батлов.<br>Сыграйте первый!</p>';
            return;
        }
        const listRows = (players) => (players || []).map((p, i) =>
            `<div class="rules-item"><span class="r-emoji">${i + 1}.</span><span class="r-name">${escapeHtml(p.nickname)}</span><span class="r-effect r-positive">${p.score}</span></div>`
        ).join('') || '<div class="rules-item"><span class="r-name" style="color:#666">—</span></div>';
        body.innerHTML = battles.map((bt, idx) => {
            const g = bt.total_groom_score ?? 0;
            const b = bt.total_bride_score ?? 0;
            let winnerLine;
            if (g > b) winnerLine = '🏆 Победа команды Жениха 🤵';
            else if (b > g) winnerLine = '🏆 Победа команды Невесты 👰';
            else winnerLine = '🤝 Ничья!';
            const num = battles.length - idx; // battle #1 is the oldest
            const when = bt.finished_at
                ? new Date(bt.finished_at * 1000).toLocaleString('ru-RU', {
                    day: '2-digit', month: '2-digit',
                    hour: '2-digit', minute: '2-digit',
                })
                : '';
            return `
            <details class="battle-card"${idx === 0 ? ' open' : ''}>
                <summary>
                    <div style="display:flex;justify-content:space-between;align-items:center">
                        <span style="color:#888;font-size:12px">Батл #${num}${when ? ' • ' + when : ''}</span>
                        <span style="color:#ffd700;font-size:12px">${idx === 0 ? 'последний' : ''}</span>
                    </div>
                    <div style="text-align:center;font-size:14px;color:#ffd700;margin:6px 0 2px">${winnerLine}</div>
                    <div style="text-align:center;font-family:'UnifrakturCook',serif;font-size:22px">
                        <span style="color:#ff4444">🤵 ${g}</span>
                        <span style="color:#888;font-size:14px"> VS </span>
                        <span style="color:#ff9ecb">${b} 👰</span>
                    </div>
                    <div style="text-align:center;color:#666;font-size:11px;margin-top:4px">▾ топ игроков</div>
                </summary>
                <div class="rules-section" style="margin-top:10px"><h3>🤵 Команда Жениха</h3>${listRows(bt.top_groom_players)}</div>
                <div class="rules-section"><h3>👰 Команда Невесты</h3>${listRows(bt.top_bride_players)}</div>
            </details>`;
        }).join('');
    } catch (err) {
        body.innerHTML = '<p style="color:#ff4444;text-align:center">Ошибка загрузки результатов</p>';
    }
});
document.getElementById("close-results-btn").addEventListener("click", () => {
    document.getElementById("results-modal").classList.add("hidden");
});

// Reset stored battle results — password-protected on the server
document.getElementById("reset-results-btn").addEventListener("click", async () => {
    const password = prompt("Введите пароль для обнуления результатов:");
    if (password === null) return;
    try {
        const res = await fetch("/api/results/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password }),
        });
        const data = await res.json();
        if (data.ok) {
            alert("Результаты обнулены!");
            document.getElementById("results-modal-body").innerHTML =
                '<p style="color:#888;text-align:center">Пока нет завершённых батлов.<br>Сыграйте первый!</p>';
        } else {
            alert(data.error || "Неверный пароль");
        }
    } catch (err) {
        alert("Ошибка сети, попробуйте ещё раз");
    }
});

// --- Reload recovery: auto-rejoin after an accidental page refresh ---
// If a session exists (tab was refreshed mid-game or in the waiting room),
// restore the name/team and reconnect right away. The server reattaches the
// old identity via resume_id, so the score and the game continue as-is.
(function tryResumeSession() {
    const saved = readSession();
    if (!saved || !saved.guest_id || !saved.nickname) return;
    document.getElementById("nickname-input").value = saved.nickname;
    selectTeam(saved.team === 'bride' ? 'bride' : 'groom');
    connectAndJoin(saved.nickname);
})();
