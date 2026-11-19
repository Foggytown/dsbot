"""Bot-authored text, per locale.

Every user-visible string is overridable from the database (`templates` table), so a
Discord admin can reword warnings without a deploy. These are the seed defaults only.
"""

from __future__ import annotations

from typing import Final

DEFAULT_LOCALE: Final = "ru"
SUPPORTED_LOCALES: Final[tuple[str, ...]] = ("en", "ru")

# Top-level bot message for an invalid report.
REPORT_ISSUES: Final[dict[str, str]] = {
    "ru": (
        "{mention}, в рапорте есть ошибки формы:\n\n{lines}\n\n"
        "Исправьте сообщение — это предупреждение исчезнет само."
    ),
    "en": (
        "{mention}, the report has form problems:\n\n{lines}\n\n"
        "Edit the message and this warning will clear itself."
    ),
}

# One line per issue code. `{field}` and `{detail}` are substituted when available.
ISSUE_LINES: Final[dict[str, dict[str, str]]] = {
    "field_missing": {
        "ru": "- Не найдено поле «{field_hint}».",
        "en": "- Missing field '{field_hint}'.",
    },
    "schema_unmatched": {
        "ru": "- Сообщение не похоже на известную форму рапорта.",
        "en": "- This message does not match any known report form.",
    },
    "idn_no_formation": {
        "ru": "- В поле 1 не указано формирование (например, «SOB | 0016»).",
        "en": "- Field 1 does not name a formation (expected e.g. 'SOB | 0016').",
    },
    "idn_unparsed": {
        "ru": "- Не удалось разобрать IDN: «{detail_raw}».",
        "en": "- Could not parse the IDN: '{detail_raw}'.",
    },
    "enum_unknown_option": {
        "ru": "- Неизвестная квалификация: «{detail_raw}».",
        "en": "- Unknown qualification: '{detail_raw}'.",
    },
    "link_not_a_url": {
        "ru": "- В поле 4 нет ссылки.",
        "en": "- Field 4 contains no link.",
    },
    "link_not_discord": {
        "ru": "- Ссылка в поле 4 не ведёт на сообщение Discord.",
        "en": "- The field 4 link is not a Discord message link.",
    },
    "evidence_text_without_file": {
        "ru": "- В поле «{field_hint}» текст вместо фото.",
        "en": "- Field '{field_hint}' contains text instead of a photo.",
    },
    "fraction_invalid": {
        "ru": "- Некорректный результат теста: {detail_got}/{detail_total}.",
        "en": "- Invalid test result: {detail_got}/{detail_total}.",
    },
    "fraction_unparsed": {
        "ru": "- Не удалось разобрать результат теста.",
        "en": "- Could not parse the test result.",
    },
    "idn_bad_length": {
        "ru": "- IDN «{detail_raw}» неверной длины: нужно 4 цифры или 6 и больше.",
        "en": "- IDN '{detail_raw}' has the wrong length: 4 digits, or 6 or more.",
    },
}

# Shown when a report validates after previously being warned.
REPORT_FIXED: Final[dict[str, str]] = {
    "ru": "Форма рапорта исправлена.",
    "en": "Report form corrected.",
}

# Shown to staff in a log channel when an approval is ambiguous.
VERDICT_CONFLICT: Final[dict[str, str]] = {
    "ru": ("Конфликт вердиктов у рапорта {link}: одинаковый уровень у {users}. Решите вручную."),
    "en": ("Verdict conflict on report {link}: equal tier from {users}. Resolve manually."),
}


def issue_line(code: str, locale: str, **context: str) -> str:
    table = ISSUE_LINES.get(code)
    if table is None:
        return f"- {code}"
    body = table.get(locale) or table.get(DEFAULT_LOCALE) or f"- {code}"
    try:
        return body.format(**context)
    except KeyError:
        return body
