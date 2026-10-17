    // === State ===
    let items = [];
    let spinning = false;
    let currentRotation = 0;
    let currentMode = 'normal'; // 'normal' | 'elimination'
    let spinDuration = 6000; // ms (default 6s, max 30s)
    let audioCtx = null;
    let tickVolume = 0.4;
    let soundEnabled = true;
    let lastSectorUnderPointer = -1;
    let highlightIdx = -1;
    // v2.4.2: Safety timeout для зависшего спина.
    // Если spin_completed не пришёл за duration + 5с — принудительно сбрасываем
    // spinning и запрашиваем свежее состояние. Защищает от случая когда
    // WS-коннект упал во время анимации и spin_completed потерялся.
    let spinSafetyTimeout = null;
    let currentSpinId = null;
    let currentSpinDurationMs = 0;

    // === v2.4.2: Safety timeout management ===
    // Защита от зависания: если spin_completed не пришёл за
    // duration + 5с после spin_started — принудительно сбросить spinning
    // и запросить свежее состояние через HTTP.
    function startSpinSafetyTimeout(durationMs, expectedSpinId) {
      clearSpinSafetyTimeout();
      spinSafetyTimeout = setTimeout(async () => {
        console.warn('[wheel] Safety timeout fired — spin_completed not received within ' +
                     (durationMs / 1000) + 's. Forcing state reset.');
        // Проверяем текущее состояние через HTTP
        try {
          const r = await fetch('/api/wheel/spin_state');
          if (r.ok) {
            const data = await r.json();
            if (data.active_spin && data.active_spin.spin_id === expectedSpinId) {
              // На сервере спин ещё активен — продлеваем timeout
              console.warn('[wheel] Server says spin still active, extending timeout.');
              startSpinSafetyTimeout(5000, expectedSpinId);
              return;
            }
          }
        } catch (e) {
          console.error('[wheel] spin_state check failed:', e);
        }
        // Сбрасываем состояние
        spinning = false;
        currentSpinId = null;
        const btn = document.getElementById('spin-btn');
        if (btn) btn.disabled = items.length < 2;
        // Запрашиваем свежие items
        await fetchItems();
      }, durationMs + 5000);
    }

    function clearSpinSafetyTimeout() {
      if (spinSafetyTimeout) {
        clearTimeout(spinSafetyTimeout);
        spinSafetyTimeout = null;
      }
    }

    function setMode(mode) {
      currentMode = mode;
      document.getElementById('mode-normal').classList.toggle('active', mode === 'normal');
      document.getElementById('mode-elimination').classList.toggle('active', mode === 'elimination');
    }

    function updateDuration(val) {
      spinDuration = parseInt(val) * 1000;
      document.getElementById('duration-label').textContent = val;
    }

    // === Audio ===
    function ensureAudio() {
      if (!audioCtx) {
        try {
          audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        } catch (e) { audioCtx = null; }
      }
      if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
    }

    function playTick(speedRatio = 1) {
      if (!soundEnabled || tickVolume <= 0 || !audioCtx) return;
      try {
        const now = audioCtx.currentTime;
        const baseFreq = 600 + 1200 * Math.max(0, Math.min(1, speedRatio));
        const sineOsc = audioCtx.createOscillator();
        sineOsc.type = 'sine';
        sineOsc.frequency.setValueAtTime(baseFreq, now);
        sineOsc.frequency.exponentialRampToValueAtTime(baseFreq * 0.6, now + 0.04);
        const squareOsc = audioCtx.createOscillator();
        squareOsc.type = 'square';
        squareOsc.frequency.setValueAtTime(baseFreq * 1.5, now);
        squareOsc.frequency.exponentialRampToValueAtTime(baseFreq, now + 0.01);
        const filter = audioCtx.createBiquadFilter();
        filter.type = 'lowpass';
        filter.frequency.setValueAtTime(baseFreq * 3, now);
        filter.Q.value = 1.0;
        const gain = audioCtx.createGain();
        gain.gain.setValueAtTime(0, now);
        gain.gain.linearRampToValueAtTime(tickVolume * 0.35, now + 0.002);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.04);
        const squareGain = audioCtx.createGain();
        squareGain.gain.setValueAtTime(0, now);
        squareGain.gain.linearRampToValueAtTime(tickVolume * 0.12, now + 0.002);
        squareGain.gain.exponentialRampToValueAtTime(0.001, now + 0.015);
        sineOsc.connect(filter); filter.connect(gain); gain.connect(audioCtx.destination);
        squareOsc.connect(squareGain); squareGain.connect(audioCtx.destination);
        sineOsc.start(now); squareOsc.start(now);
        sineOsc.stop(now + 0.05); squareOsc.stop(now + 0.02);
      } catch (e) {}
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
      if (tickVolume === 0) {
        if (soundEnabled) { soundEnabled = false; const btn = document.getElementById('sound-btn'); btn.textContent = '🔇'; btn.classList.add('muted'); }
      } else if (!soundEnabled) {
        soundEnabled = true; const btn = document.getElementById('sound-btn'); btn.textContent = '🔊'; btn.classList.remove('muted'); ensureAudio();
      }
    }

    // === Canvas ===
    const canvas = document.getElementById('wheel-canvas');
    const ctx = canvas.getContext('2d');
    const canvasSize = 500;
    // v2.4.1: DPR для чёткой отрисовки на ретине
    const DPR = Math.min(window.devicePixelRatio || 1, 3);
    canvas.width = canvasSize * DPR; canvas.height = canvasSize * DPR;
    const center = canvasSize / 2;
    const radius = canvasSize / 2 - 20;

    // v2.4.1: Читаемый цвет текста на секторе по яркости фона
    function textColorFor(bg) {
      const m = /^#?([0-9a-f]{6})$/i.exec((bg || '').trim());
      if (!m) return '#1A1A1A';
      const n = parseInt(m[1], 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
      const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
      return lum > 0.58 ? '#1A1A1A' : '#FFFFFF';
    }

    // v2.4.1: Обрезка текста по ширине в пикселях
    function fitText(text, maxWidth) {
      if (ctx.measureText(text).width <= maxWidth) return text;
      let lo = 0, hi = text.length;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (ctx.measureText(text.slice(0, mid) + '…').width <= maxWidth) lo = mid; else hi = mid - 1;
      }
      return lo > 0 ? text.slice(0, lo).trimEnd() + '…' : '…';
    }

    function drawWheel(rotation = 0) {
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      ctx.clearRect(0, 0, canvasSize, canvasSize);
      if (items.length === 0) {
        // v2.4.1: Пунктирное кольцо с иконкой вместо серого круга
        ctx.beginPath(); ctx.arc(center, center, radius, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(255,255,255,0.04)'; ctx.fill();
        ctx.setLineDash([10, 8]); ctx.strokeStyle = 'rgba(255,183,3,0.45)'; ctx.lineWidth = 3; ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = '#FFB703'; ctx.font = '56px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText('🎬', center, center - 24);
        ctx.fillStyle = 'rgba(200,200,210,0.9)'; ctx.font = '600 20px sans-serif';
        ctx.fillText('Нет лотов', center, center + 38);
        return;
      }
      const anglePer = (Math.PI * 2) / items.length;
      ctx.save(); ctx.translate(center, center); ctx.rotate(rotation);
      for (let i = 0; i < items.length; i++) {
        const startAngle = i * anglePer, endAngle = startAngle + anglePer;
        const isHighlighted = (i === highlightIdx);
        const secColor = items[i].color || '#FFB703';
        ctx.beginPath(); ctx.moveTo(0, 0); ctx.arc(0, 0, radius, startAngle, endAngle); ctx.closePath();
        ctx.fillStyle = secColor; ctx.fill();
        ctx.strokeStyle = '#1A1A1A'; ctx.lineWidth = 2; ctx.stroke();
        if (isHighlighted) {
          ctx.beginPath(); ctx.moveTo(0, 0); ctx.arc(0, 0, radius, startAngle, endAngle); ctx.closePath();
          ctx.strokeStyle = '#FFD700'; ctx.lineWidth = 5; ctx.stroke();
          ctx.shadowColor = '#FFD700'; ctx.shadowBlur = 15;
          ctx.beginPath(); ctx.moveTo(0, 0); ctx.arc(0, 0, radius * 0.95, startAngle, endAngle); ctx.closePath();
          ctx.strokeStyle = 'rgba(255, 215, 0, 0.4)'; ctx.lineWidth = 3; ctx.stroke();
          ctx.shadowBlur = 0;
        }
        ctx.save(); ctx.rotate(startAngle + anglePer / 2);
        ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
        // v2.4.1: Цвет текста по яркости сектора + размер по ширине
        ctx.fillStyle = isHighlighted ? '#FFD700' : textColorFor(secColor);
        const arcAtText = 2 * (radius - 18) * Math.sin(anglePer / 2);
        const fontSize = Math.max(10, Math.min(16, Math.floor(arcAtText * 0.6)));
        ctx.font = `bold ${fontSize}px sans-serif`;
        const maxTextWidth = radius - 18 - 52;
        const name = fitText(items[i].name, maxTextWidth);
        ctx.fillText(name, radius - 18, 0); ctx.restore();
      }
      ctx.beginPath(); ctx.arc(0, 0, 40, 0, Math.PI * 2);
      ctx.fillStyle = '#1A1A1A'; ctx.fill(); ctx.strokeStyle = '#FFB703'; ctx.lineWidth = 4; ctx.stroke();
      ctx.fillStyle = '#FFB703'; ctx.font = 'bold 18px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText('🐝', 0, 0);
      ctx.restore();
    }

    function renderList() {
      const list = document.getElementById('items-list');
      const empty = document.getElementById('empty-state');
      const clearBtn = document.getElementById('clear-btn');
      const count = document.getElementById('count');
      const spinBtn = document.getElementById('spin-btn');
      count.textContent = items.length; list.innerHTML = '';
      if (items.length === 0) {
        empty.style.display = 'block'; clearBtn.style.display = 'none'; spinBtn.disabled = true;
      } else {
        empty.style.display = 'none'; clearBtn.style.display = items.length > 0 ? 'block' : 'none';
        spinBtn.disabled = items.length < 2 || spinning;
        items.forEach((item, idx) => {
          const li = document.createElement('li'); li.className = 'item-row';
          li.innerHTML = `<span class="item-color" style="background:${item.color || '#FFB703'}"></span><span class="item-name">${escapeHtml(item.name)}</span><button class="item-remove" data-id="${item.id}" title="Удалить">×</button>`;
          list.appendChild(li);
        });
        list.querySelectorAll('.item-remove').forEach(btn => { btn.addEventListener('click', () => removeItem(btn.dataset.id)); });
      }
      drawWheel(currentRotation);
    }

    function escapeHtml(s) { const div = document.createElement('div'); div.textContent = s; return div.innerHTML; }

    // === API calls ===
    async function fetchItems() {
      const r = await fetch('/api/wheel/items'); const data = await r.json();
      items = data.items || []; renderList();
    }
    async function addItem(name) {
      const formData = new FormData(); formData.append('name', name);
      const r = await fetch('/api/wheel/items', { method: 'POST', body: formData });
      const data = await r.json();
      if (r.ok) { items = data.items || []; renderList(); return true; }
      alert(data.error || 'Ошибка'); return false;
    }
    async function removeItem(id) {
      const r = await fetch(`/api/wheel/items/${id}`, { method: 'DELETE' });
      const data = await r.json(); if (r.ok) { items = data.items || []; renderList(); }
    }
    async function clearWheel() {
      if (!confirm('Удалить все фильмы из колеса?')) return;
      const r = await fetch('/api/wheel/clear', { method: 'POST' });
      const data = await r.json(); if (r.ok) { items = data.items || []; renderList(); }
    }

    // === Physics easing (identical to wheel_physics.py) ===
    function wheelEase(t) {
      const ACCEL_END = 0.20, PLATEAU_END = 0.70;
      const accel_area = ACCEL_END / 2, plateau_area = PLATEAU_END - ACCEL_END, decel_area = (1 - PLATEAU_END) / 2;
      const v_max = 1 / (accel_area + plateau_area + decel_area);
      if (t < ACCEL_END) { const u = t / ACCEL_END, u3 = u*u*u; return v_max * ACCEL_END * (u3 - u3*u/2); }
      else if (t < PLATEAU_END) { return v_max * ACCEL_END * 0.5 + v_max * (t - ACCEL_END); }
      else { const u = (t - PLATEAU_END) / (1 - PLATEAU_END), u3 = u*u*u, u4 = u3*u; return v_max * ACCEL_END * 0.5 + v_max * (PLATEAU_END - ACCEL_END) + v_max * (1 - PLATEAU_END) * (u - u3 + u4/2); }
    }

    function getSectorUnderPointer(rotation, itemsCount) {
      if (itemsCount === 0) return -1;
      const anglePer = (Math.PI * 2) / itemsCount;
      let normalized = (-Math.PI / 2 - rotation) % (Math.PI * 2);
      if (normalized < 0) normalized += Math.PI * 2;
      return Math.floor(normalized / anglePer) % itemsCount;
    }

    function bouncePointer() {
      const p = document.getElementById('pointer'); if (!p) return;
      p.classList.remove('bouncing'); void p.offsetWidth; p.classList.add('bouncing');
    }

    // === v2.4.0: Server-authoritative spin ===
    // Client receives physics params from server and animates.
    // Server determines the winner — client does NOT send results back.
    function animatePhysics(initialRotation, totalRotation, durationMs, snapshotCount, onComplete) {
      const startTime = performance.now();
      lastSectorUnderPointer = getSectorUnderPointer(initialRotation, snapshotCount);

      function frame(now) {
        const elapsed = now - startTime;
        const progress = Math.min(elapsed / durationMs, 1);
        const eased = wheelEase(progress);
        currentRotation = initialRotation + totalRotation * eased;
        drawWheel(currentRotation);

        // Ticks
        if (snapshotCount > 0) {
          const currentSector = getSectorUnderPointer(currentRotation, snapshotCount);
          if (currentSector !== lastSectorUnderPointer && currentSector >= 0) {
            lastSectorUnderPointer = currentSector;
            // velocity ratio from easing derivative
            let vel = 0;
            const ACCEL_END = 0.20, PLATEAU_END = 0.70;
            if (progress < ACCEL_END) { const u = progress / ACCEL_END; vel = u * u * (3 - 2 * u); }
            else if (progress < PLATEAU_END) { vel = 1; }
            else { const u = (progress - PLATEAU_END) / (1 - PLATEAU_END); vel = 1 - (u * u * (3 - 2 * u)); }
            playTick(vel);
            bouncePointer();
          }
        }

        if (progress < 1) requestAnimationFrame(frame);
        else if (onComplete) onComplete();
      }
      requestAnimationFrame(frame);
    }

    // Show banner based on mode + winner + eliminated
    function showWinnerBanner(mode, winner, eliminated, remainingCount) {
      const banner = document.getElementById('winner-banner');
      if (mode === 'elimination_final' && eliminated && winner) {
        banner.innerHTML = `❌ Выбыл: ${escapeHtml(eliminated.name)}<br><span style="font-size:1.1em;">🏆 Победитель: ${escapeHtml(winner.name)}!</span>`;
        banner.style.background = 'linear-gradient(135deg, #FFB703 0%, #FB8500 100%)';
        banner.style.color = '#1A1A1A';
      } else if (mode === 'elimination' && eliminated) {
        banner.textContent = `❌ Выбыл: ${eliminated.name} (осталось ${remainingCount})`;
        banner.style.background = 'var(--red)'; banner.style.color = '#fff';
      } else if (winner) {
        banner.textContent = `🎉 Победитель: ${winner.name}!`;
        banner.style.background = ''; banner.style.color = '';
      }
      banner.classList.add('show');
      setTimeout(() => {
        banner.classList.remove('show'); banner.style.background = ''; banner.style.color = '';
      }, 5000);
    }

    // === Spin (classic) ===
    async function spin() {
      if (spinning || items.length < 2) return;
      spinning = true;
      currentSpinId = null; // будет установлен после ответа сервера
      currentSpinDurationMs = spinDuration;
      document.getElementById('spin-btn').disabled = true;
      document.getElementById('winner-banner').classList.remove('show');
      highlightIdx = -1;
      ensureAudio();

      const formData = new FormData();
      formData.append('duration', Math.round(spinDuration / 1000).toString());
      const r = await fetch('/api/wheel/spin', { method: 'POST', body: formData });
      const data = await r.json();
      if (!r.ok) {
        if (data.error_code === 'winner_already_determined' || data.error_code === 'active_collection_exists') {
          alert(data.error + '\n\nНажмите OK чтобы перейти к /movienight.');
          window.location.href = '/movienight'; return;
        }
        alert(data.error || 'Ошибка спина');
        spinning = false;
        clearSpinSafetyTimeout();
        document.getElementById('spin-btn').disabled = false;
        return;
      }

      // v2.4.0: Server returned physics params. Animate using them.
      // No more client-side winner determination or /api/wheel/result.
      if (data.physics) {
        currentSpinId = data.spin_id;
        // v2.4.2: Запускаем safety timeout — если spin_completed не придёт
        // за duration + 5с, принудительно сбросим состояние.
        startSpinSafetyTimeout(spinDuration, data.spin_id);
        animatePhysics(
          data.physics.initial_rotation,
          data.physics.total_rotation,
          data.physics.duration_sec * 1000,
          data.physics.items_count,
          () => {
            // Animation complete — wait for spin_completed from server
            // (server posts to Discord/TG after delay, then broadcasts)
          }
        );
      } else {
        // Сервер вернул 200, но без physics — что-то не так, сбрасываем
        spinning = false;
        clearSpinSafetyTimeout();
        document.getElementById('spin-btn').disabled = items.length < 2;
      }
    }

    // === Spin (elimination) ===
    async function spinElimination() {
      if (spinning || items.length < 2) return;
      spinning = true;
      currentSpinId = null;
      currentSpinDurationMs = spinDuration;
      document.getElementById('spin-btn').disabled = true;
      document.getElementById('winner-banner').classList.remove('show');
      highlightIdx = -1;
      ensureAudio();

      const fd = new FormData();
      fd.append('duration', Math.round(spinDuration / 1000).toString());
      const r = await fetch('/api/wheel/spin_elimination', { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok) {
        if (data.error_code === 'winner_already_determined' || data.error_code === 'active_collection_exists') {
          alert(data.error + '\n\nНажмите OK чтобы перейти к /movienight.');
          window.location.href = '/movienight'; return;
        }
        alert(data.error || 'Ошибка спина');
        spinning = false;
        clearSpinSafetyTimeout();
        document.getElementById('spin-btn').disabled = items.length < 2;
        return;
      }

      if (data.physics) {
        currentSpinId = data.spin_id;
        startSpinSafetyTimeout(spinDuration, data.spin_id);
        animatePhysics(
          data.physics.initial_rotation,
          data.physics.total_rotation,
          data.physics.duration_sec * 1000,
          data.physics.items_count,
          () => { /* wait for spin_completed */ }
        );
      } else {
        spinning = false;
        clearSpinSafetyTimeout();
        document.getElementById('spin-btn').disabled = items.length < 2;
      }
    }

    // === WebSocket ===
    function connectWS() {
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${protocol}//${location.host}/ws/wheel?guild_id=${typeof GUILD_ID !== 'undefined' ? GUILD_ID : 0}`);

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'wheel_updated') {
          // v2.4.2: если клиент думает что spinning=true, но сервер шлёт
          // wheel_updated (а не spin_started) — значит на сервере нет
          // активного спина. Сбрасываем зависшее состояние.
          if (spinning) {
            console.warn('[wheel] Got wheel_updated while spinning — server has no active spin. Resetting.');
            spinning = false;
            currentSpinId = null;
            clearSpinSafetyTimeout();
            const btn = document.getElementById('spin-btn');
            if (btn) btn.disabled = items.length < 2;
          }
          items = msg.payload.items || []; renderList();
        } else if (msg.type === 'spin_started') {
          // v2.4.0: Server sent physics params. Animate using them.
          // This handles both initiator (who already got physics via HTTP)
          // and viewers (who get it via WS). Since the physics are identical,
          // both will animate the same way.
          if (spinning) return; // already animating from HTTP response
          const p = msg.payload;
          const physics = p.physics;
          if (!physics || !p.items || p.items.length === 0) return;

          items = p.items; renderList(); ensureAudio();
          spinning = true;
          currentSpinId = p.spin_id;
          currentSpinDurationMs = physics.duration_sec * 1000;
          document.getElementById('spin-btn').disabled = true;
          document.getElementById('winner-banner').classList.remove('show');
          highlightIdx = -1;

          // v2.4.2: safety timeout для зрителей тоже
          startSpinSafetyTimeout(currentSpinDurationMs, p.spin_id);

          animatePhysics(
            physics.initial_rotation,
            physics.total_rotation,
            physics.duration_sec * 1000,
            physics.items_count,
            () => { /* wait for spin_completed */ }
          );
        } else if (msg.type === 'spin_completed') {
          // v2.4.0: Server confirmed result. Snap to final rotation.
          const p = msg.payload;
          if (p.final_rotation != null) {
            currentRotation = p.final_rotation;
            drawWheel(currentRotation);
          }
          spinning = false;
          currentSpinId = null;
          clearSpinSafetyTimeout();
          document.getElementById('spin-btn').disabled = items.length < 2;

          if (p.winner) {
            const winnerIdx = getSectorUnderPointer(currentRotation, items.length);
            if (winnerIdx >= 0) highlightIdx = winnerIdx;
            showWinnerBanner(p.mode, p.winner, p.eliminated, p.remaining_count);
            fetchItems();
          }
          setTimeout(() => { highlightIdx = -1; }, 3000);
        }
      };

      ws.onclose = () => { setTimeout(connectWS, 3000); };
      ws.onerror = (e) => { console.error('WS error', e); ws.close(); };
    }

    // === Event listeners ===
    document.getElementById('add-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = document.getElementById('item-name');
      const name = input.value.trim();
      if (!name) return;
      if (await addItem(name)) input.value = '';
    });

    document.getElementById('spin-btn').addEventListener('click', () => {
      if (currentMode === 'elimination') spinElimination(); else spin();
    });
    document.getElementById('clear-btn').addEventListener('click', clearWheel);

    // === Test mode (superuser only) ===
    function showTestMsg(text, kind) {
      const el = document.getElementById('test-mode-msg'); if (!el) return;
      el.textContent = text; el.className = 'test-mode-msg ' + (kind || '');
      el.style.display = 'block';
      if (kind !== 'error') setTimeout(() => { el.style.display = 'none'; }, 4000);
    }

    async function startTestMode() {
      if (!confirm('Запустить тестовый режим колеса?\n\nВсе спины будут помечены как тестовые. Посты в Discord/TG пойдут с префиксом [TEST]. По завершении все тестовые данные будут удалены из БД.')) return;
      try {
        const r = await fetch('/api/wheel/test/start', { method: 'POST' });
        const data = await r.json();
        if (r.ok) { showTestMsg('✅ Тестовый режим запущен. Лог: ' + (data.log_path || ''), 'success'); setTimeout(() => location.reload(), 1500); }
        else showTestMsg(data.error || 'Не удалось запустить', 'error');
      } catch (e) { showTestMsg('Сетевая ошибка: ' + e.message, 'error'); }
    }

    async function endTestMode() {
      if (!confirm('Завершить тестовый режим?\n\nВСЕ тестовые записи будут удалены из БД. Лог-файл сохранится.')) return;
      try {
        const r = await fetch('/api/wheel/test/end', { method: 'POST' });
        const data = await r.json();
        if (r.ok) {
          const stats = data.cleanup_stats || {};
          showTestMsg(`✅ Тест завершён. Удалено: ${stats.wheel_items_deleted || 0} items, ${stats.winners_deleted || 0} winners.`, 'success');
          setTimeout(() => location.reload(), 3000);
        } else showTestMsg(data.error || 'Не удалось завершить', 'error');
      } catch (e) { showTestMsg('Сетевая ошибка: ' + e.message, 'error'); }
    }

    async function addTestItem() {
      const input = document.getElementById('test-item-name');
      const name = (input?.value || '').trim();
      if (!name) { showTestMsg('Введите название', 'error'); return; }
      try {
        const fd = new FormData(); fd.append('name', name);
        const r = await fetch('/api/wheel/test/add_item', { method: 'POST', body: fd });
        const data = await r.json();
        if (r.ok) { input.value = ''; showTestMsg(`✅ Добавлен: "${name}" (всего ${data.count})`, 'success'); }
        else showTestMsg(data.error || 'Ошибка', 'error');
      } catch (e) { showTestMsg('Сетевая ошибка: ' + e.message, 'error'); }
    }

    // Initial state
    items = typeof INITIAL_ITEMS !== 'undefined' ? INITIAL_ITEMS : [];
    renderList();
    connectWS();
