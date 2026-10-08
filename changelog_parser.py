"""Парсер CHANGELOG.md — извлекает версию и список изменений.

Поддерживает 2 формата:

1. **Новый формат (v2.3.2+)** — теги-секции:
   ```
   ## v2.3.2 (62fa9ea)

   #НОВЫЕ_ФИЧИ#
   - текст для всех юзеров

   #АДМИНСКИЕ_ФИЧИ#
   - текст только для админов (отдельным сообщением)

   #ТЕХНИЧЕСКАЯ_ИНФОРМАЦИЯ#
   - технические детали (не постится никуда, только для разработчика)
   ```

2. **Старый формат** — `### Новое` / `### Исправлено` / `### Улучшения`:
   ```
   ## v2.3.1
   ### Новое
   - ...
   ### Исправлено
   - ...
   ```
"""
from __future__ import annotations

import re
import logging
from pathlib import Path
from dataclasses import dataclass, field

log = logging.getLogger("changelog")


# Соответствие тег-секций и ключей в ChangelogEntry.sections
# Используем каноничные человекочитаемые ключи чтобы совместить оба формата.
TAG_TO_KEY = {
    "#НОВЫЕ_ФИЧИ#": "Новое",
    "#АДМИНСКИЕ_ФИЧИ#": "Админские фичи",
    "#ТЕХНИЧЕСКАЯ_ИНФОРМАЦИЯ#": "Техническая информация",
}

# Секции которые НЕ должны попадать в публичный анон (общий канал)
NON_PUBLIC_SECTIONS = {"Админские фичи", "Техническая информация"}

# Секции которые отправляются отдельным сообщением в админский чат
ADMIN_SECTIONS = {"Админские фичи"}


@dataclass
class ChangelogEntry:
    version: str
    commit_hash: str | None = None  # v2.3.2+ — хэш коммита из заголовка
    sections: dict[str, list[str]] = field(default_factory=dict)
    # {"Новое": [...], "Админские фичи": [...], "Техническая информация": [...]}
    # Для старых записей: {"Новое": [...], "Исправлено": [...]}

    @property
    def public_sections(self) -> dict[str, list[str]]:
        """Секции которые можно публиковать в общий канал (без админских и технических)."""
        return {k: v for k, v in self.sections.items() if k not in NON_PUBLIC_SECTIONS}

    @property
    def admin_sections(self) -> dict[str, list[str]]:
        """Секции которые отправляются отдельным сообщением в админский чат."""
        return {k: v for k, v in self.sections.items() if k in ADMIN_SECTIONS}


def _parse_tag_format(block: str) -> dict[str, list[str]]:
    """Парсинг нового формата с тегами #НОВЫЕ_ФИЧИ# / #АДМИНСКИЕ_ФИЧИ# / #ТЕХНИЧЕСКАЯ_ИНФОРМАЦИЯ#.

    Возвращает dict {section_key: [items]} где section_key — каноничное имя ('Новое', 'Админские фичи', etc.).
    """
    sections: dict[str, list[str]] = {}
    # Разбиваем блок по тегам #...#
    # Паттерн: ищем #TAG# в начале строки (с опциональными пробелами)
    tag_pattern = re.compile(r'^#\s*([А-ЯA-Z_]+)\s*#', re.MULTILINE)
    splits = tag_pattern.split(block)

    # splits = [pre_text, tag1, content1, tag2, content2, ...]
    # pre_text — текст до первого тега (обычно пусто или пустая строка после заголовка версии)
    i = 1
    while i < len(splits) - 1:
        tag_raw = splits[i].strip()
        content = splits[i + 1]
        # Полный тег с диезами
        full_tag = f"#{tag_raw}#"
        section_key = TAG_TO_KEY.get(full_tag)
        if not section_key:
            # Неизвестный тег — пропускаем
            i += 2
            continue
        # Парсим элементы списка (строки начинающиеся с - или •)
        items = []
        for line in content.split("\n"):
            line = line.rstrip()
            if line.startswith("- "):
                items.append(line[2:].strip())
            elif line.startswith("• "):
                items.append(line[2:].strip())
            elif re.match(r"^\s{2,}\*", line):
                # вложенные пункты типа "  * под-пункт"
                items.append(line.strip().lstrip("*").strip())
        if items:
            sections[section_key] = items
        i += 2
    return sections


def _parse_legacy_format(block: str) -> dict[str, list[str]]:
    """Парсинг старого формата ### Заголовок / - item."""
    sections: dict[str, list[str]] = {}
    section_parts = re.split(r'^###\s+', block, flags=re.MULTILINE)
    for part in section_parts[1:]:
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
    return sections


def parse_changelog(filepath: str | Path = "CHANGELOG.md") -> list[ChangelogEntry]:
    """Распарсить CHANGELOG.md. Возвращает список записей (новые первыми)."""
    path = Path(filepath)
    if not path.exists():
        log.error("CHANGELOG.md not found at: %s", path.absolute())
        return []

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        log.error("CHANGELOG.md is empty at: %s", path.absolute())
        return []

    entries: list[ChangelogEntry] = []

    # Разбиваем по заголовкам ## vX.Y.Z
    blocks = re.split(r'(?=^##\s+v)', text, flags=re.MULTILINE)

    for block in blocks:
        # Ищем версию: ## v2.3.2  или  ## v2.3.2 (62fa9ea)
        version_match = re.match(r'^##\s+(v[\d.]+)(?:\s*\(([0-9a-f]+)\))?', block)
        if not version_match:
            continue
        version = version_match.group(1)
        commit_hash = version_match.group(2)  # может быть None для старых записей

        # Пробуем новый формат (теги) — если есть хотя бы один тег
        has_tags = bool(re.search(r'^#\s*[А-ЯA-Z_]+\s*#', block, re.MULTILINE))
        if has_tags:
            sections = _parse_tag_format(block)
        else:
            sections = _parse_legacy_format(block)

        entries.append(ChangelogEntry(version=version, commit_hash=commit_hash, sections=sections))
        log.debug("Parsed version %s (hash=%s) with %d sections",
                  version, commit_hash, len(sections))

    log.info("Parsed %d changelog entries from %s", len(entries), path.name)
    return entries


def get_latest_version(filepath: str | Path = "CHANGELOG.md") -> str:
    """Возвращает последнюю версию из changelog (например 'v2.3.2')."""
    entries = parse_changelog(filepath)
    return entries[0].version if entries else "v0.0.0"


def get_latest_changelog(filepath: str | Path = "CHANGELOG.md") -> ChangelogEntry | None:
    """Возвращает запись последней версии."""
    entries = parse_changelog(filepath)
    return entries[0] if entries else None
