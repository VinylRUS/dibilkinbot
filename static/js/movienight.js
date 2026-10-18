    // === Состояние ===
    const maxPerUser = MAX_PER_USER;
    const isOrganizer = IS_ORGANIZER;
    const hasCollection = HAS_COLLECTION;
    let selectedIds = new Set([
      ...USER_PICK_IDS
    ]);
    let userIsReady = USER_IS_READY;

    // === WebSocket ===
    let ws = null;
    let wsPingTimer = null;
    let wsWasClosed = false;
    function connectWS() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      // ВАЖНО: guild_id должен совпадать с гильдией сессии, иначе события
      // collection_* (рассылаются по guild_id) сюда не дойдут.
      ws = new WebSocket(`${proto}://${location.host}/ws/wheel?guild_id=${typeof GUILD_ID !== 'undefined' ? GUILD_ID : 0}`);
      ws.onopen = () => {
        // Heartbeat: прокси часто режут «молчащие» WS через ~60 сек
        clearInterval(wsPingTimer);
        wsPingTimer = setInterval(() => { try { ws.send('ping'); } catch (e) {} }, 25000);
        // После переподключения могли пропустить события — досинхронизируемся
        if (wsWasClosed && hasCollection) refreshParticipants();
        wsWasClosed = false;
        const el = document.getElementById('ws-status');
        if (el) {
          el.textContent = '● live';
          el.className = 'ws-status online';
        }
      };
      ws.onclose = () => {
        clearInterval(wsPingTimer);
        wsWasClosed = true;
        const el = document.getElementById('ws-status');
        if (el) {
          el.textContent = '● offline';
          el.className = 'ws-status offline';
        }
        // Переподключение через 3 сек
        setTimeout(connectWS, 3000);
      };
      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        handleWSMessage(msg);
      };
    }

    function handleWSMessage(msg) {
      if (!hasCollection) return;
      switch (msg.type) {
        case 'collection_started':
          // Сбор начат (кем-то другим) — перезагружаем
          location.reload();
          break;
        case 'collection_participant_joined':
          refreshParticipants();
          break;
        case 'collection_participant_ready':
          updateParticipantReady(msg.payload.participant.user_discord_id, msg.payload.participant.is_ready);
          break;
        case 'collection_picks_updated':
          // Обновляем счётчик picks у конкретного юзера
          refreshParticipants();
          break;
        case 'collection_participant_kicked':
          // Если кикнули нас — перезагружаем
          if (msg.payload.user_discord_id === USER_DISCORD_ID) {
            alert('Вас кикнули из сбора.');
            location.reload();
          } else {
            refreshParticipants();
          }
          break;
        case 'collection_completed':
          // Сбор завершён — фильмы загружены в колесо. Показываем кнопку «Перейти к колесу».
          // Никакого авто-редиректа — организатор сам решает когда крутить.
          showCollectionCompleted(msg.payload.picks_count);
          break;
        case 'collection_cancelled':
          alert('❌ Сбор отменён организатором.');
          location.reload();
          break;
      }
    }

    async function refreshParticipants() {
      try {
        const r = await fetch('/api/collection/status');
        const data = await r.json();
        if (!data.active) {
          location.reload();
          return;
        }
        // Обновляем sidebar
        const list = document.getElementById('participants-list');
        if (!list) return;
        let html = '';
        let readyCount = 0;
        let totalPicks = 0;
        for (const p of data.participants) {
          const isReady = p.is_ready && !p.kicked_at;
          const isKicked = !!p.kicked_at;
          if (isReady) readyCount++;
          totalPicks += p.picks_count || 0;
          const displayName = p.display_name || `User#${p.user_discord_id}`;
          const avatarHtml = p.avatar_url
            ? `<img src="${p.avatar_url}" alt="">`
            : '👤';
          const meta = isKicked ? 'кикнут' : (isReady ? '✓ готов' : '⏳ выбирает...');
          const statusClass = isReady ? 'ready' : (isKicked ? 'kicked' : 'waiting');
          const kickBtn = (isOrganizer && !isKicked && p.user_discord_id !== COLLECTION_STARTED_BY)
            ? `<button type="button" class="participant-kick" onclick="kickParticipant(${p.user_discord_id})">✕</button>`
            : '';
          html += `
            <div class="participant-row ${isKicked ? 'kicked' : ''} ${isReady ? 'ready' : ''}" data-user-id="${p.user_discord_id}">
              <div class="participant-avatar">${avatarHtml}</div>
              <div class="participant-info">
                <div class="participant-name">${displayName}</div>
                <div class="participant-meta">${meta} · ${p.picks_count || 0} фильмов</div>
              </div>
              <div class="participant-status ${statusClass}"></div>
              ${kickBtn}
            </div>`;
        }
        list.innerHTML = html;
        // Обновляем stats
        const statParticipants = document.getElementById('stat-participants');
        const statReady = document.getElementById('stat-ready');
        const statPicks = document.getElementById('stat-picks');
        if (statParticipants) statParticipants.textContent = data.participants.length;
        if (statReady) statReady.textContent = readyCount;
        if (statPicks) statPicks.textContent = totalPicks;
        // Кнопка start-spin: активна только если все готовы
        const startSpinBtn = document.getElementById('start-spin-btn');
        if (startSpinBtn) {
          const allReady = readyCount === data.participants.filter(p => !p.kicked_at).length;
          startSpinBtn.disabled = !allReady;
          if (allReady) {
            startSpinBtn.textContent = '🎡 Начать крутку колеса';
          } else {
            startSpinBtn.textContent = `Ожидают готовности: ${data.participants.filter(p => !p.kicked_at).length - readyCount}`;
          }
        }
      } catch (e) {
        console.error('refreshParticipants failed:', e);
      }
    }

    function updateParticipantReady(userId, isReady) {
      const row = document.querySelector(`.participant-row[data-user-id="${userId}"]`);
      if (!row) return;
      row.dataset.isReady = isReady;
      row.classList.toggle('ready', isReady);
      const status = row.querySelector('.participant-status');
      const meta = row.querySelector('.participant-meta');
      if (isReady) {
        status.className = 'participant-status ready';
        meta.textContent = '✓ готов';
      } else {
        status.className = 'participant-status waiting';
        meta.textContent = '⏳ выбирает...';
      }
      refreshParticipants(); // пересчитать счётчики
    }

    // === Чекбоксы выбора ===
    function togglePick(watchlistId) {
      if (selectedIds.has(watchlistId)) {
        selectedIds.delete(watchlistId);
      } else {
        if (selectedIds.size >= maxPerUser) {
          alert(`Можно выбрать только ${maxPerUser} фильм(ов). Снимите один из выбранных чтобы выбрать другой.`);
          return;
        }
        selectedIds.add(watchlistId);
      }
      // Обновляем UI
      document.querySelectorAll('.pick-item').forEach(el => {
        const id = parseInt(el.dataset.watchlistId);
        el.classList.toggle('checked', selectedIds.has(id));
      });
      updateProgressUI();
    }

    function updateProgressUI() {
      const count = selectedIds.size;
      const countEl = document.getElementById('picks-count');
      if (countEl) countEl.textContent = count;
      const progressEl = document.getElementById('picks-progress');
      if (progressEl) {
        progressEl.className = 'progress-value ' + (count === maxPerUser ? 'complete' : (count > maxPerUser ? 'over' : ''));
      }
      const readyBtn = document.getElementById('ready-btn');
      if (readyBtn) {
        if (count === maxPerUser) {
          readyBtn.className = 'ready-btn ready';
          readyBtn.textContent = '✓ Готов! (нажмите чтобы сохранить и ждать других)';
        } else {
          readyBtn.className = 'ready-btn';
          readyBtn.textContent = `Выберите ещё ${maxPerUser - count} фильм(ов)`;
        }
      }
    }

    async function toggleReady() {
      if (selectedIds.size !== maxPerUser) {
        alert(`Нужно выбрать ровно ${maxPerUser} фильм(ов). Сейчас: ${selectedIds.size}.`);
        return;
      }
      // Сохраняем picks
      const idsStr = Array.from(selectedIds).join(',');
      const formData = new FormData();
      formData.append('watchlist_ids', idsStr);
      try {
        const r = await fetch('/api/collection/save_picks', { method: 'POST', body: formData });
        const data = await r.json();
        if (r.ok) {
          userIsReady = true;
          updateProgressUI();
          // Опционально: alert что готов
        } else {
          alert(data.error || 'Ошибка сохранения');
        }
      } catch (e) {
        alert('Сетевая ошибка: ' + e.message);
      }
    }

    // === Organizer actions ===
    async function startCollection() {
      const maxInput = document.getElementById('max_per_user');
      const max = Math.max(1, Math.min(10, parseInt(maxInput.value) || 5));
      const btn = event.target;
      btn.disabled = true;
      btn.textContent = '⏳ Запускаю...';
      const formData = new FormData();
      formData.append('max_per_user', max);
      try {
        const r = await fetch('/api/collection/start', { method: 'POST', body: formData });
        const data = await r.json();
        if (r.ok) {
          location.reload();
        } else {
          alert(data.error || 'Ошибка');
          btn.disabled = false;
          btn.textContent = '🎬 Начать сбор';
        }
      } catch (e) {
        alert('Сетевая ошибка: ' + e.message);
        btn.disabled = false;
        btn.textContent = '🎬 Начать сбор';
      }
    }

    async function startSpin(force) {
      const btn = event.target;
      const confirmMsg = force
        ? 'Запустить крутку принудительно? Не все участники готовы.'
        : 'Запустить крутку колеса? Все выбранные фильмы попадут в колесо (в случайном порядке).';
      if (!confirm(confirmMsg)) return;
      btn.disabled = true;
      btn.textContent = '⏳ Загружаю в колесо...';
      const formData = new FormData();
      formData.append('force', force);
      try {
        const r = await fetch('/api/collection/start_spin', { method: 'POST', body: formData });
        const data = await r.json();
        if (r.ok) {
          // Показываем кнопку «Перейти к колесу» вместо автоперехода
          showCollectionCompleted(data.picks_count);
        } else {
          alert(data.error || 'Ошибка');
          btn.disabled = false;
          btn.textContent = force ? '⚡ Начать принудительно' : '🎡 Начать крутку колеса';
        }
      } catch (e) {
        alert('Сетевая ошибка: ' + e.message);
        btn.disabled = false;
        btn.textContent = force ? '⚡ Начать принудительно' : '🎡 Начать крутку колеса';
      }
    }

    function showCollectionCompleted(picksCount) {
      // Заменяем organizer controls на блок «Перейти к колесу»
      const orgControls = document.querySelector('.organizer-controls');
      if (!orgControls) {
        // Обычный участник: organizer-controls нет — показываем баннер сверху
        const slot = document.getElementById('wheel-ready-slot');
        if (slot && !slot.hasChildNodes()) {
          slot.innerHTML = `
            <div class="wheel-ready-banner">
              <span>✅ Сбор завершён — в колесе ${picksCount} фильм(ов).</span>
              <a href="/wheel" class="org-btn" style="text-decoration:none;display:inline-block;">🎡 Перейти к колесу</a>
            </div>`;
        }
      }
      if (orgControls) {
        orgControls.innerHTML = `
          <h4>✅ Сбор завершён</h4>
          <p style="font-size: 0.85rem; margin: 0 0 14px; color: var(--text-muted);">
            Фильмы загружены в колесо (${picksCount} шт.). Перейдите к колесу чтобы крутить.
          </p>
          <a href="/wheel" class="org-btn" style="text-decoration: none; display: block; text-align: center;">
            🎡 Перейти к колесу
          </a>
        `;
      }
      // Скрываем прогресс-бар и список выбора (сбор завершён)
      const progressBar = document.querySelector('.progress-bar');
      if (progressBar) progressBar.style.display = 'none';
      const pickList = document.getElementById('pick-list');
      if (pickList) pickList.style.display = 'none';
      const readyBtn = document.getElementById('ready-btn');
      if (readyBtn) readyBtn.style.display = 'none';
    }

    async function cancelCollection() {
      if (!confirm('Отменить сбор? Все выборы будут утеряны, фильмы останутся в вишлистах.')) return;
      try {
        const r = await fetch('/api/collection/cancel', { method: 'POST' });
        if (r.ok) {
          location.reload();
        }
      } catch (e) {
        alert('Ошибка: ' + e.message);
      }
    }

    async function kickParticipant(userId) {
      if (!confirm('Кикнуть участника? Его выборы будут удалены, фильмы останутся в вишлисте.')) return;
      const formData = new FormData();
      formData.append('target_user_id', userId);
      try {
        const r = await fetch('/api/collection/kick', { method: 'POST', body: formData });
        if (r.ok) {
          refreshParticipants();
        } else {
          const data = await r.json();
          alert(data.error || 'Ошибка');
        }
      } catch (e) {
        alert('Сетевая ошибка: ' + e.message);
      }
    }

    function copyLink() {
      const linkEl = document.getElementById('collect-link');
      if (!linkEl) return;
      const text = linkEl.textContent.trim();
      navigator.clipboard.writeText(text).then(() => {
        alert('Ссылка скопирована: ' + text);
      }).catch(() => {
        prompt('Скопируйте вручную:', text);
      });
    }

    // === Init ===
    if (hasCollection) {
      connectWS();
      // Polling каждые 10 сек как backup (если WS упал)
      setInterval(refreshParticipants, 10000);
      // Обновить ссылку на collect
      fetch('/api/collection/status').then(r => r.json()).then(data => {
        if (data.active) {
          const linkEl = document.getElementById('collect-link');
          // Мы не знаем panel_base_url на клиенте, но можем взять location.origin
          if (linkEl) {
            linkEl.textContent = `${location.origin}/collect/${data.collection.token}`;
          }
        }
      });
    }
