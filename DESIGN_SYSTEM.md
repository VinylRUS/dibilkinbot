# 🎨 DeeBeelkin Design System

Документ-справочник для разработки новых модулей. Описывает визуальный язык панели, CSS-переменные, компоненты и правила использования. Создан на основе Apple Liquid Glass HIG и Material Design 3.

---

## 1. Философия: 4 правила стекла

Панель использует glassmorphism, но **не на всех элементах**. Следуйте этим правилам:

### Правило 1: Стекло — это навигация, а не контент
`backdrop-filter: blur()` применяется **только** к функциональным слоям:
- Сайдбар (навигация)
- Status-bar (индикаторы подключений)
- Модальные окна (поверх контента)
- Toast-уведомления
- Dropdown-списки (custom-select-options)
- Иконки ачивок (ach-icon-wrap)

**Контентные карточки** (`article.card`, `.now-card`, `.quick-link`, `table`) — полупрозрачные без blur. Используют `--content-bg` вместо `--glass-bg`.

### Правило 2: Стекло не семплирует другое стекло
Не ставите стеклянную панель поверх другой стеклянной. Два соседних элемента с `backdrop-filter` дают мутный, некорректный блюр. Если внутри стеклянного контейнера нужны элементы — делайте их непрозрачными (`--content-bg`).

### Правило 3: Saturate + Inset Highlight
Стекло должно «сочить» цвет и «светиться» на краях:
```css
backdrop-filter: blur(20px) saturate(180%);
box-shadow: var(--glass-inset); /* inset 0 1px 0 rgba(255,255,255,0.18) */
```

### Правило 4: Мягкие радиусы
Стекло — «жидкое». Острые углы разрушают ощущение:
- Контентные карточки: `border-radius: 16px` (`--content-radius`)
- Модалки: `border-radius: 20px`
- Кнопки: `border-radius: 12px`
- Иконки ачивок: `border-radius: 50%`

---

## 2. Цветовая система

### Пчелиная тема (не менять!)
Цвета берутся из CSS-переменных в `:root`. Две темы: **dark** (по умолчанию) и **light**.

### Dark тема
| Переменная | Значение | Назначение |
|---|---|---|
| `--bg-base` | `#0F0F1A` | Базовый фон |
| `--bg-gradient` | `linear-gradient(135deg, #0F0F1A 0%, #1A1A2E 50%, #16213E 100%)` | Градиент body |
| `--accent` | `#FFB703` | Янтарный (основной акцент) |
| `--accent-2` | `#FFC940` | Светлый акцент |
| `--accent-hover` | `#FFD060` | Hover акцента |
| `--accent-soft` | `rgba(255, 183, 3, 0.12)` | Полупрозрачный акцент (подложки, glow) |
| `--accent-glow` | `0 0 24px rgba(255, 183, 3, 0.4)` | Свечение акцента |
| `--accent-text` | `#1A1A1A` | Текст на акцентном фоне |
| `--text` | `#FFFFFF` | Основной текст |
| `--text-muted` | `rgba(255, 255, 255, 0.6)` | Приглушённый текст |
| `--text-soft` | `rgba(255, 255, 255, 0.4)` | Мягкий текст |
| `--green` | `#6FAE5A` | Успех (Трутень) |
| `--red` | `#E55A4E` | Опасность (удаление) |
| `--gold` | `#FFD700` | Ачивки (gold) |
| `--purple` | `#B57BDC` | Ачивки (purple) |
| `--blue` | `#5DADE2` | Ачивки (blue) |

### Light тема
| Переменная | Значение |
|---|---|
| `--bg-base` | `#F4EAD3` |
| `--accent` | `#FB8500` |
| `--text` | `#2B2B2B` |
| `--glass-bg` | `rgba(255, 255, 255, 0.25)` |
| `--content-bg` | `rgba(255, 255, 255, 0.45)` |

### Декоративные фоновые blobs
`body::before` и `body::after` создают размытые цветные круги поверх градиента. Без них стекло «нечего преломлять». Не убирать!

---

## 3. CSS-переменные

### Glass (для навигации/модалок)
```css
--glass-bg: rgba(255, 255, 255, 0.06);        /* dark | light: 0.25 */
--glass-bg-hover: rgba(255, 255, 255, 0.12);   /* dark | light: 0.4 */
--glass-border: rgba(255, 255, 255, 0.12);    /* dark | light: 0.45 */
--glass-blur: blur(20px) saturate(180%);
--glass-inset: inset 0 1px 0 rgba(255, 255, 255, 0.18);
--glass-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
--glass-shadow-hover: 0 12px 40px rgba(0, 0, 0, 0.5);
```

### Content (для карточек/таблиц — без blur)
```css
--content-bg: rgba(255, 255, 255, 0.04);       /* dark | light: 0.45 */
--content-bg-hover: rgba(255, 255, 255, 0.08); /* dark | light: 0.6 */
--content-border: rgba(255, 255, 255, 0.08);   /* dark | light: 0.5 */
--content-radius: 16px;
```

### Анимации
```css
--spring: cubic-bezier(0.34, 1.56, 0.64, 1);   /* пружинный возврат */
--smooth: cubic-bezier(0.4, 0, 0.2, 1);       /* плавный */
```

### Мобильные
```css
@media (max-width: 768px) {
  --glass-blur: blur(12px) saturate(180%);  /* меньше blur на мобилках */
}
```

### Fallback
```css
@supports not (backdrop-filter: blur(1px)) {
  --glass-bg: rgba(40, 40, 55, 0.92);  /* почти непрозрачный */
}
```

---

## 4. Компоненты

### Карточка контентная (без blur)
```html
<article class="card">
  <h3>Заголовок</h3>
  <p>Содержимое</p>
</article>
```
```css
/* Использует --content-bg, --content-border, --content-radius */
/* НЕ использует backdrop-filter */
/* Hover: translateY(-3px) + glow */
```

### Карточка стеклянная (только для навигации/модалок)
```html
<div class="glass-panel">
  <!-- навигационный контент -->
</div>
```
```css
background: var(--glass-bg);
backdrop-filter: var(--glass-blur);
border: 1px solid var(--glass-border);
box-shadow: var(--glass-shadow), var(--glass-inset);
border-radius: 16px;
```

### Кнопки

| Класс | Назначение | Press |
|---|---|---|
| `.btn-primary` | Главная CTA | `scale(0.95)` + spring |
| `.btn-outline` | Вторичные действия | `scale(0.95)` |
| `.btn-danger` | Удаление | `scale(0.95)` |
| `.btn-ghost` | Редкие действия | `scale(0.95)` |
| `.btn-icon` | Иконки (✏️, ×) | `scale(0.90)` |

Все кнопки имеют:
- `transition: transform 0.2s var(--spring)`
- Ripple-эффект от точки клика (через JS `pointerdown` listener)
- `position: relative; overflow: hidden;` (для ripple)
- Hover: `translateY(-2px)` + `box-shadow` glow

### Модальные окна
```css
/* Анимация появления */
.grant-modal { animation: modal-pop-in 0.25s var(--spring); }
.grant-modal-content { animation: modal-scale-in 0.3s var(--spring); }
/* scale(0.85) → scale(1) с пружиной */
```

### Toast-уведомления
```css
/* Въезд справа с пружиной */
.toast { animation: toast-slide-in 0.3s var(--spring); }
/* Выезд плавно */
.toast.removing { animation: toast-slide-out 0.2s var(--smooth); }
```

### Иконки ачивок
```css
.ach-icon-wrap {
  background: var(--glass-bg);
  backdrop-filter: var(--glass-blur);
  border: 1.5px solid currentColor;  /* accent ring через currentColor */
  box-shadow: var(--glass-inset), inset 0 -1px 0 rgba(0,0,0,0.1);
  border-radius: 50%;
}
```
Цвет иконки задаётся через `style="color: var(--accent);"` — наследуется `currentColor` в SVG.

### Sidebar
- `position: fixed` (слева)
- `backdrop-filter: blur(20px) saturate(180%)`
- `::before` — radial-gradient сверху (спекулярный блик)
- Пункты `.sb-item`: hover → `translateX(2px)`, active → `scale(0.97)`

---

## 5. Анимации и микро-взаимодействия

### Press-down
Все кнопки: `:active { transform: scale(0.95); }` с `transition: transform 0.2s var(--spring)`

### Ripple
JS-генерация `.ripple-wave` span от точки клика через `pointerdown` event listener в `sidebar.html`.

### Stagger (каскад)
Списки появляются не одновременно:
```css
.achievements-list > *:nth-child(N) { animation-delay: N*30ms; }
```

### Hover-glow
```css
.achievement-card:hover {
  box-shadow: var(--glass-shadow-hover), var(--glass-inset), 0 0 20px var(--accent-soft);
}
```

### Focus ring (accessibility)
```css
*:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
```

---

## 6. Типографика

| Уровень | Размер | Вес | Line-height | Letter-spacing |
|---|---|---|---|---|
| h2 (заголовок страницы) | 1.3rem | 700 | 1.5 | 0 |
| h3 (секция) | 1.1rem | 700 | 1.5 | 0 |
| Body | 0.88rem | 400 | 1.6 | 0 |
| Label | 0.78rem | 600 | 1.5 | 0 |
| Mono (даты) | 0.76rem | 400 | 1.5 | 0 |

Шрифт: системный sans-serif (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`).

---

## 7. Роли

| Роль | Бейдж | Цвет |
|---|---|---|
| Матка (superuser) | `.sb-admin-badge` | `var(--accent)` (оранжевый) |
| Трутень (junior-admin) | `.sb-admin-badge` | `var(--green)` (зелёный) |
| Пчела (user) | нет бейджа | — |

---

## 8. Чего избегать

| Плохо | Почему |
|---|---|
| `backdrop-filter` на каждой карточке | Стекло ≠ контент (Apple Rule 1) |
| Стекло поверх стекла | Мутный блюр (Apple Rule 2) |
| `filter: blur()` вместо `backdrop-filter` | Размывает сам элемент, а не фон за ним |
| `ease-in-out` для нажатий | Нет «живого» отклика, используйте `--spring` |
| Ripple в центре кнопки | Ripple должен быть в точке касания |
| `border-radius` < 12px на панелях | Острые углы разрушают «жидкое стекло» |
| Неоновые цвета на стекле | Стекло нейтральное, цвет — от фона |
| `box-shadow` с blur > 24px | Слишком «мыльная» тень |

---

## 9. Структура файлов

```
static/
  style.css           — все CSS-переменные и компоненты
  icons/              — PNG иконки (liquid glass стиль)
  icons/custom/       — скачанные URL-иконки (персистентные)
templates/
  _footer.html        — глобальный футер (версия + commit ID)
  sidebar.html        — сайдбар + JS (toast, ripple, upgradeSelects, renderAchievementSVG)
  achievements.html   — конструктор ачивок + inline CSS
  profile_public.html — публичный профиль + inline CSS
  panel.html          — дашборд + inline CSS
  ...                 — остальные страницы
```

---

## 10. Ссылки

- [Apple Liquid Glass HIG](https://developer.apple.com/design/human-interface-guidelines/material)
- [Material Design 3 — Motion](https://m3.material.io/styles/motion/overview)
- [Glassmorphism: практическая выжимка](https://github.com/VinylRUS/dibilkinbot/blob/main/DESIGN_SYSTEM.md) (этот файл)
- Оригинальный research-файл от DeepSeek: `deepseek_markdown_20261008.md`
