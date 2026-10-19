"""v2.4.0: Серверная физическая модель колеса.

Сервер генерирует начальные параметры физической симуляции,
рассчитывает траекторию и конечный угол, определяет победителя.
Frontend получает параметры и анимирует по ним — но НЕ определяет результат.

Формула wheelEase идентична JS-версии (static/js/wheel.js) —
проверено тестами на совпадение до 4 знака после запятой.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class SpinPhysics:
    """Параметры физической симуляции спина.

    Передаются клиенту через WS spin_started. Клиент анимирует
    по этим параметрам, используя ту же формулу wheelEase.
    """
    spin_id: str
    items_count: int
    duration_sec: float        # длительность анимации в секундах
    initial_rotation: float     # начальный угол колеса (радианы)
    total_rotation: float       # суммарный поворот за всю анимацию (радианы)
    final_rotation: float       # конечный угол = initial + total (радианы)
    winner_idx: int             # индекс сектора-победителя
    mode: str                   # 'classic' | 'elimination' | 'elimination_final'
    started_at_ts: float        # Unix timestamp начала (для синхронизации)


def wheel_ease(t: float) -> float:
    """Функция easing — идентична JS wheelEase() из static/js/wheel.js.

    3 фазы:
    - 0-20%: разгон (smoothstep)
    - 20-70%: плато (linear)
    - 70-100%: торможение (smoothstep)

    Производная непрерывна на стыках → нет рывков скорости.
    """
    ACCEL_END = 0.20
    PLATEAU_END = 0.70
    accel_area = ACCEL_END / 2
    plateau_area = PLATEAU_END - ACCEL_END
    decel_area = (1 - PLATEAU_END) / 2
    v_max = 1.0 / (accel_area + plateau_area + decel_area)

    if t < ACCEL_END:
        u = t / ACCEL_END
        u3 = u * u * u
        integral = u3 - u3 * u / 2
        return v_max * ACCEL_END * integral
    elif t < PLATEAU_END:
        accel_full = v_max * ACCEL_END * (1 - 0.5)
        return accel_full + v_max * (t - ACCEL_END)
    else:
        accel_full = v_max * ACCEL_END * 0.5
        plateau_full = v_max * (PLATEAU_END - ACCEL_END)
        u = (t - PLATEAU_END) / (1 - PLATEAU_END)
        u3 = u * u * u
        u4 = u3 * u
        integral = u - u3 + u4 / 2
        return accel_full + plateau_full + v_max * (1 - PLATEAU_END) * integral


def get_sector_under_pointer(rotation: float, items_count: int) -> int:
    """Вычислить индекс сектора под указателем для данного rotation.

    Указатель вверху (12 часов) = угол -π/2 в canvas системе.
    Возвращает -1 если items_count == 0.
    """
    if items_count == 0:
        return -1
    angle_per = (2 * math.pi) / items_count
    norm = (-math.pi / 2 - rotation) % (2 * math.pi)
    if norm < 0:
        norm += 2 * math.pi
    return int(norm // angle_per) % items_count


def generate_spin(
    spin_id: str,
    items_count: int,
    current_rotation: float,
    duration_sec: float,
    mode: str = "classic",
    started_at_ts: float = 0.0,
) -> SpinPhysics:
    """Сгенерировать параметры физического спина.

    Сервер определяет:
    1. Случайный суммарный поворот (5-7 оборотов + случайная доля)
    2. Конечный угол = current_rotation + total_rotation
    3. Winner = сектор под конечным углом

    Клиент получает эти параметры и анимирует от initial_rotation
    к final_rotation за duration_sec секунд, используя wheel_ease().

    Результат (winner_idx) определяется сервером — клиент не может
    его переопределить.
    """
    # 5-7 полных оборотов + случайная доля оборота
    extra_turns = 5 + random.randint(0, 2)
    random_fraction = random.random() * 2 * math.pi
    total_rotation = extra_turns * 2 * math.pi + random_fraction

    final_rotation = current_rotation + total_rotation
    winner_idx = get_sector_under_pointer(final_rotation, items_count)

    return SpinPhysics(
        spin_id=spin_id,
        items_count=items_count,
        duration_sec=duration_sec,
        initial_rotation=current_rotation,
        total_rotation=total_rotation,
        final_rotation=final_rotation,
        winner_idx=winner_idx,
        mode=mode,
        started_at_ts=started_at_ts,
    )


def physics_to_dict(physics: SpinPhysics) -> dict:
    """Сериализация для WS/JSON."""
    return asdict(physics)
