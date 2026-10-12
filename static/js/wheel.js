    // === State ===
    let items = [];
    let spinning = false;
    let currentRotation = 0;
    let currentMode = 'normal'; // 'normal' | 'elimination'
    let spinDuration = 5000; // ms
    // v2.3.3: ID текущего спина — чтобы WS spin_started от своего же спина
    // не запускал анимацию повторно (инициатор уже анимирует после HTTP-ответа)
    let _currentSpinId = null;
    // v2.3.3: Звук тиков через Web Audio API (синтез, без mp3)
    let audioCtx = null;
    let tickVolume = 0.4; // 0..1
    let soundEnabled = true;
    let lastSectorUnderPointer = -1;

    function setMode(mode) {
      currentMode = mode;
      document.getElementById('mode-normal').classList.toggle('active', mode === 'normal');
      document.getElementById('mode-elimination').classList.toggle('active', mode === 'elimination');
    }

    function updateDuration(val) {
      spinDuration = parseInt(val) * 1000;
      document.getElementById('duration-label').textContent = val;
    }

    // v2.3.3: Звук тиков — синтез через Web Audio API
    function ensureAudio() {
      if (!audioCtx) {
        try {
          audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        } catch (e) {
          audioCtx = null;
        }
      }
      // Браузеры требуют resume() после user gesture
      if (audioCtx && audioCtx.state === 'suspended') {
        audioCtx.resume();
      }
    }

    // v2.3.3: Звук тиков — улучшенный синтез через Web Audio API.
    // Вариант C: sine (база) + square (атака) + lowpass filter.
    // Pitch привязан к скорости вращения (velocity/v_max, 0..1):
    // 1.0 = высокая скорость → высокий тон (1800Hz)
    // 0.0 = остановка → низкий тон (600Hz)
    function playTick(speedRatio = 1) {
      if (!soundEnabled || tickVolume <= 0 || !audioCtx) return;
      try {
        const now = audioCtx.currentTime;
        // v2.3.3: Pitch от скорости. speedRatio 0..1 → freq 600..1800.
        const baseFreq = 600 + 1200 * Math.max(0, Math.min(1, speedRatio));

        // === Sine wave — базовый тон (мягкий, чистый) ===
        const sineOsc = audioCtx.createOscillator();
        sineOsc.type = 'sine';
        sineOsc.frequency.setValueAtTime(baseFreq, now);
        sineOsc.frequency.exponentialRampToValueAtTime(baseFreq * 0.6, now + 0.04);

        // === Square wave — атака/щелчок в начале (короткий, тихий) ===
        const squareOsc = audioCtx.createOscillator();
        squareOsc.type = 'square';
        squareOsc.frequency.setValueAtTime(baseFreq * 1.5, now);
        squareOsc.frequency.exponentialRampToValueAtTime(baseFreq, now + 0.01);

        // === Low-pass filter — приглушение, не резкий ===
        const filter = audioCtx.createBiquadFilter();
        filter.type = 'lowpass';
        filter.frequency.setValueAtTime(baseFreq * 3, now);
        filter.Q.value = 1.0;

        // === Gain envelope — очень короткий (40мс), экспоненциальное затухание ===
        const gain = audioCtx.createGain();
        gain.gain.setValueAtTime(0, now);
        gain.gain.linearRampToValueAtTime(tickVolume * 0.35, now + 0.002);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.04);

        const squareGain = audioCtx.createGain();
        squareGain.gain.setValueAtTime(0, now);
        squareGain.gain.linearRampToValueAtTime(tickVolume * 0.12, now + 0.002);
        squareGain.gain.exponentialRampToValueAtTime(0.001, now + 0.015);

        sineOsc.connect(filter);
        filter.connect(gain);
        gain.connect(audioCtx.destination);
        squareOsc.connect(squareGain);
        squareGain.connect(audioCtx.destination);

        sineOsc.start(now);
        squareOsc.start(now);
        sineOsc.stop(now + 0.05);
        squareOsc.stop(now + 0.02);
      } catch (e) {
        // silent
      }
    }

    function toggleSound() {
      soundEnabled = !soundEnabled;
      const btn = document.getElementById('sound-btn');
      btn.textContent = soundEnabled ? '🔊' : '🔇';
      btn.classList.toggle('muted', !soundEnabled);
      if (soundEnabled) ensureAudio();
    }

    function setVolume(val) {
      tickVolume = Math.min(1, Math.max(0, parseInt(val) / 100));
      // Если громкость 0 — считаем что выключено
      if (tickVolume === 0) {
        const btn = document.getElementById('sound-btn');
        if (soundEnabled) {
          soundEnabled = false;
          btn.textContent = '🔇';
          btn.classList.add('muted');
        }
      } else if (!soundEnabled) {
        // Если громкость > 0 и звук был выключен — включаем
        soundEnabled = true;
        const btn = document.getElementById('sound-btn');
        btn.textContent = '🔊';
        btn.classList.remove('muted');
        ensureAudio();
      }
    }

    // === Canvas ===
    const canvas = document.getElementById('wheel-canvas');
    const ctx = canvas.getContext('2d');
    const canvasSize = 500;
    canvas.width = canvasSize;
    canvas.height = canvasSize;

    const center = canvasSize / 2;
    const radius = canvasSize / 2 - 20;
    // v2.3.3: индекс подсвеченного сектора (золотая обводка в конце анимации)
    let highlightIdx = -1;

    function drawWheel(rotation = 0) {
      ctx.clearRect(0, 0, canvasSize, canvasSize);

      if (items.length === 0) {
        // Пустое колесо — серый круг
        ctx.beginPath();
        ctx.arc(center, center, radius, 0, Math.PI * 2);
        ctx.fillStyle = '#333';
        ctx.fill();
        ctx.strokeStyle = '#666';
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.fillStyle = '#999';
        ctx.font = '20px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('Колесо пустое', center, center);
        return;
      }

      const anglePer = (Math.PI * 2) / items.length;
      ctx.save();
      ctx.translate(center, center);
      ctx.rotate(rotation);

      for (let i = 0; i < items.length; i++) {
        const startAngle = i * anglePer;
        const endAngle = startAngle + anglePer;
        const isHighlighted = (i === highlightIdx);

        // Сектор
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.arc(0, 0, radius, startAngle, endAngle);
        ctx.closePath();
        ctx.fillStyle = items[i].color || '#FFB703';
        ctx.fill();
        ctx.strokeStyle = '#1A1A1A';
        ctx.lineWidth = 2;
        ctx.stroke();

        // v2.3.3: Подсветка winner-сектора золотой обводкой
        if (isHighlighted) {
          ctx.beginPath();
          ctx.moveTo(0, 0);
          ctx.arc(0, 0, radius, startAngle, endAngle);
          ctx.closePath();
          ctx.strokeStyle = '#FFD700';
          ctx.lineWidth = 5;
          ctx.stroke();
          // Внутренний glow
          ctx.shadowColor = '#FFD700';
          ctx.shadowBlur = 15;
          ctx.beginPath();
          ctx.moveTo(0, 0);
          ctx.arc(0, 0, radius * 0.95, startAngle, endAngle);
          ctx.closePath();
          ctx.strokeStyle = 'rgba(255, 215, 0, 0.4)';
          ctx.lineWidth = 3;
          ctx.stroke();
          ctx.shadowBlur = 0;
        }

        // Текст
        ctx.save();
        ctx.rotate(startAngle + anglePer / 2);
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = isHighlighted ? '#FFD700' : '#1A1A1A';
        const fontSize = items.length > 15 ? 11 : (items.length > 8 ? 13 : 15);
        ctx.font = `bold ${fontSize}px sans-serif`;
        const name = items[i].name.length > 30 ? items[i].name.slice(0, 28) + '…' : items[i].name;
        ctx.fillText(name, radius - 18, 0);
        ctx.restore();
      }

      // Центральный круг (декоративный)
      ctx.beginPath();
      ctx.arc(0, 0, 40, 0, Math.PI * 2);
      ctx.fillStyle = '#1A1A1A';
      ctx.fill();
      ctx.strokeStyle = '#FFB703';
      ctx.lineWidth = 4;
      ctx.stroke();
      ctx.fillStyle = '#FFB703';
      ctx.font = 'bold 18px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('🐝', 0, 0);

      ctx.restore();
    }

    // === Render items list ===
    function renderList() {
      const list = document.getElementById('items-list');
      const empty = document.getElementById('empty-state');
      const clearBtn = document.getElementById('clear-btn');
      const count = document.getElementById('count');
      const spinBtn = document.getElementById('spin-btn');

      count.textContent = items.length;
      list.innerHTML = '';

      const elimBtn = document.getElementById('spin-elimination-btn');
      if (items.length === 0) {
        empty.style.display = 'block';
        clearBtn.style.display = 'none';
        spinBtn.disabled = true;
        if (elimBtn) elimBtn.disabled = true;
      } else {
        empty.style.display = 'none';
        clearBtn.style.display = items.length > 0 ? 'block' : 'none';
        spinBtn.disabled = items.length < 2 || spinning;
        if (elimBtn) elimBtn.disabled = items.length < 2 || spinning;

        items.forEach((item, idx) => {
          const li = document.createElement('li');
          li.className = 'item-row';
          li.innerHTML = `
            <span class="item-color" style="background:${item.color || '#FFB703'}"></span>
            <span class="item-name">${escapeHtml(item.name)}</span>
            <button class="item-remove" data-id="${item.id}" title="Удалить">×</button>
          `;
          list.appendChild(li);
        });

        // Bind remove buttons
        list.querySelectorAll('.item-remove').forEach(btn => {
          btn.addEventListener('click', () => removeItem(btn.dataset.id));
        });
      }

      drawWheel(currentRotation);
    }

    function escapeHtml(s) {
      const div = document.createElement('div');
      div.textContent = s;
      return div.innerHTML;
    }

    // === API calls ===
    async function fetchItems() {
      const r = await fetch('/api/wheel/items');
      const data = await r.json();
      items = data.items || [];
      renderList();
    }

    async function addItem(name) {
      const formData = new FormData();
      formData.append('name', name);
      const r = await fetch('/api/wheel/items', { method: 'POST', body: formData });
      const data = await r.json();
      if (r.ok) {
        items = data.items || [];
        renderList();
        return true;
      }
      alert(data.error || 'Ошибка');
      return false;
    }

    async function removeItem(id) {
      const r = await fetch(`/api/wheel/items/${id}`, { method: 'DELETE' });
      const data = await r.json();
      if (r.ok) {
        items = data.items || [];
        renderList();
      }
    }

    async function clearWheel() {
      if (!confirm('Удалить все фильмы из колеса?')) return;
      const r = await fetch('/api/wheel/clear', { method: 'POST' });
      const data = await r.json();
      if (r.ok) {
        items = data.items || [];
        renderList();
      }
    }

    // === Spin animation ===
    // v2.3.3: Физика анимации — плавная кривая с непрерывной производной.
    // 3 фазы: разгон (0-20%) → плато (20-70%) → торможение (70-100%).
    // Производная на стыках совпадает → нет рывков скорости.
    //
    // Подход: задаём скорость v(t) как функцию (smoothstep для разгона/торможения,
    // константа для плато). Позиция = интеграл скорости. Нормируем на S=1 при t=1.
    function wheelEase(t) {
      const ACCEL_END = 0.20;
      const PLATEAU_END = 0.70;
      // Скорость в каждой фазе:
      //  - Разгон (0..ACCEL_END): v(t) = smoothstep(t/ACCEL_END) * v_max
      //  - Плато: v(t) = v_max
      //  - Торможение: v(t) = (1 - smoothstep(u)) * v_max, где u = (t-PLATEAU_END)/(1-PLATEAU_END)
      // Площадь под скоростью = пройденный путь. Нормируем v_max чтобы при t=1 S=1.
      const accel_area = ACCEL_END / 2;              // integral smoothstep(0..1) = 1/2
      const plateau_area = (PLATEAU_END - ACCEL_END); // v_max * 1 (константа)
      const decel_area = (1 - PLATEAU_END) / 2;       // 1/2 (обратный smoothstep)
      const v_max = 1 / (accel_area + plateau_area + decel_area);
      if (t < ACCEL_END) {
        // S(t) = v_max * ACCEL_END * integral_0^{t/ACCEL_END} smoothstep(u) du
        // integral smoothstep(u) du = u^3 - u^4/2 (т.к. smoothstep(u)=3u^2-2u^3)
        const u = t / ACCEL_END;
        const u3 = u * u * u;
        const integral = u3 - u3 * u / 2;  // u^3 - u^4/2
        return v_max * ACCEL_END * integral;
      } else if (t < PLATEAU_END) {
        // S(t) = S(ACCEL_END) + v_max * (t - ACCEL_END)
        const accel_full = v_max * ACCEL_END * (1 - 0.5);  // 1/2 при u=1
        return accel_full + v_max * (t - ACCEL_END);
      } else {
        // S(t) = S(PLATEAU_END) + v_max * (1-PLATEAU_END) * integral_0^u (1 - smoothstep) du
        // integral (1 - smoothstep) du = u - (u^3 - u^4/2) = u - u^3 + u^4/2
        const accel_full = v_max * ACCEL_END * 0.5;
        const plateau_full = v_max * (PLATEAU_END - ACCEL_END);
        const u = (t - PLATEAU_END) / (1 - PLATEAU_END);
        const u3 = u * u * u;
        const u4 = u3 * u;
        const integral = u - u3 + u4 / 2;
        return accel_full + plateau_full + v_max * (1 - PLATEAU_END) * integral;
      }
    }

    function easeOutCubic(t) {
      return 1 - Math.pow(1 - t, 3);
    }

    // v2.3.3: Проверка какой сектор под указателем — для тиков
    function getSectorUnderPointer(rotation, itemsCount) {
      if (itemsCount === 0) return -1;
      const anglePer = (Math.PI * 2) / itemsCount;
      // Указатель сверху = угол -π/2 в стандартной canvas системе (12 часов)
      // Колесо повернуто на `rotation` → сектор i находится на угле (i * anglePer + rotation)
      // Сектор под указателем = тот у которого (i * anglePer + rotation) ≈ -π/2 (mod 2π)
      // → i = (-π/2 - rotation) / anglePer (mod itemsCount)
      let normalized = (-Math.PI / 2 - rotation) % (Math.PI * 2);
      if (normalized < 0) normalized += Math.PI * 2;
      const idx = Math.floor(normalized / anglePer) % itemsCount;
      return idx;
    }

    function bouncePointer() {
      const p = document.getElementById('pointer');
      if (!p) return;
      p.classList.remove('bouncing');
      // force reflow чтобы анимация перезапустилась
      void p.offsetWidth;
      p.classList.add('bouncing');
    }

    function animateSpin(duration, onComplete, snapshotItemsCount, isInitiator) {
      // v2.3.3 Вариант D: настоящая физика — angular velocity + friction.
      // Колесо не «крутится к target», а замедляется по инерции и останавливается
      // где остановится. Победитель = сектор под указателем ПОСЛЕ остановки.
      //
      // Фазы (как и раньше, но теперь в терминах velocity):
      // 0-20% (разгон): velocity 0 → v_max через smoothstep
      // 20-70% (плато): velocity = v_max
      // 70-100% (торможение): v_max → 0 через smoothstep
      //
      // v_max подбираем так чтобы за duration суммарный поворот был ~5-7 оборотов.
      const startRotation = currentRotation;
      const startTime = performance.now();
      lastSectorUnderPointer = getSectorUnderPointer(currentRotation, snapshotItemsCount);

      // Целевой поворот за всё время (5-7 оборотов) — определяет v_max
      const extraTurns = 5 + Math.floor(Math.random() * 3);
      const totalRotationTarget = extraTurns * Math.PI * 2 + Math.random() * Math.PI * 2;
      // v_max вычисляем из условия: интеграл velocity = totalRotationTarget
      const ACCEL_END = 0.20;
      const PLATEAU_END = 0.70;
      const accel_area = ACCEL_END / 2;
      const plateau_area = PLATEAU_END - ACCEL_END;
      const decel_area = (1 - PLATEAU_END) / 2;
      const v_max = totalRotationTarget / (accel_area + plateau_area + decel_area);

      function frame(now) {
        const elapsed = now - startTime;
        const progress = Math.min(elapsed / duration, 1);
        // velocity в этой точке (для pitch-модуляции тиков)
        let velocity = 0;
        if (progress < ACCEL_END) {
          const u = progress / ACCEL_END;
          velocity = v_max * (u * u * (3 - 2 * u));  // smoothstep
        } else if (progress < PLATEAU_END) {
          velocity = v_max;
        } else {
          const u = (progress - PLATEAU_END) / (1 - PLATEAU_END);
          velocity = v_max * (1 - (u * u * (3 - 2 * u)));  // 1 - smoothstep
        }
        // Позиция = интеграл velocity → wheelEase(progress) * totalRotationTarget
        const eased = wheelEase(progress);
        currentRotation = startRotation + totalRotationTarget * eased;
        drawWheel(currentRotation);

        // v2.3.3: Тик при прохождении сектора под указателем.
        // Pitch = относительная скорость (0..1).
        if (snapshotItemsCount > 0) {
          const currentSector = getSectorUnderPointer(currentRotation, snapshotItemsCount);
          if (currentSector !== lastSectorUnderPointer && currentSector >= 0) {
            lastSectorUnderPointer = currentSector;
            const pitch = velocity / v_max;  // 0..1
            playTick(pitch);
            bouncePointer();
          }
        }

        if (progress < 1) {
          requestAnimationFrame(frame);
        } else {
          onComplete();
        }
      }
      requestAnimationFrame(frame);
    }

    async function spin() {
      if (spinning || items.length < 2) return;
      spinning = true;
      document.getElementById('spin-btn').disabled = true;
      document.getElementById('winner-banner').classList.remove('show');
      highlightIdx = -1;

      // v2.3.3: активируем аудио (нужно для тиков после user gesture)
      ensureAudio();

      // Сnapshot ДО fetch — WS может заменить items пока ждём ответа
      const itemsSnapshot = items.slice();
      const snapshotCount = itemsSnapshot.length;

      // v2.3.3 Вариант D: генерируем spin_id на клиенте, отправляем на сервер.
      // Backend НЕ выбирает winner — он только фиксирует активный спин.
      // После остановки колеса мы определим winner по позиции и отправим
      // POST /api/wheel/result.
      const clientSpinId = 'c_' + Math.random().toString(36).slice(2, 10);
      _currentSpinId = clientSpinId;

      const formData = new FormData();
      formData.append('duration', Math.round(spinDuration / 1000).toString());
      formData.append('client_spin_id', clientSpinId);
      const r = await fetch('/api/wheel/spin', { method: 'POST', body: formData });
      const data = await r.json();
      if (!r.ok) {
        if (data.error_code === 'winner_already_determined' || data.error_code === 'active_collection_exists') {
          alert(data.error + '\n\nНажмите OK чтобы перейти к /movienight.');
          window.location.href = '/movienight';
          return;
        }
        alert(data.error || 'Ошибка спина');
        spinning = false;
        _currentSpinId = null;
        document.getElementById('spin-btn').disabled = false;
        return;
      }

      // v2.3.3 Вариант D: backend прислал spin_id (наш clientSpinId) и items.
      // Запускаем физическую анимацию — куда остановится, туда и остановится.
      const animItems = data.items && data.items.length > 0 ? data.items : itemsSnapshot;
      const animDuration = (data.duration || Math.round(spinDuration / 1000)) * 1000;
      runSpinAnimation(animItems, animDuration, 'classic', clientSpinId, /*isInitiator=*/true);
    }

    // v2.3.3 Вариант D: универсальная функция анимации.
    // Колесо крутится по физике (без target_item), останавливается естественным путём.
    // После остановки определяется winner = сектор под указателем.
    // Если isInitiator=true — отправляем POST /api/wheel/result с winner_id + rotation.
    function runSpinAnimation(snapshotItems, durationMs, mode, spinId, isInitiator) {
      const snapshotCount = snapshotItems.length;
      if (snapshotCount < 1) {
        spinning = false;
        _currentSpinId = null;
        document.getElementById('spin-btn').disabled = items.length < 2;
        return;
      }

      // Синхронизируем локальные items со снапшотом (на случай если пришли через WS)
      items = snapshotItems;
      renderList();

      animateSpin(durationMs, () => {
        // Анимация завершена — определяем winner по позиции указателя.
        const winnerIdx = getSectorUnderPointer(currentRotation, snapshotCount);
        const winner = (winnerIdx >= 0 && winnerIdx < snapshotItems.length) ? snapshotItems[winnerIdx] : null;

        if (!winner) {
          // Аномалия — не удалось определить сектор. Обновим состояние.
          fetchItems();
          spinning = false;
          _currentSpinId = null;
          document.getElementById('spin-btn').disabled = items.length < 2;
          return;
        }

        // v2.3.3: подсветка winner-сектора
        highlightIdx = winnerIdx;

        // Показываем баннер
        const banner = document.getElementById('winner-banner');
        if (mode === 'elimination_final') {
          // Для elimination_final winner — это финальный победитель
          // (определяется отдельно, см. WS spin_completed handler)
          banner.textContent = `🎉 Победитель: ${winner.name}!`;
          banner.style.background = 'linear-gradient(135deg, #FFB703 0%, #FB8500 100%)';
          banner.style.color = '#1A1A1A';
        } else if (mode === 'elimination') {
          // Промежуточный раунд — winner = кто выбыл
          banner.textContent = `❌ Выбыл: ${winner.name} (осталось ${snapshotCount - 1})`;
          banner.style.background = 'var(--red)';
          banner.style.color = '#fff';
        } else {
          // classic
          banner.textContent = `🎉 Победитель: ${winner.name}!`;
          banner.style.background = '';
          banner.style.color = '';
        }
        banner.classList.add('show');

        // v2.3.3 Вариант D: если мы инициатор — отправляем результат на сервер.
        // Backend валидирует, удаляет winner, постит в Discord/TG,
        // рассылает WS spin_completed другим клиентам.
        if (isInitiator) {
          sendSpinResult(spinId, winner.id, currentRotation, mode);
        }

        // Обновим список (для инициатора — после подтверждения сервера;
        // для не-инициатора — обновление придёт через WS wheel_updated)
        if (isInitiator) {
          // Не обновляем сразу — сервер пришлёт WS wheel_updated через ~1с
        } else {
          fetchItems();
        }

        spinning = false;
        // _currentSpinId НЕ сбрасываем здесь для инициатора — он нужен
        // для обработки WS spin_completed (чтобы не показать баннер дважды).
        // Сбросим после получения spin_completed.
        if (!isInitiator) {
          _currentSpinId = null;
        }
        document.getElementById('spin-btn').disabled = items.length < 2;

        // Снять подсветку и баннер через 5 сек
        setTimeout(() => {
          highlightIdx = -1;
          banner.classList.remove('show');
          banner.style.background = '';
          banner.style.color = '';
        }, 5000);
      }, snapshotCount, isInitiator);
    }

    // v2.3.3 Вариант D: отправить результат спина на сервер для валидации.
    async function sendSpinResult(spinId, winnerId, rotation, mode) {
      try {
        const fd = new FormData();
        fd.append('spin_id', spinId);
        fd.append('winner_id', winnerId.toString());
        fd.append('rotation', rotation.toString());
        fd.append('mode', mode);
        const r = await fetch('/api/wheel/result', { method: 'POST', body: fd });
        const data = await r.json();
        if (!r.ok) {
          console.warn('Wheel result submission failed:', data.error);
        }
        // Сервер валидировал, удалил winner, постит в Discord/TG.
        // Обновим список у себя.
        fetchItems();
      } catch (e) {
        console.error('sendSpinResult error:', e);
      }
    }

    // === Load from watchlist (убрано в v1.8.3 — заменено сбором фильмов) ===

    function connectWS() {
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      // Передаём guild_id как query-параметр чтобы WS получил правильный список лотов
      const ws = new WebSocket(`${protocol}//${location.host}/ws/wheel?guild_id=${typeof GUILD_ID !== 'undefined' ? GUILD_ID : 0}`);

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'wheel_updated') {
          if (!spinning) {
            items = msg.payload.items || [];
            renderList();
          }
        } else if (msg.type === 'spin_started') {
          // v2.3.3 Вариант D: Если это наш собственный спин — мы уже анимируем
          // после HTTP-ответа, игнорируем WS чтобы не запустить дважды.
          if (msg.payload.spin_id === _currentSpinId) {
            return;
          }
          // Если мы уже крутим что-то другое — не вмешиваемся
          if (spinning) return;

          // v2.3.3 Вариант D: backend прислал items + duration (БЕЗ target_item).
          // Запускаем физическую анимацию — куда остановится, туда и остановится.
          // После остановки — определяем winner, но НЕ отправляем на сервер
          // (это сделает инициатор). Мы только показываем анимацию.
          const payload = msg.payload;
          const wsItems = payload.items || [];
          const wsMode = payload.mode || 'classic';
          const wsDuration = (payload.duration || 5) * 1000;

          if (wsItems.length < 1) return;

          // Синхронизируем локальные items
          items = wsItems;
          renderList();
          ensureAudio();

          spinning = true;
          document.getElementById('spin-btn').disabled = true;
          document.getElementById('winner-banner').classList.remove('show');
          highlightIdx = -1;
          _currentSpinId = payload.spin_id;
          runSpinAnimation(wsItems, wsDuration, wsMode, payload.spin_id, /*isInitiator=*/false);
        } else if (msg.type === 'spin_completed') {
          // v2.3.3 Вариант D: backend подтвердил результат.
          // Если мы инициатор и уже показали баннер — игнорируем (сбрасываем только _currentSpinId).
          // Если мы не инициатор (анимировали как зритель) — обновим rotation чтобы
          // наше колесо остановилось в той же позиции что у инициатора.
          const payload = msg.payload;
          if (payload.spin_id === _currentSpinId) {
            // Это наш спин — если мы инициатор, баннер уже показан.
            // Сбрасываем _currentSpinId чтобы быть готовым к новому спину.
            _currentSpinId = null;
            return;
          }
          // Мы — зритель. Если наша анимация ещё идёт — досрочно завершим
          // и выставим финальную позицию от сервера.
          if (spinning && payload.rotation != null) {
            // Плавно докрутим к rotation от сервера (короткая анимация ~500мс)
            currentRotation = payload.rotation;
            drawWheel(currentRotation);
            spinning = false;
            _currentSpinId = null;
            document.getElementById('spin-btn').disabled = items.length < 2;
          }
          // Покажем баннер победителя (если ещё не показан)
          if (payload.winner) {
            const banner = document.getElementById('winner-banner');
            if (payload.mode === 'elimination_final' && payload.eliminated) {
              banner.innerHTML = `❌ Выбыл: ${escapeHtml(payload.eliminated.name)}<br><span style="font-size:1.1em;">🏆 Победитель: ${escapeHtml(payload.winner.name)}!</span>`;
              banner.style.background = 'linear-gradient(135deg, #FFB703 0%, #FB8500 100%)';
              banner.style.color = '#1A1A1A';
            } else if (payload.mode === 'elimination') {
              banner.textContent = `❌ Выбыл: ${payload.winner.name}`;
              banner.style.background = 'var(--red)';
              banner.style.color = '#fff';
            } else {
              banner.textContent = `🎉 Победитель: ${payload.winner.name}!`;
              banner.style.background = '';
              banner.style.color = '';
            }
            banner.classList.add('show');
            setTimeout(() => {
              banner.classList.remove('show');
              banner.style.background = '';
              banner.style.color = '';
            }, 5000);
          }
        } else if (msg.type === 'spin_result') {
          // Совместимость со старым протоколом — игнорируем
          // (Вариант D использует spin_completed вместо spin_result)
        } else if (msg.type === 'elimination_result') {
          if (spinning) return;
          if (msg.payload.eliminated) {
            const banner = document.getElementById('winner-banner');
            banner.textContent = `❌ Выбыл: ${msg.payload.eliminated.name} (осталось ${msg.payload.remaining_count})`;
            banner.style.background = 'var(--red)';
            banner.style.color = '#fff';
            banner.classList.add('show');
            setTimeout(() => {
              banner.classList.remove('show');
              banner.style.background = '';
              banner.style.color = '';
            }, 5000);
          }
        }
      };

      ws.onclose = () => {
        // Переподключение через 3 сек
        setTimeout(connectWS, 3000);
      };

      ws.onerror = (e) => {
        console.error('WS error', e);
        ws.close();
      };
    }

    // === Event listeners ===
    // v2.3.3: удалены dead-code startFilmnight/endFilmnight (ссылались на /api/filmnight/*
    //         который удалён в v1.8.3, заменён на /api/collection/* в movienight.html).

    document.getElementById('add-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = document.getElementById('item-name');
      const name = input.value.trim();
      if (!name) return;
      if (await addItem(name)) {
        input.value = '';
      }
    });

    document.getElementById('spin-btn').addEventListener('click', () => {
      if (currentMode === 'elimination') {
        spinElimination();
      } else {
        spin();
      }
    });
    document.getElementById('clear-btn').addEventListener('click', clearWheel);

    // === Elimination spin ===
    async function spinElimination() {
      if (spinning || items.length < 2) return;
      spinning = true;
      document.getElementById('spin-btn').disabled = true;
      document.getElementById('winner-banner').classList.remove('show');
      highlightIdx = -1;

      ensureAudio();

      const itemsSnapshot = items.slice();
      const snapshotCount = itemsSnapshot.length;

      // v2.3.3 Вариант D: генерируем spin_id на клиенте
      const clientSpinId = 'c_' + Math.random().toString(36).slice(2, 10);
      _currentSpinId = clientSpinId;

      const r = await fetch('/api/wheel/spin_elimination', {
        method: 'POST',
        body: (() => {
          const fd = new FormData();
          fd.append('duration', Math.round(spinDuration / 1000).toString());
          fd.append('client_spin_id', clientSpinId);
          return fd;
        })()
      });
      const data = await r.json();
      if (!r.ok) {
        if (data.error_code === 'winner_already_determined' || data.error_code === 'active_collection_exists') {
          alert(data.error + '\n\nНажмите OK чтобы перейти к /movienight.');
          window.location.href = '/movienight';
          return;
        }
        alert(data.error || 'Ошибка спина');
        spinning = false;
        _currentSpinId = null;
        document.getElementById('spin-btn').disabled = items.length < 2;
        return;
      }

      // v2.3.3 Вариант D: backend прислал items + mode + duration (без winner).
      // Запускаем физическую анимацию — куда остановится, туда и остановится.
      const animItems = data.items && data.items.length > 0 ? data.items : itemsSnapshot;
      const animDuration = (data.duration || Math.round(spinDuration / 1000)) * 1000;
      const animMode = data.mode || 'elimination';
      runSpinAnimation(animItems, animDuration, animMode, clientSpinId, /*isInitiator=*/true);
    }

    // === v2.3.3: Тестовый режим (только для Матки) ===
    function showTestMsg(text, kind) {
      const el = document.getElementById('test-mode-msg');
      if (!el) return;
      el.textContent = text;
      el.className = 'test-mode-msg ' + (kind || '');
      el.style.display = 'block';
      // Авто-скрытие через 4 сек (кроме ошибок)
      if (kind !== 'error') {
        setTimeout(() => { el.style.display = 'none'; }, 4000);
      }
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
        showTestMsg('Сетевая ошибка: ' + e.message, 'error');
      }
    }

    async function endTestMode() {
      if (!confirm('Завершить тестовый режим?\n\nВСЕ тестовые записи (wheel_items, winners, ratings) будут удалены из БД. Лог-файл сохранится для анализа.')) return;
      try {
        const r = await fetch('/api/wheel/test/end', { method: 'POST' });
        const data = await r.json();
        if (r.ok) {
          const stats = data.cleanup_stats || {};
          const msg = `✅ Тест завершён. Удалено: ${stats.wheel_items_deleted || 0} items, ${stats.winners_deleted || 0} winners, ${stats.ratings_deleted || 0} ratings.\nЛог: ${data.log_path || ''}`;
          showTestMsg(msg, 'success');
          setTimeout(() => location.reload(), 3000);
        } else {
          showTestMsg(data.error || 'Не удалось завершить', 'error');
        }
      } catch (e) {
        showTestMsg('Сетевая ошибка: ' + e.message, 'error');
      }
    }

    async function addTestItem() {
      const input = document.getElementById('test-item-name');
      const name = (input?.value || '').trim();
      if (!name) { showTestMsg('Введите название', 'error'); return; }
      try {
        const fd = new FormData();
        fd.append('name', name);
        const r = await fetch('/api/wheel/test/add_item', { method: 'POST', body: fd });
        const data = await r.json();
        if (r.ok) {
          input.value = '';
          showTestMsg(`✅ Добавлен: "${name}" (всего ${data.count})`, 'success');
        } else {
          showTestMsg(data.error || 'Ошибка', 'error');
        }
      } catch (e) {
        showTestMsg('Сетевая ошибка: ' + e.message, 'error');
      }
    }

    // Initial state из SSR (передаётся через <script> в HTML)
    items = typeof INITIAL_ITEMS !== 'undefined' ? INITIAL_ITEMS : [];
    renderList();
    connectWS();
