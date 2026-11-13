'use strict';
// =================================================================
// DeeBeelkin — Wheel of Films  (v3.0 rewrite)
// =================================================================
// Architecture:
//   * Server is the single source of truth for spin results.
//     The server picks winner_idx up-front via wheel_physics.generate_spin
//     and ships the full physics blob back in the HTTP response
//     AND broadcasts it via WS spin_started (so viewers see the same spin).
//
//   * Client state machine:
//       IDLE -> SPINNING -> REVEALING -> IDLE
//     In SPINNING/REVEALING the canvas is rendered from a *snapshot*
//     of items that came with the physics blob, so live wheel_updated
//     events (which remove the winner from the sidebar list) do not
//     corrupt the spinning canvas.
//
//   * The sidebar list reflects live items at all times — when a
//     spin completes, the winner disappears from the sidebar list
//     as soon as the server broadcasts wheel_updated (or the polling
//     fallback fetches /api/wheel/state).
//
//   * WS is wrapped in try/catch on every message. The server may
//     close with custom application-level codes:
//       4001 = invalid guild — DO NOT reconnect (was the source of
//              the infinite-reconnect storm before v3.0)
//       4002 = server error — reconnect with capped exponential
//              backoff (1s, 2s, 4s, 8s, 15s, 15s, ...)
//
//   * Polling fallback: every 3s, fetch /api/wheel/state. This is
//     a safety net for when WS is dead (proxy killed the connection,
//     mobile network dropped, etc.). Items list + active spin state
//     always reflect the server.
//
//   * Winner card: a prominent card replaces the old text-only banner.
//     Shows the winning film's name + poster (if tmdb_id is set) +
//     "Смотреть на Кинопоиске" link. Stays visible until the user
//     dismisses it or starts another spin.
// =================================================================

// === Phases ===
const Phase = Object.freeze({
  IDLE: 'idle',           // ready, can spin
  SPINNING: 'spinning',    // animation in progress
  REVEALING: 'revealing', // animation done, winner card shown
});

// === State (module-private) ===
let phase = Phase.IDLE;
let items = [];              // live items (sidebar source-of-truth)
let spinSnapshot = null;     // items snapshot taken at spin start (canvas source during spin)
let spinPhysics = null;      // current spin physics object (from server)
let spinMode = 'classic';    // 'classic' | 'elimination' | 'elimination_final'
let currentRotation = 0;     // canvas rotation (radians)
let uiMode = 'normal';       // 'normal' | 'elimination' (UI selection, not the spin mode)
let spinDuration = 6000;     // ms — taken from duration slider
let audioCtx = null;
let tickVolume = 0.4;
let soundEnabled = true;
let lastSectorUnderPointer = -1;
let highlightIdx = -1;

let bannerTimer = null;
let progressTimer = null;
let pollTimer = null;
let postSpinFallbackTimer = null;

// WebSocket
let ws = null;
let wsPingTimer = null;
let wsReconnectAttempts = 0;
let wsGaveUp = false;       // server said 4001 — stop trying
const WS_MAX_RECONNECT_DELAY = 15000;

// Cache for posters: tmdb_id -> poster URL. Avoids re-fetching the
// same movie lookup every spin.
const _posterCache = new Map();

// v3.7.0: Cache for GIF-poster Image objects (для drawImage на секторе).
// Key: poster URL. Value: HTMLImageElement (или null если ещё грузится/ошибка).
// Анимированный GIF НЕ анимируется в canvas через drawImage — рисуем первый
// кадр (PNG-постер, подготовленный сервером через Pillow). Сам GIF виден
// в sidebar через <img>, где он анимируется штатно.
const _sectorImageCache = new Map();
let _sectorImagesPending = 0;  // сколько Image ещё грузятся — для триггера redraw

function _getSectorImage(url) {
  if (!url) return null;
  if (_sectorImageCache.has(url)) {
    return _sectorImageCache.get(url);
  }
  // Новый — создаём и начинаем загрузку
  const img = new Image();
  img.crossOrigin = "anonymous";  // на случай если CDN
  _sectorImageCache.set(url, null);  // временно null — грузится
  _sectorImagesPending++;
  img.onload = () => {
    _sectorImageCache.set(url, img);
    _sectorImagesPending--;
    // Перерисуем когда все загрузились — иначе можно получить дёрганье
    // при каждом onload. Если в данный момент IDLE — redraw безопасен.
    if (_sectorImagesPending === 0 && phase === Phase.IDLE) {
      drawWheel(currentRotation);
    }
  };
  img.onerror = () => {
    _sectorImageCache.set(url, null);
    _sectorImagesPending--;
    // null в кеше → fallback на цвет
  };
  img.src = url;
  return null;
}

// v3.7.0: prewarm кеша для всех текущих items (вызывается при получении items).
function _prewarmSectorImages(itemsArr) {
  if (!itemsArr || itemsArr.length === 0) return;
  itemsArr.forEach((it) => {
    if (it.gif_poster_path) _getSectorImage(it.gif_poster_path);
  });
}

// v3.7.0: единый helper для обновления items — prewarm cache + render + redraw.
function setItems(newItems) {
  items = Array.isArray(newItems) ? newItems : [];
  _prewarmSectorImages(items);
  renderSidebar();
  if (phase === Phase.IDLE) drawWheel(currentRotation);
}

// v3.8.0/v3.8.1: Центральная GIF колеса — кружок-контейнер всегда виден.
// Внутри: <img id="hub-gif"> (если URL) или <span id="hub-default">🐝</span>.
// GIF обрезается по кругу через overflow:hidden + border-radius:50% на контейнере.
// <img> независим от canvas redraw — обновляется мгновенно через WS event.
let _hubGifUrl = null;
let _hubGifLibrary = [];  // v3.8.1: список сохранённых URL'ов

function setHubGif(url) {
  const img = document.getElementById('hub-gif');
  const def = document.getElementById('hub-default');
  if (!img || !def) return;
  const newUrl = (url && String(url).trim()) || null;
  if (newUrl === _hubGifUrl) return;  // dedupe
  _hubGifUrl = newUrl;
  if (!newUrl) {
    // Нет гифки → показываем пчелу 🐝 (кружок остаётся виден всегда)
    img.style.display = 'none';
    img.removeAttribute('src');
    img.onerror = null;
    def.style.display = 'block';
    return;
  }
  // Есть гифка → показываем <img>, скрываем пчелу
  def.style.display = 'none';
  img.onerror = () => {
    // Если гифка не загрузилась — fallback на пчелу 🐝 (только если это всё ещё тот же URL)
    if (_hubGifUrl === newUrl) {
      img.style.display = 'none';
      def.style.display = 'block';
    }
  };
  img.src = newUrl;
  img.style.display = 'block';
}

async function fetchHubGif() {
  try {
    const r = await fetch('/api/wheel/hub_gif', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    setHubGif(data.url || null);
  } catch (e) { /* swallow network errors */ }
}

// v3.8.1/v3.8.3: Библиотека гифок
async function fetchHubGifLibrary() {
  // v3.8.3: библиотека грузится ВСЕМ юзерам (не только Матке) —
  // обычные юзеры могут выбирать из сохранённого Маткой.
  try {
    const r = await fetch('/api/wheel/hub_gif/library', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    _hubGifLibrary = Array.isArray(data.urls) ? data.urls : [];
    renderHubGifLibrary();
  } catch (e) { /* swallow */ }
}

function renderHubGifLibrary() {
  const container = document.getElementById('hub-gif-library');
  if (!container) return;
  if (_hubGifLibrary.length === 0) {
    const emptyText = (typeof IS_SUPERUSER !== 'undefined' && IS_SUPERUSER)
      ? 'Пусто. Загрузите гифки через /settings.'
      : 'Пусто. Матка ещё не загрузила ни одной гифки.';
    container.innerHTML = `<div class="hub-gif-library-empty">${emptyText}</div>`;
    return;
  }
  container.innerHTML = '';
  _hubGifLibrary.forEach((url) => {
    const thumb = document.createElement('div');
    thumb.className = 'hub-gif-thumb';
    // v3.10.0: toggle — если этот URL сейчас активный, thumbnail темнеет
    if (_hubGifUrl === url) {
      thumb.classList.add('hub-gif-thumb-active');
    }
    thumb.title = _hubGifUrl === url ? 'Клик чтобы убрать (вернуть пчелу)' : 'Клик чтобы поставить';
    const img = document.createElement('img');
    img.src = url;
    img.alt = '';
    img.loading = 'lazy';
    img.onerror = () => { thumb.style.display = 'none'; };
    thumb.appendChild(img);
    // v3.10.0: toggle логика
    // - клик по неактивному thumbnail → установить (меняет гифку)
    // - клик по активному thumbnail → убрать (вернуть пчелу 🐝)
    // - клик по другому когда есть активный → поменять
    thumb.addEventListener('click', (e) => {
      if (e.target.classList.contains('hub-gif-thumb-remove')) return;
      if (_hubGifUrl === url) {
        // Уже активный → убрать (вернуть пчелу)
        setHubGifViaApi('', false);
      } else {
        // Не активный → установить
        setHubGifViaApi(url, false);
      }
    });
    // Кнопка удаления из библиотеки — только для Матки
    if (typeof IS_SUPERUSER !== 'undefined' && IS_SUPERUSER) {
      const removeBtn = document.createElement('button');
      removeBtn.className = 'hub-gif-thumb-remove';
      removeBtn.textContent = '×';
      removeBtn.title = 'Удалить из библиотеки';
      removeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        removeFromLibrary(url);
      });
      thumb.appendChild(removeBtn);
    }
    container.appendChild(thumb);
  });
}

async function setHubGifViaApi(url, saveToLibrary) {
  const formData = new FormData();
  formData.append('url', url || '');
  if (saveToLibrary) formData.append('save_to_library', 'true');
  try {
    const r = await fetch('/api/wheel/hub_gif', { method: 'POST', body: formData });
    const data = await r.json();
    if (r.ok && data.ok) {
      setHubGif(data.url || null);
      // Если сервер вернул обновлённую библиотеку (save_to_library=true) — перерендерим
      if (data.library) {
        _hubGifLibrary = data.library;
        renderHubGifLibrary();
      }
      return true;
    }
    alert('Ошибка: ' + (data.error || `HTTP ${r.status}`));
    return false;
  } catch (e) {
    alert('Сетевая ошибка: ' + (e?.message || e));
    return false;
  }
}

async function setHubGifFromInput() {
  const input = document.getElementById('hub-gif-url-input');
  const chk = document.getElementById('hub-gif-save-chk');
  if (!input) return;
  const url = (input.value || '').trim();
  const save = chk ? chk.checked : false;
  if (url && !/^https?:\/\//i.test(url)) {
    alert('URL должен начинаться с http:// или https://');
    return;
  }
  const ok = await setHubGifViaApi(url, save);
  if (ok && !url) {
    // Очистили — снимаем галочку
    if (chk) chk.checked = false;
    if (input) input.value = '';
  }
}

async function clearHubGif() {
  const input = document.getElementById('hub-gif-url-input');
  const chk = document.getElementById('hub-gif-save-chk');
  if (input) input.value = '';
  if (chk) chk.checked = false;
  await setHubGifViaApi('', false);
}

async function removeFromLibrary(url) {
  if (!confirm('Удалить гифку из библиотеки?')) return;
  try {
    // DELETE с query-параметром (HTTP DELETE не имеет body)
    const r = await fetch(`/api/wheel/hub_gif/library?url=${encodeURIComponent(url)}`, { method: 'DELETE' });
    const data = await r.json();
    if (r.ok && data.ok) {
      _hubGifLibrary = Array.isArray(data.urls) ? data.urls : [];
      renderHubGifLibrary();
    } else {
      alert('Ошибка: ' + (data.error || `HTTP ${r.status}`));
    }
  } catch (e) {
    alert('Сетевая ошибка: ' + (e?.message || e));
  }
}

// =================================================================
// === UI bindings (exposed globally for inline onclick handlers) ===
// =================================================================

window.setMode = function (mode) {
  uiMode = mode;
  const normalBtn = document.getElementById('mode-normal');
  const elimBtn = document.getElementById('mode-elimination');
  if (normalBtn) normalBtn.classList.toggle('active', mode === 'normal');
  if (elimBtn) elimBtn.classList.toggle('active', mode === 'elimination');
};

// v3.8.1: expose для inline onclick в wheel.html
window.setHubGifFromInput = setHubGifFromInput;
window.clearHubGif = clearHubGif;

window.updateDuration = function (val) {
  spinDuration = Math.max(3, Math.min(30, parseInt(val, 10) || 6)) * 1000;
  const label = document.getElementById('duration-label');
  if (label) label.textContent = String(Math.round(spinDuration / 1000));
};

window.toggleSound = function () {
  soundEnabled = !soundEnabled;
  const btn = document.getElementById('sound-btn');
  if (btn) {
    btn.textContent = soundEnabled ? '🔊' : '🔇';
    btn.classList.toggle('muted', !soundEnabled);
  }
  if (soundEnabled) ensureAudio();
};

window.setVolume = function (val) {
  tickVolume = Math.min(1, Math.max(0, parseInt(val, 10) / 100));
  if (tickVolume === 0) {
    if (soundEnabled) {
      soundEnabled = false;
      const btn = document.getElementById('sound-btn');
      if (btn) { btn.textContent = '🔇'; btn.classList.add('muted'); }
    }
  } else if (!soundEnabled) {
    soundEnabled = true;
    const btn = document.getElementById('sound-btn');
    if (btn) { btn.textContent = '🔊'; btn.classList.remove('muted'); }
    ensureAudio();
  }
};

window.startTestMode = startTestMode;
window.endTestMode = endTestMode;
window.addTestItem = addTestItem;
window.dismissWinnerCard = dismissWinnerCard;

// =================================================================
// === Audio ===
// =================================================================
function ensureAudio() {
  if (!audioCtx) {
    try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
    catch (e) { audioCtx = null; }
  }
  if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
}

function playTick(speedRatio = 1) {
  if (!soundEnabled || tickVolume <= 0 || !audioCtx) return;
  try {
    // v3.11: мягкий деревянный «ток» вместо резкого square-щелчка.
    // triangle + lowpass + короткий exp-спад — округло и не раздражает.
    const now = audioCtx.currentTime;
    const r = Math.max(0, Math.min(1, speedRatio));
    const baseFreq = 360 + 420 * r;  // ниже прежнего (было 600..1800)
    const osc = audioCtx.createOscillator();
    osc.type = 'triangle';
    osc.frequency.setValueAtTime(baseFreq, now);
    osc.frequency.exponentialRampToValueAtTime(baseFreq * 0.5, now + 0.05);
    const filter = audioCtx.createBiquadFilter();
    filter.type = 'lowpass';
    filter.frequency.setValueAtTime(1500, now);
    filter.Q.value = 0.5;
    const gain = audioCtx.createGain();
    gain.gain.setValueAtTime(0, now);
    gain.gain.linearRampToValueAtTime(tickVolume * 0.26, now + 0.004);
    gain.gain.exponentialRampToValueAtTime(0.0008, now + 0.06);
    osc.connect(filter); filter.connect(gain); gain.connect(audioCtx.destination);
    osc.start(now); osc.stop(now + 0.07);
  } catch (e) { /* swallow */ }
}

// =================================================================
// === Canvas rendering ===
// =================================================================
const canvas = document.getElementById('wheel-canvas');
const ctx = canvas.getContext('2d');
const canvasSize = 500;
const DPR = Math.min(window.devicePixelRatio || 1, 3);
canvas.width = canvasSize * DPR;
canvas.height = canvasSize * DPR;
const center = canvasSize / 2;
const radius = canvasSize / 2 - 20;

function textColorFor(bg) {
  const m = /^#?([0-9a-f]{6})$/i.exec((bg || '').trim());
  if (!m) return '#1A1A1A';
  const n = parseInt(m[1], 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.58 ? '#1A1A1A' : '#FFFFFF';
}

function fitText(text, maxWidth) {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(text.slice(0, mid) + '…').width <= maxWidth) lo = mid; else hi = mid - 1;
  }
  return lo > 0 ? text.slice(0, lo).trimEnd() + '…' : '…';
}

// Returns the items array that the canvas should render right now.
function getCanvasItems() {
  if (phase !== Phase.IDLE && spinSnapshot && spinSnapshot.length > 0) return spinSnapshot;
  return items;
}

function drawWheel(rotation = 0) {
  if (!ctx) return;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, canvasSize, canvasSize);

  const drawItems = getCanvasItems();

  if (drawItems.length === 0) {
    // v3.8.1: пустое колесо — только subtle dashed circle (без 🎬 и 'Нет лотов',
    // они конфликтовали с центральным hub-circle). Hub-circle с пчелой 🐝
    // остаётся виден в центре как индикатор пустого колеса.
    ctx.beginPath(); ctx.arc(center, center, radius, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255,255,255,0.04)'; ctx.fill();
    ctx.setLineDash([10, 8]); ctx.strokeStyle = 'rgba(255,183,3,0.45)';
    ctx.lineWidth = 3; ctx.stroke();
    ctx.setLineDash([]);
    return;
  }

  const anglePer = (Math.PI * 2) / drawItems.length;
  ctx.save();
  ctx.translate(center, center);
  ctx.rotate(rotation);

  for (let i = 0; i < drawItems.length; i++) {
    const startAngle = i * anglePer, endAngle = startAngle + anglePer;
    const isHighlighted = (i === highlightIdx);
    const secColor = drawItems[i].color || '#FFB703';
    const posterUrl = drawItems[i].gif_poster_path || null;

    // Sector fill (база — всегда цвет, для прозрачности GIF-постера и fallback)
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.arc(0, 0, radius, startAngle, endAngle);
    ctx.closePath();
    ctx.fillStyle = secColor; ctx.fill();
    ctx.strokeStyle = '#1A1A1A'; ctx.lineWidth = 2; ctx.stroke();

    // v3.7.0: GIF-poster overlay — drawImage с cover-логикой, clip по сектору.
    // Анимированный GIF не анимируется в canvas — рисуем PNG-постер (первый кадр).
    if (posterUrl) {
      const img = _getSectorImage(posterUrl);
      if (img && img.width > 0 && img.height > 0) {
        ctx.save();
        // clip по сектору (чтобы изображение не вышло за границы)
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.arc(0, 0, radius, startAngle, endAngle);
        ctx.closePath();
        ctx.clip();
        // cover-логика: центрируем в (0,0) — центр колеса — масштабируем
        // так, чтобы изображение покрыло весь круг (radius от центра).
        const scale = Math.max(radius * 2 / img.width, radius * 2 / img.height);
        const dw = img.width * scale;
        const dh = img.height * scale;
        ctx.drawImage(img, -dw / 2, -dh / 2, dw, dh);
        ctx.restore();
      }
      // если img === null — грузится или ошибка, fallback на цвет уже отрисован выше
    }

    // Highlight overlay
    if (isHighlighted) {
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, radius, startAngle, endAngle);
      ctx.closePath();
      ctx.strokeStyle = '#FFD700'; ctx.lineWidth = 5; ctx.stroke();
      ctx.shadowColor = '#FFD700'; ctx.shadowBlur = 15;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, radius * 0.95, startAngle, endAngle);
      ctx.closePath();
      ctx.strokeStyle = 'rgba(255, 215, 0, 0.4)'; ctx.lineWidth = 3; ctx.stroke();
      ctx.shadowBlur = 0;
    }

    // Label
    ctx.save();
    ctx.rotate(startAngle + anglePer / 2);
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    ctx.fillStyle = isHighlighted ? '#FFD700' : textColorFor(secColor);
    const arcAtText = 2 * (radius - 18) * Math.sin(anglePer / 2);
    const fontSize = Math.max(10, Math.min(16, Math.floor(arcAtText * 0.6)));
    ctx.font = `bold ${fontSize}px sans-serif`;
    const maxTextWidth = radius - 18 - 52;
    const name = fitText(drawItems[i].name, maxTextWidth);
    // v3.7.0: если есть GIF-постер, текст может быть нечитаем поверх картинки —
    // добавляем subtle dark shadow для контраста
    if (posterUrl) {
      ctx.shadowColor = 'rgba(0,0,0,0.85)';
      ctx.shadowBlur = 4;
    }
    ctx.fillText(name, radius - 18, 0);
    ctx.shadowBlur = 0;
    ctx.restore();
  }

  // Hub — отрисовка убрана в v3.8.1. Центральный хаб теперь живёт как
  // <div id="hub-circle"> поверх canvas (CSS .hub-circle), внутри <img> (если
  // есть URL) или <span>🐝</span> (default). Это даёт всегда видимый кружок-
  // контейнер, GIF обрезается по кругу через overflow:hidden, и анимация
  // работает независимо от canvas redraw (можно менять GIF во время крутки).
  ctx.restore();
}

// =================================================================
// === Sidebar list (live items) ===
// =================================================================
function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

function renderSidebar() {
  const list = document.getElementById('items-list');
  const empty = document.getElementById('empty-state');
  const clearBtn = document.getElementById('clear-btn');
  const count = document.getElementById('count');
  if (!list) return;

  count.textContent = String(items.length);
  list.innerHTML = '';

  if (items.length === 0) {
    if (empty) empty.style.display = 'block';
    if (clearBtn) clearBtn.style.display = 'none';
  } else {
    if (empty) empty.style.display = 'none';
    if (clearBtn) clearBtn.style.display = 'block';
    items.forEach((item) => {
      const li = document.createElement('li');
      li.className = 'item-row';
      // v3.7.0: если есть GIF — показываем анимированный <img> (GIF анимируется
      // штатно через <img>, в отличие от canvas drawImage который берёт 1 кадр).
      // Цветной кружок оставляем как fallback если GIF не загрузился.
      const itemId = String(item.id);
      const gifPath = item.gif_path || '';
      const colorDot = `<span class="item-color" style="background:${item.color || '#FFB703'}"></span>`;
      const gifImg = gifPath
        ? `<img class="item-gif-preview" src="${escapeHtml(gifPath)}" alt="" loading="lazy" onerror="this.style.display='none'">`
        : '';
      li.innerHTML =
        colorDot +
        gifImg +
        `<span class="item-name">${escapeHtml(item.name)}</span>`;
      // v3.7.0: для Матки — кнопки управления GIF
      if (typeof IS_SUPERUSER !== 'undefined' && IS_SUPERUSER) {
        const gifControls = document.createElement('span');
        gifControls.className = 'item-gif-controls';
        // Upload (file input)
        const fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.accept = 'image/gif';
        fileInput.className = 'gif-file-input';
        fileInput.dataset.id = itemId;
        fileInput.addEventListener('change', (e) => {
          const f = e.target.files && e.target.files[0];
          if (f) uploadGif(itemId, f);
          e.target.value = '';  // сбросить чтобы можно было выбрать тот же файл снова
        });
        const uploadLabel = document.createElement('label');
        uploadLabel.className = 'gif-upload-btn';
        uploadLabel.title = gifPath ? 'Заменить GIF' : 'Загрузить GIF (только Матка)';
        uploadLabel.textContent = '📎';
        uploadLabel.appendChild(fileInput);
        gifControls.appendChild(uploadLabel);
        // Remove GIF (если есть)
        if (gifPath) {
          const removeGifBtn = document.createElement('button');
          removeGifBtn.className = 'gif-remove-btn';
          removeGifBtn.title = 'Удалить GIF';
          removeGifBtn.textContent = '🎬ⓧ';
          removeGifBtn.dataset.id = itemId;
          removeGifBtn.addEventListener('click', () => removeGif(itemId));
          gifControls.appendChild(removeGifBtn);
        }
        li.appendChild(gifControls);
      }
      const removeBtn = document.createElement('button');
      removeBtn.className = 'item-remove';
      removeBtn.dataset.id = itemId;
      removeBtn.textContent = '×';
      removeBtn.title = 'Удалить';
      removeBtn.addEventListener('click', () => removeItem(item.id));
      li.appendChild(removeBtn);
      list.appendChild(li);
    });
  }
  updateButtonState();
}

// v3.7.0: upload GIF для сектора (только Матка)
async function uploadGif(itemId, file) {
  if (!file) return;
  if (file.size > 2 * 1024 * 1024) {
    alert('GIF слишком большой (макс 2MB). Размер: ' + (file.size / 1024 / 1024).toFixed(2) + ' MB');
    return;
  }
  const formData = new FormData();
  formData.append('file', file);
  try {
    const r = await fetch(`/api/wheel/items/${encodeURIComponent(itemId)}/gif`, {
      method: 'POST',
      body: formData,
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      setItems(data.items);
    } else {
      alert('Ошибка загрузки GIF: ' + (data.error || `HTTP ${r.status}`));
    }
  } catch (e) {
    alert('Сетевая ошибка: ' + (e?.message || e));
  }
}

// v3.7.0: удалить GIF сектора (только Матка)
async function removeGif(itemId) {
  if (!confirm('Удалить GIF с этого сектора?')) return;
  try {
    const r = await fetch(`/api/wheel/items/${encodeURIComponent(itemId)}/gif`, { method: 'DELETE' });
    const data = await r.json();
    if (r.ok && data.ok) {
      setItems(data.items);
    } else {
      alert('Ошибка удаления GIF: ' + (data.error || `HTTP ${r.status}`));
    }
  } catch (e) {
    alert('Сетевая ошибка: ' + (e?.message || e));
  }
}

function updateButtonState() {
  const btn = document.getElementById('spin-btn');
  if (!btn) return;
  const activeItems = getCanvasItems();
  btn.disabled = (phase !== Phase.IDLE) || activeItems.length < 2;
}

// =================================================================
// === Progress bar ===
// =================================================================
function showProgress() {
  const p = document.getElementById('spin-progress');
  if (!p) return;
  p.classList.add('show');
}

function setProgress(pct) {
  const bar = document.getElementById('spin-progress-bar');
  if (bar) bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
}

function hideProgress() {
  const p = document.getElementById('spin-progress');
  if (p) p.classList.remove('show');
  setProgress(0);
}

// =================================================================
// === Winner card ===
// =================================================================
async function fetchPosterUrl(tmdbId) {
  // Returns a URL or null. Uses /api/wheel/poster?tmdb_id=... if available,
  // otherwise tries Kinopoisk lookup endpoint, otherwise returns null.
  if (!tmdbId) return null;
  if (_posterCache.has(tmdbId)) return _posterCache.get(tmdbId);
  try {
    const r = await fetch(`/api/wheel/poster?tmdb_id=${encodeURIComponent(tmdbId)}`, { cache: 'no-store' });
    if (!r.ok) { _posterCache.set(tmdbId, null); return null; }
    const data = await r.json();
    const url = data.poster_url || null;
    _posterCache.set(tmdbId, url);
    return url;
  } catch (e) { return null; }
}

function dismissWinnerCard() {
  const card = document.getElementById('winner-card');
  if (card) card.classList.remove('show');
  if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
}

function showWinnerCard(winner, mode, eliminated, remainingCount) {
  if (!winner) return;
  const card = document.getElementById('winner-card');
  const posterEl = document.getElementById('winner-card-poster');
  const nameEl = document.getElementById('winner-card-name');
  const subtitleEl = document.getElementById('winner-card-subtitle');
  const actionsEl = document.getElementById('winner-card-actions');
  if (!card || !nameEl) return;

  // Set name + subtitle
  nameEl.textContent = winner.name || '';
  if (mode === 'elimination_final' && eliminated) {
    if (subtitleEl) subtitleEl.textContent = `Выбыл: ${eliminated.name}`;
  } else if (mode === 'elimination' && eliminated) {
    if (subtitleEl) subtitleEl.textContent = `Осталось: ${remainingCount}`;
  } else {
    if (subtitleEl) subtitleEl.textContent = '';
  }

  // Reset poster to placeholder
  if (posterEl) {
    posterEl.classList.add('placeholder');
    posterEl.textContent = '🎬';
    posterEl.removeAttribute('src');
    posterEl.onload = null;
    posterEl.onerror = null;
  }

  // Fetch poster async (if tmdb_id present)
  if (posterEl && winner.tmdb_id) {
    fetchPosterUrl(winner.tmdb_id).then((url) => {
      if (!url || !posterEl) return;
      // Only show the poster if the card is still showing the SAME winner
      // (user might have started another spin in the meantime)
      if (nameEl.textContent !== (winner.name || '')) return;
      const img = new Image();
      img.onload = () => {
        if (nameEl.textContent !== (winner.name || '')) return;
        posterEl.classList.remove('placeholder');
        posterEl.textContent = '';
        posterEl.src = url;
      };
      img.src = url;
    });
  }

  // Actions: kinopoisk search + dismiss
  if (actionsEl) {
    actionsEl.innerHTML = '';
    const search = document.createElement('a');
    search.className = 'winner-card-btn';
    search.href = `https://www.kinopoisk.ru/index.php?kp_query=${encodeURIComponent(winner.name || '')}`;
    search.target = '_blank';
    search.rel = 'noopener noreferrer';
    search.textContent = '🔍 На Кинопоиске';
    actionsEl.appendChild(search);

    const close = document.createElement('button');
    close.className = 'winner-card-btn ghost';
    close.type = 'button';
    close.textContent = '✕ Закрыть';
    close.addEventListener('click', dismissWinnerCard);
    actionsEl.appendChild(close);
  }

  card.classList.add('show');

  // Auto-hide after 12s (longer than old 5s banner — user wanted more visible)
  if (bannerTimer) clearTimeout(bannerTimer);
  bannerTimer = setTimeout(dismissWinnerCard, 12000);
}

// =================================================================
// === REST API calls ===
// =================================================================
async function fetchItems() {
  try {
    const r = await fetch('/api/wheel/items', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    setItems(data.items);
  } catch (e) { /* swallow network errors */ }
}

async function fetchWheelState() {
  // Polling fallback — fetches /api/wheel/state (items + active_spin).
  // Used both as a safety net when WS is dead, and to catch up if we missed events.
  if (wsGaveUp) {
    // WS permanently rejected — keep polling every 3s.
  }
  try {
    const r = await fetch('/api/wheel/state', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    const newItems = Array.isArray(data.items) ? data.items : [];
    items = newItems;
    renderSidebar();
    if (phase === Phase.IDLE) drawWheel(currentRotation);

    // If server says there's an active spin and we're idle — sync it.
    const active = data.active_spin;
    if (active && phase === Phase.IDLE) {
      handleSpinStarted(active, /*fromPolling=*/true);
    }
  } catch (e) { /* swallow */ }
}

async function removeItem(id) {
  try {
    const r = await fetch(`/api/wheel/items/${encodeURIComponent(id)}`, { method: 'DELETE' });
    const data = await r.json();
    if (r.ok) {
      setItems(data.items);
    }
  } catch (e) { /* swallow */ }
}

async function clearWheel() {
  if (!confirm('Удалить все фильмы из колеса?')) return;
  try {
    const r = await fetch('/api/wheel/clear', { method: 'POST' });
    const data = await r.json();
    if (r.ok) {
      setItems(data.items);
    }
  } catch (e) { /* swallow */ }
}

// =================================================================
// === Physics easing (must be byte-identical to wheel_physics.wheel_ease) ===
// =================================================================
function wheelEase(t) {
  const ACCEL_END = 0.20, PLATEAU_END = 0.70;
  const accel_area = ACCEL_END / 2;
  const plateau_area = PLATEAU_END - ACCEL_END;
  const decel_area = (1 - PLATEAU_END) / 2;
  const v_max = 1 / (accel_area + plateau_area + decel_area);
  if (t < ACCEL_END) {
    const u = t / ACCEL_END, u3 = u * u * u;
    return v_max * ACCEL_END * (u3 - u3 * u / 2);
  } else if (t < PLATEAU_END) {
    return v_max * ACCEL_END * 0.5 + v_max * (t - ACCEL_END);
  } else {
    const u = (t - PLATEAU_END) / (1 - PLATEAU_END), u3 = u * u * u, u4 = u3 * u;
    return v_max * ACCEL_END * 0.5 + v_max * (PLATEAU_END - ACCEL_END)
         + v_max * (1 - PLATEAU_END) * (u - u3 + u4 / 2);
  }
}

function getSectorUnderPointer(rotation, itemsCount) {
  if (itemsCount <= 0) return -1;
  const anglePer = (Math.PI * 2) / itemsCount;
  let normalized = (-Math.PI / 2 - rotation) % (Math.PI * 2);
  if (normalized < 0) normalized += Math.PI * 2;
  return Math.floor(normalized / anglePer) % itemsCount;
}

function bouncePointer() {
  const p = document.getElementById('pointer');
  if (!p) return;
  p.classList.remove('bouncing');
  void p.offsetWidth;
  p.classList.add('bouncing');
}

// =================================================================
// === Animation ===
// =================================================================
function animatePhysics(initialRotation, totalRotation, durationMs, snapshotCount, onProgress, onComplete) {
  const startTime = performance.now();
  lastSectorUnderPointer = getSectorUnderPointer(initialRotation, snapshotCount);

  function frame(now) {
    const elapsed = now - startTime;
    const progress = Math.min(elapsed / durationMs, 1);
    const eased = wheelEase(progress);
    currentRotation = initialRotation + totalRotation * eased;
    drawWheel(currentRotation);

    // Update progress bar
    if (typeof onProgress === 'function') onProgress(progress);

    // Tick sound + pointer bounce on sector change
    if (snapshotCount > 0) {
      const currentSector = getSectorUnderPointer(currentRotation, snapshotCount);
      if (currentSector !== lastSectorUnderPointer && currentSector >= 0) {
        lastSectorUnderPointer = currentSector;
        // Velocity ratio from easing derivative (approx)
        let vel = 0;
        const ACCEL_END = 0.20, PLATEAU_END = 0.70;
        if (progress < ACCEL_END) {
          const u = progress / ACCEL_END; vel = u * u * (3 - 2 * u);
        } else if (progress < PLATEAU_END) {
          vel = 1;
        } else {
          const u = (progress - PLATEAU_END) / (1 - PLATEAU_END);
          vel = 1 - (u * u * (3 - 2 * u));
        }
        playTick(vel);
        bouncePointer();
      }
    }

    if (progress < 1) {
      requestAnimationFrame(frame);
    } else if (typeof onComplete === 'function') {
      onComplete();
    }
  }
  requestAnimationFrame(frame);
}

// =================================================================
// === Phase / spin flow ===
// =================================================================
function setPhase(newPhase) {
  phase = newPhase;
  updateButtonState();
  // Visual cue on the wheel container
  const wrap = document.getElementById('wheel-canvas-wrap');
  if (wrap) wrap.classList.toggle('spinning', phase === Phase.SPINNING);
}

// Compute the result objects from physics + snapshot, then show winner card.
function revealResult(physics, snapshot, mode) {
  const winnerIdx = physics.winner_idx;
  const itemsCount = snapshot.length;

  // Snap to final position (in case the animation finished off-target)
  if (physics.final_rotation != null) {
    currentRotation = physics.final_rotation;
  }
  highlightIdx = (winnerIdx >= 0 && winnerIdx < itemsCount) ? winnerIdx : -1;
  drawWheel(currentRotation);
  hideProgress();

  // Determine winner / eliminated based on mode + winner_idx
  let eliminated = null, winner = null, remainingCount = itemsCount;
  if (winnerIdx >= 0 && winnerIdx < itemsCount) {
    eliminated = snapshot[winnerIdx];
    if (mode === 'classic') {
      winner = eliminated;
      remainingCount = Math.max(0, itemsCount - 1);
    } else if (mode === 'elimination_final') {
      winner = snapshot.find((it, idx) => idx !== winnerIdx) || null;
      remainingCount = Math.max(0, itemsCount - 2);
    } else { // 'elimination'
      winner = null;
      remainingCount = Math.max(0, itemsCount - 1);
    }
  }

  if (winner) {
    showWinnerCard(winner, mode, eliminated, remainingCount);
  } else if (eliminated) {
    // Pure elimination round — show smaller banner
    const banner = document.getElementById('winner-banner');
    if (banner) {
      banner.textContent = `❌ Выбыл: ${eliminated.name} (осталось ${remainingCount})`;
      banner.style.background = 'var(--red, #E4583E)';
      banner.style.color = '#fff';
      banner.classList.add('show');
      if (bannerTimer) clearTimeout(bannerTimer);
      bannerTimer = setTimeout(() => {
        if (banner) { banner.classList.remove('show'); banner.style.background = ''; banner.style.color = ''; }
      }, 6000);
    }
  }

  setPhase(Phase.REVEALING);

  // After 1.5s, return to IDLE. Sidebar items should already have been
  // updated via WS wheel_updated or polling by then. As a safety net,
  // also fetch.
  if (postSpinFallbackTimer) clearTimeout(postSpinFallbackTimer);
  postSpinFallbackTimer = setTimeout(() => {
    postSpinFallbackTimer = null;
    spinSnapshot = null;
    spinPhysics = null;
    setPhase(Phase.IDLE);
    drawWheel(currentRotation);
    fetchItems();
  }, 1500);

  // Clear highlight after 5s
  setTimeout(() => {
    highlightIdx = -1;
    if (phase === Phase.IDLE) drawWheel(currentRotation);
  }, 5000);
}

// Classic spin — POST /api/wheel/spin
async function spin() {
  if (phase !== Phase.IDLE) return;
  if (items.length < 2) return;

  setPhase(Phase.SPINNING);
  ensureAudio();
  dismissWinnerCard();
  const banner = document.getElementById('winner-banner');
  if (banner) banner.classList.remove('show');
  highlightIdx = -1;
  showProgress();
  setProgress(0);

  let data;
  try {
    const formData = new FormData();
    formData.append('duration', String(Math.round(spinDuration / 1000)));
    const r = await fetch('/api/wheel/spin', { method: 'POST', body: formData });
    data = await r.json();
    if (!r.ok) {
      hideProgress();
      handleSpinError(data);
      setPhase(Phase.IDLE);
      drawWheel(currentRotation);
      return;
    }
  } catch (e) {
    hideProgress();
    alert('Сетевая ошибка: ' + (e?.message || e));
    setPhase(Phase.IDLE);
    drawWheel(currentRotation);
    return;
  }

  if (!data.physics) {
    hideProgress();
    setPhase(Phase.IDLE);
    drawWheel(currentRotation);
    return;
  }

  // Take a snapshot of items as they were for this spin — the server
  // also returns items[] in the response, prefer that for consistency
  // with what the server used to determine winner_idx.
  spinSnapshot = (Array.isArray(data.items) && data.items.length > 0)
    ? data.items
    : items.slice();
  spinPhysics = data.physics;
  spinMode = data.mode || 'classic';

  animatePhysics(
    spinPhysics.initial_rotation,
    spinPhysics.total_rotation,
    spinPhysics.duration_sec * 1000,
    spinSnapshot.length,
    (p) => setProgress(p * 100),
    () => revealResult(spinPhysics, spinSnapshot, spinMode)
  );
}

// Elimination spin — POST /api/wheel/spin_elimination
async function spinElimination() {
  if (phase !== Phase.IDLE) return;
  if (items.length < 2) return;

  setPhase(Phase.SPINNING);
  ensureAudio();
  dismissWinnerCard();
  const banner = document.getElementById('winner-banner');
  if (banner) banner.classList.remove('show');
  highlightIdx = -1;
  showProgress();
  setProgress(0);

  let data;
  try {
    const fd = new FormData();
    fd.append('duration', String(Math.round(spinDuration / 1000)));
    const r = await fetch('/api/wheel/spin_elimination', { method: 'POST', body: fd });
    data = await r.json();
    if (!r.ok) {
      hideProgress();
      handleSpinError(data);
      setPhase(Phase.IDLE);
      drawWheel(currentRotation);
      return;
    }
  } catch (e) {
    hideProgress();
    alert('Сетевая ошибка: ' + (e?.message || e));
    setPhase(Phase.IDLE);
    drawWheel(currentRotation);
    return;
  }

  if (!data.physics) {
    hideProgress();
    setPhase(Phase.IDLE);
    drawWheel(currentRotation);
    return;
  }

  spinSnapshot = (Array.isArray(data.items) && data.items.length > 0)
    ? data.items
    : items.slice();
  spinPhysics = data.physics;
  spinMode = data.mode || 'elimination';

  animatePhysics(
    spinPhysics.initial_rotation,
    spinPhysics.total_rotation,
    spinPhysics.duration_sec * 1000,
    spinSnapshot.length,
    (p) => setProgress(p * 100),
    () => revealResult(spinPhysics, spinSnapshot, spinMode)
  );
}

function handleSpinError(data) {
  if (!data) return;
  if (data.error_code === 'winner_already_determined'
      || data.error_code === 'active_collection_exists') {
    alert((data.error || 'Ошибка') + '\n\nНажмите OK чтобы перейти к /movienight.');
    window.location.href = '/movienight';
    return;
  }
  alert(data.error || 'Ошибка спина');
}

// =================================================================
// === WebSocket ===
// =================================================================
function wsReconnectDelay() {
  // Capped exponential backoff: 1s, 2s, 4s, 8s, 15s, 15s, ...
  const base = Math.min(WS_MAX_RECONNECT_DELAY, 1000 * Math.pow(2, wsReconnectAttempts));
  wsReconnectAttempts++;
  return base;
}

function connectWS() {
  if (wsGaveUp) return;  // server rejected us permanently

  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // v3.0: GUILD_ID is now a STRING (template passes it quoted), so no
  // precision loss for Discord IDs > 2^53.
  const guildId = (typeof GUILD_ID !== 'undefined') ? GUILD_ID : '0';
  const url = `${protocol}//${location.host}/ws/wheel?guild_id=${encodeURIComponent(guildId)}`;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    setTimeout(connectWS, wsReconnectDelay());
    return;
  }

  ws.onopen = () => {
    wsReconnectAttempts = 0;
    clearInterval(wsPingTimer);
    // Heartbeat — proxies (BotHost etc.) kill idle WS connections ~60s.
    wsPingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send('ping'); } catch (e) { /* swallow */ }
      }
    }, 25000);
    // Sync items from HTTP on each fresh WS open — guarantees we're
    // not stale even if we missed events during the disconnect.
    fetchWheelState();
  };

  ws.onmessage = (event) => {
    // Robust parse — one bad message must not kill the handler.
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      return;
    }
    if (!msg || typeof msg !== 'object' || !msg.type) return;

    try {
      switch (msg.type) {
        case 'wheel_updated':  handleWheelUpdated(msg.payload); break;
        case 'spin_started':   handleSpinStarted(msg.payload, false); break;
        case 'spin_completed': handleSpinCompleted(msg.payload); break;
        case 'hub_gif_changed': setHubGif(msg.payload && msg.payload.url || null); break;  // v3.8.0
        case 'error':          handleWSError(msg.payload); break;
        default: break;
      }
    } catch (e) {
      // Don't let one handler failure break the connection.
      if (console && console.error) console.error('WS handler error', e);
    }
  };

  ws.onclose = (event) => {
    clearInterval(wsPingTimer);
    ws = null;

    // v3.0: respect server close codes. 4001 = invalid guild — give up.
    // This was the source of the infinite-reconnect storm before v3.0.
    if (event && event.code === 4001) {
      wsGaveUp = true;
      if (console && console.warn) {
        console.warn('[wheel] WS closed with 4001 (invalid guild). Stopping reconnect. Reason:', event.reason);
      }
      return;
    }
    // 4002 = server error, 1000 = normal close, 1006 = abnormal — try to reconnect.
    setTimeout(connectWS, wsReconnectDelay());
  };

  ws.onerror = () => {
    // Just close — onclose handles reconnect logic.
    try { ws && ws.close(); } catch (e) {}
  };
}

// --- WS event handlers ---
function handleWheelUpdated(payload) {
  if (!payload) return;
  setItems(payload.items);  // v3.7.0: prewarm cache + render + redraw
}

function handleSpinStarted(payload, fromPolling) {
  if (!payload) return;
  const physics = payload.physics;
  if (!physics) return;
  const payloadItems = Array.isArray(payload.items) ? payload.items : [];
  if (payloadItems.length === 0) return;

  // If we're already animating, skip — unless this is from polling and
  // we somehow missed the spin (then sync up).
  if (phase === Phase.SPINNING || phase === Phase.REVEALING) {
    return;
  }

  spinSnapshot = payloadItems;
  spinPhysics = physics;
  spinMode = payload.mode || 'classic';
  setPhase(Phase.SPINNING);
  ensureAudio();
  dismissWinnerCard();
  const banner = document.getElementById('winner-banner');
  if (banner) banner.classList.remove('show');
  highlightIdx = -1;
  showProgress();
  setProgress(0);

  animatePhysics(
    spinPhysics.initial_rotation,
    spinPhysics.total_rotation,
    spinPhysics.duration_sec * 1000,
    spinSnapshot.length,
    (p) => setProgress(p * 100),
    () => revealResult(spinPhysics, spinSnapshot, spinMode)
  );
}

function handleSpinCompleted(payload) {
  if (!payload) return;

  // Snap to final rotation (safety net in case the animation ended off-target).
  if (payload.final_rotation != null) {
    currentRotation = payload.final_rotation;
  }

  // Force exit from SPINNING if animation somehow never completed.
  if (phase === Phase.SPINNING) {
    // If we still have a snapshot + physics, reveal result now.
    if (spinSnapshot && spinPhysics) {
      revealResult(spinPhysics, spinSnapshot, spinMode);
    } else if (payload.winner) {
      // Fallback banner from server payload.
      showWinnerCard(payload.winner, payload.mode, payload.eliminated, payload.remaining_count);
      setPhase(Phase.REVEALING);
      setTimeout(() => {
        spinSnapshot = null;
        spinPhysics = null;
        setPhase(Phase.IDLE);
        drawWheel(currentRotation);
        fetchItems();
      }, 1500);
    } else {
      hideProgress();
      setPhase(Phase.IDLE);
      drawWheel(currentRotation);
    }
  } else if (phase === Phase.REVEALING) {
    // Already revealing — just snap rotation, banner is already shown.
    drawWheel(currentRotation);
  } else {
    // IDLE — maybe we missed the spin entirely. Show banner from server payload.
    if (payload.winner) {
      showWinnerCard(payload.winner, payload.mode, payload.eliminated, payload.remaining_count);
      drawWheel(currentRotation);
    }
  }

  // Always refresh the sidebar — winner was just removed on the server.
  fetchItems();
}

function handleWSError(payload) {
  if (!payload) return;
  if (console && console.warn) {
    console.warn('[wheel] Server-side WS error:', payload.code, payload.message);
  }
}

// =================================================================
// === Polling fallback (v3.0) ===
// =================================================================
function startPolling() {
  // Every 3s — cheap insurance against dead WS.
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(fetchWheelState, 3000);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// =================================================================
// === Event listeners ===
// =================================================================
document.getElementById('spin-btn')?.addEventListener('click', () => {
  if (uiMode === 'elimination') spinElimination();
  else spin();
});

document.getElementById('clear-btn')?.addEventListener('click', clearWheel);

// Manual add form (only present when the page includes it — e.g. test mode)
document.getElementById('add-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('item-name');
  if (!input) return;
  const name = input.value.trim();
  if (!name) return;
  try {
    const formData = new FormData();
    formData.append('name', name);
    const r = await fetch('/api/wheel/items', { method: 'POST', body: formData });
    const data = await r.json();
    if (r.ok) {
      setItems(data.items);
      input.value = '';
    } else {
      alert(data.error || 'Ошибка');
    }
  } catch (e) {
    alert('Сетевая ошибка: ' + (e?.message || e));
  }
});

// =================================================================
// === Test mode (superuser only) ===
// =================================================================
function showTestMsg(text, kind) {
  const el = document.getElementById('test-mode-msg');
  if (!el) return;
  el.textContent = text;
  el.className = 'test-mode-msg ' + (kind || '');
  el.style.display = 'block';
  if (kind !== 'error') setTimeout(() => { el.style.display = 'none'; }, 4000);
}

async function startTestMode() {
  if (!confirm('Запустить тестовый режим колеса?\n\nВсе спины будут помечены как тестовые. Посты в Discord/TG пойдут с префиксом [TEST]. По завершении все тестовые данные будут удалены из БД.')) return;
  try {
    const r = await fetch('/api/wheel/test/start', { method: 'POST' });
    const data = await r.json();
    if (r.ok) {
      showTestMsg('✅ Тестовый режим запущен. Лог: ' + (data.log_path || ''), 'success');
      setTimeout(() => location.reload(), 1500);
    } else {
      showTestMsg(data.error || 'Не удалось запустить', 'error');
    }
  } catch (e) {
    showTestMsg('Сетевая ошибка: ' + (e?.message || e), 'error');
  }
}

async function endTestMode() {
  if (!confirm('Завершить тестовый режим?\n\nВСЕ тестовые записи будут удалены из БД. Лог-файл сохранится.')) return;
  try {
    const r = await fetch('/api/wheel/test/end', { method: 'POST' });
    const data = await r.json();
    if (r.ok) {
      const stats = data.cleanup_stats || {};
      showTestMsg(
        `✅ Тест завершён. Удалено: ${stats.wheel_items_deleted || 0} items, ${stats.winners_deleted || 0} winners.`,
        'success'
      );
      setTimeout(() => location.reload(), 3000);
    } else {
      showTestMsg(data.error || 'Не удалось завершить', 'error');
    }
  } catch (e) {
    showTestMsg('Сетевая ошибка: ' + (e?.message || e), 'error');
  }
}

async function addTestItem() {
  const input = document.getElementById('test-item-name');
  const name = (input?.value || '').trim();
  if (!name) { showTestMsg('Введите название', 'error'); return; }
  try {
    const fd = new FormData(); fd.append('name', name);
    const r = await fetch('/api/wheel/test/add_item', { method: 'POST', body: fd });
    const data = await r.json();
    if (r.ok) {
      input.value = '';
      showTestMsg(`✅ Добавлен: "${name}" (всего ${data.count})`, 'success');
    } else {
      showTestMsg(data.error || 'Ошибка', 'error');
    }
  } catch (e) {
    showTestMsg('Сетевая ошибка: ' + (e?.message || e), 'error');
  }
}

// =================================================================
// === Bootstrap ===
// =================================================================
(function init() {
  setItems((typeof INITIAL_ITEMS !== 'undefined' && Array.isArray(INITIAL_ITEMS)) ? INITIAL_ITEMS : []);
  fetchHubGif();         // v3.8.0: загрузить текущую центральную GIF с сервера
  fetchHubGifLibrary();  // v3.8.1: загрузить библиотеку гифок (только для Матки)
  connectWS();
  startPolling();
  // Page lifecycle: if user comes back to the tab after being away,
  // sync state immediately.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      fetchWheelState();
      fetchHubGif();  // v3.8.0: перетянем hub_gif вдруг другой Матка сменил
    }
  });
})();
