"""v3.9.0: Тесты тура по вебпанели.

Покрывает:
- Dashboard рендерит _tour_banner partial
- SSR переменные IS_SUPERUSER/IS_ADMIN корректны для разных ролей
- Driver.js + tour.js + driver.min.css подключены
- Mobile gate (< 768px) — через CSS media query (display:none)
- localStorage logic — dismissed/hidden keys

Backend логики нет (всё на frontend), поэтому тесты только на render.
"""
from __future__ import annotations

import pytest


def test_dashboard_includes_tour_banner(client):
    """Dashboard / рендерит tour banner с кнопками."""
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'id="tour-banner"' in html, "tour-banner div missing"
    assert 'Посмотреть тур' in html, "primary button missing"
    assert 'Больше не показывать' in html, "dismiss button missing"
    # Driver.js подключен
    assert '/static/vendor/driver.min.js' in html, "Driver.js script missing"
    assert '/static/vendor/driver.min.css' in html, "Driver.js CSS missing"
    assert '/static/js/tour.js' in html, "tour.js missing"


def test_dashboard_ssr_superuser_vars(client):
    """conftest client — is_superuser=True. SSR переменные должны быть true."""
    r = client.get("/")
    html = r.text
    assert 'IS_SUPERUSER = true' in html, "IS_SUPERUSER should be true for conftest superuser"
    assert 'IS_ADMIN = true' in html, "IS_ADMIN should be true"


def test_dashboard_ssr_regular_user(client):
    """Обычный юзер — IS_SUPERUSER=false, IS_ADMIN=false."""
    import deps
    # Сохраним original cookie чтобы восстановить для других тестов
    original_cookie = client.cookies.get("session")
    try:
        token = deps.create_session({
            "discord_id": 9999,
            "username": "regular",
            "display_name": "Regular",
            "is_admin": False,
            "is_superuser": False,
            "role": "user",
            "current_guild_id": 1515661305011376209,
        })
        client.cookies.set("session", token)
        r = client.get("/")
        html = r.text
        assert 'IS_SUPERUSER = false' in html, "IS_SUPERUSER should be false for regular user"
        assert 'IS_ADMIN = false' in html, "IS_ADMIN should be false"
    finally:
        # Восстановим original superuser cookie чтобы не ломать последующие тесты
        if original_cookie:
            client.cookies.set("session", original_cookie)


def test_tour_banner_hidden_on_mobile_css(client):
    """CSS media query скрывает баннер на мобиле (< 768px)."""
    r = client.get("/")
    html = r.text
    # CSS должен содержать media query max-width: 768px с display:none для .tour-banner
    assert '@media (max-width: 768px)' in html
    assert '.tour-banner { display: none' in html or '.tour-banner{display:none' in html or \
           '.tour-banner { display: none !important' in html, \
           "mobile gate CSS missing"


def test_tour_banner_dismiss_button_present(client):
    """Кнопка 'Больше не показывать' — записывает в localStorage (tour.js)."""
    r = client.get("/")
    html = r.text
    assert 'dismissTourBannerForever' in html, "dismissTourBannerForever function missing"
    assert 'hideTourBannerSession' in html, "hideTourBannerSession function missing"


def test_tour_arrow_hint_removed(client):
    """v3.10.0: arrow hint убран — анимация не нужна, sidebar свёрнут по умолчанию."""
    r = client.get("/")
    html = r.text
    assert 'id="tour-arrow-hint"' not in html, "tour-arrow-hint should be removed in v3.10.0"
    assert 'tour-arrow-bounce' not in html, "arrow animation should be removed"
    assert 'tour-banner-pulse' not in html, "banner pulse animation should be removed"
    assert 'tour-bee-bounce' not in html, "bee bounce animation should be removed"


def test_driver_js_vendor_files_exist():
    """Driver.js vendor файлы скачаны в static/vendor/."""
    from pathlib import Path
    vendor = Path(__file__).resolve().parent.parent / "static" / "vendor"
    assert (vendor / "driver.min.js").exists(), "driver.min.js missing"
    assert (vendor / "driver.min.css").exists(), "driver.min.css missing"
    # Размеры — не пустые
    assert (vendor / "driver.min.js").stat().st_size > 5000, "driver.min.js too small"
    assert (vendor / "driver.min.css").stat().st_size > 1000, "driver.min.css too small"


def test_tour_js_loaded_only_on_dashboard(client):
    """tour.js подключается ТОЛЬКО на dashboard / — не на других страницах."""
    # На /wheel — tour.js не должен быть подключён
    r = client.get("/wheel")
    html = r.text
    assert '/static/js/tour.js' not in html, "tour.js should NOT be loaded on /wheel"
    assert '/static/vendor/driver.min.js' not in html, "Driver.js should NOT be loaded on /wheel"
