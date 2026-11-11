// v3.9.0: Тур по вебпанели dibilkinbot.
// Driver.js загружается через _tour_banner.html (только на dashboard /).
// Логика:
//   - localStorage 'deebeelkin_tour_dismissed_v1' = true только при "Больше не показывать"
//   - sessionStorage 'deebeelkin_tour_hidden_v1' = true при close (×) — вернётся при reload
//   - Mobile (< 768px) — баннер не показывается
//   - Skip в Driver.js — прерывает тур, баннер остаётся (по требованию)
//   - Завершение тура — баннер остаётся (только "Больше не показывать" убирает)

(function() {
  'use strict';

  const DISMISS_KEY = 'deebeelkin_tour_dismissed_v1';
  const HIDDEN_KEY = 'deebeelkin_tour_hidden_v1';

  function isMobile() {
    return window.innerWidth < 768;
  }

  function shouldShowBanner() {
    if (isMobile()) return false;
    if (localStorage.getItem(DISMISS_KEY) === 'true') return false;
    if (sessionStorage.getItem(HIDDEN_KEY) === 'true') return false;
    return true;
  }

  function showBanner() {
    const banner = document.getElementById('tour-banner');
    if (!banner) return;
    banner.style.display = 'flex';
    // v3.9.0: arrow hint — если баннер вне viewport (юзер скроллил), показываем стрелку
    checkArrowHint();
  }

  function hideBanner() {
    const banner = document.getElementById('tour-banner');
    if (banner) banner.style.display = 'none';
    hideArrowHint();
  }

  function checkArrowHint() {
    const banner = document.getElementById('tour-banner');
    const arrow = document.getElementById('tour-arrow-hint');
    if (!banner || !arrow) return;
    if (banner.style.display === 'none') {
      arrow.style.display = 'none';
      return;
    }
    const rect = banner.getBoundingClientRect();
    // Если баннер не виден (выше viewport) — показываем стрелку-указатель
    if (rect.bottom < 80) {
      arrow.style.display = 'block';
    } else {
      arrow.style.display = 'none';
    }
  }

  function hideArrowHint() {
    const arrow = document.getElementById('tour-arrow-hint');
    if (arrow) arrow.style.display = 'none';
  }

  // === Exposed globally для inline onclick в _tour_banner.html ===
  window.hideTourBannerSession = function() {
    // Close (×) — скрываем на текущую сессию, вернётся при reload
    sessionStorage.setItem(HIDDEN_KEY, 'true');
    hideBanner();
  };

  window.dismissTourBannerForever = function() {
    // "Больше не показывать" — навсегда (localStorage)
    localStorage.setItem(DISMISS_KEY, 'true');
    sessionStorage.removeItem(HIDDEN_KEY);
    hideBanner();
  };

  window.startDeebeelkinTour = function() {
    // v3.9.0: Driver.js 1.3.1 IIFE сборка — функция driver() живёт на
    // window.driver.js.driver (НЕ window.driver.driver).
    // IIFE оборачивает модуль: this.driver.js = function(D){...}({})
    // → результат вызова (объект {driver: ke, [Symbol.toStringTag]: "Module"})
    // присваивается this.driver.js.
    const driverModule = (window.driver && window.driver.js) || window.driver;
    if (!driverModule || typeof driverModule.driver !== 'function') {
      console.error('Driver.js not loaded or API mismatch', {
        hasDriver: !!window.driver,
        hasDriverJs: !!(window.driver && window.driver.js),
        driverType: typeof (window.driver && window.driver.driver),
        driverJsDriverType: typeof (window.driver && window.driver.js && window.driver.js.driver),
      });
      alert('Тур не загрузился (Driver.js). Обнови страницу и попробуй снова.');
      return;
    }
    // Скрываем баннер на время тура
    hideBanner();

    const steps = buildTourSteps();
    if (steps.length === 0) {
      console.warn('Tour: no steps');
      return;
    }

    // Мапим в формат Driver.js 1.3.x — шаг без element = центрированный popover
    // без highlight (используется для приветствия).
    const mappedSteps = steps.map(s => {
      const step = {
        popover: {
          title: s.title,
          description: s.description,
          side: s.side || 'bottom',
          align: s.align || 'start',
        },
      };
      if (s.element) {
        step.element = s.element;
      }
      return step;
    });

    const driverObj = driverModule.driver({
      showProgress: true,
      animate: true,
      allowClose: true,
      overlayOpacity: 0.75,
      smoothScroll: true,
      stagePadding: 8,
      stageRadius: 8,
      nextBtnText: 'Далее →',
      prevBtnText: '← Назад',
      doneBtnText: 'Завершить ✓',
      onDestroy: () => {
        // После закрытия тура (любым способом) — баннер НЕ показываем снова
        // (по требованию: только "Больше не показывать" убирает навсегда).
        // sessionStorage HIDDEN_KEY не ставим — баннер вернётся при reload.
      },
    });

    driverObj.setSteps(mappedSteps);
    driverObj.drive();
  };

  function buildTourSteps() {
    // Шаги depend от роли (Матка видит больше) и от того что есть на странице.
    // Используем CSS-селекторы (sidebar links) — не требуем ID.
    const steps = [];
    const isSuperuser = (typeof IS_SUPERUSER !== 'undefined') && IS_SUPERUSER;
    const isAdmin = (typeof IS_ADMIN !== 'undefined') && IS_ADMIN;

    // Шаг 1: приветствие — БЕЗ element (центрированный popover без highlight).
    // Раньше было `element: '#tour-banner'` но мы скрываем баннер перед туром,
    // и Driver.js не может подсветить скрытый элемент.
    steps.push({
      element: null,
      title: '🐝 Привет!',
      description: 'Короткий тур по панели — 6 шагов, ~1 минута. Жми «Далее» чтобы продолжить, или «Завершить» чтобы прервать.',
      side: 'bottom',
      align: 'center',
    });

    // Шаг 2: Киновечер
    const movienightLink = document.querySelector('.sb-item[href="/movienight"]');
    if (movienightLink) {
      steps.push({
        element: movienightLink,
        title: '🎬 Киновечер',
        description: 'Тут собирают фильмы для колеса. Каждый юзер добавляет желаемое из списка просмотренного или вручную. Когда все готовы — админ запускает колесо.',
        side: 'right',
        align: 'center',
      });
    }

    // Шаг 3: Колесо
    const wheelLink = document.querySelector('.sb-item[href="/wheel"]');
    if (wheelLink) {
      steps.push({
        element: wheelLink,
        title: '🎡 Колесо фильмов',
        description: 'Случайный выбор фильма для просмотра. Слева — лоты, внизу кнопка «КРУТИТЬ». В центре можно поставить гифку — Матка загружает, остальные выбирают из библиотеки.',
        side: 'right',
        align: 'center',
      });
    }

    // Шаг 4: Bell с ачивками
    const bell = document.querySelector('.sb-bell');
    if (bell) {
      steps.push({
        element: bell,
        title: '🔔 Ачивки',
        description: 'За активность — рейтинги фильмов, цитаты, время в войсе — даём ачивки. Колокольчик показывает сколько новых. Клик — переход к списку.',
        side: 'bottom',
        align: 'center',
      });
    }

    // Шаг 5: Аватарка → профиль
    const avatar = document.querySelector('.sb-user-avatar');
    if (avatar) {
      steps.push({
        element: avatar,
        title: '👤 Профиль',
        description: 'Настройки профиля — уведомления, Steam, пароль — открываются через клик на аватарку в нижнем углу sidebar. Не самое очевидное решение, зато лаконичное.',
        side: 'top',
        align: 'center',
      });
    }

    // Шаг 6 (для админов): Каналы
    if (isAdmin) {
      const channelsLink = document.querySelector('.sb-item[href="/channels"]');
      if (channelsLink) {
        steps.push({
          element: channelsLink,
          title: '🔧 Каналы',
          description: 'ID Discord каналов куда бот постит — ачивки, цитаты, победители колеса, обновления. Тут же настраивается канал для анонсов ачивок (новое в v3.6.0).',
          side: 'right',
          align: 'center',
        });
      }
    }

    // Шаг 7 (для Матки): Юзеры
    if (isSuperuser) {
      const usersLink = document.querySelector('.sb-item[href="/users"]');
      if (usersLink) {
        steps.push({
          element: usersLink,
          title: '👥 Юзеры',
          description: 'Управление ролями: Пчела ↔ Трутень. Матка управляется только через env (ADMIN_DISCORD_ID). Если не получается понизить Трутня — проверь .env.',
          side: 'right',
          align: 'center',
        });
      }
    }

    return steps;
  }

  // === Bootstrap ===
  function init() {
    if (shouldShowBanner()) {
      showBanner();
    }
    // Scroll listener — обновляем arrow hint
    window.addEventListener('scroll', checkArrowHint, { passive: true });
    window.addEventListener('resize', () => {
      // Если изменился размер — перепроверяем показ
      if (shouldShowBanner()) {
        const banner = document.getElementById('tour-banner');
        if (banner && banner.style.display === 'none') {
          showBanner();
        }
      } else {
        hideBanner();
      }
      checkArrowHint();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
