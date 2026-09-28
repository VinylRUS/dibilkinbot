"""Парсер CHANGELOG.md — извлекает версию и список изменений."""
from __future__ import annotations

import re
from pathlib import Path
from dataclasses import dataclass


@dataclass
class ChangelogEntry:
    version: str
    sections: dict[str, list[str]]  # {"Новое": [...], "Исправлено": [...]}


def parse_changelog(filepath: str | Path = "CHANGELOG.md") -> list[ChangelogEntry]:
    """Распарсить CHANGELOG.md. Возвращает список записей (новые первыми)."""
    path = Path(filepath)
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    entries: list[ChangelogEntry] = []

    # Разбиваем по заголовкам ## vX.Y.Z
    blocks = re.split(r'(?=^##\s+v)', text, flags=re.MULTILINE)

    for block in blocks:
        # Ищем версию
        version_match = re.match(r'^##\s+(v[\d.]+)', block)
        if not version_match:
            continue
        version = version_match.group(1)

        # Парсим секции ### Заголовок
        sections: dict[str, list[str]] = {}
        section_parts = re.split(r'^###\s+', block, flags=re.MULTILINE)

        for part in section_parts[1:]:  # пропускаем первую часть (заголовок версии)
            lines = part.strip().split('\n')
            section_title = lines[0].strip()
            items = []
            for line in lines[1:]:
                line = line.strip()
                if line.startswith('- '):
                    items.append(line[2:].strip())
                elif line.startswith('• '):
                    items.append(line[2:].strip())
            if items:
                sections[section_title] = items

        entries.append(ChangelogEntry(version=version, sections=sections))

    return entries


def get_latest_version(filepath: str | Path = "CHANGELOG.md") -> str:
    """Возвращает последнюю версию из changelog (например 'v1.6.0')."""
    entries = parse_changelog(filepath)
    return entries[0].version if entries else "v0.0.0"


def get_latest_changelog(filepath: str | Path = "CHANGELOG.md") -> ChangelogEntry | None:
    """Возвращает запись последней версии."""
    entries = parse_changelog(filepath)
    return entries[0] if entries else None
