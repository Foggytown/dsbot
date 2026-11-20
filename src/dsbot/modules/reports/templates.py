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
    "nrp_bad_length": {
        "ru": (
            "- NRP-IDN «{detail_raw}» должен быть ровно 4 цифры (системный IDN идёт после лорного)."
        ),
        "en": (
            "- NRP-IDN '{detail_raw}' must be exactly 4 digits "
            "(the system IDN comes after the lore IDN)."
        ),
    },
    "formation_unknown": {
        "ru": "- Неизвестное формирование: «{detail_raw}».",
        "en": "- Unknown formation: '{detail_raw}'.",
    },
    "lore_prefix_invalid": {
        "ru": ("- Лорный IDN может начинаться только с 01/ или 00/, а указано «{detail_raw}»."),
        "en": "- A lore IDN may only start with 01/ or 00/, but '{detail_raw}' was given.",
    },
    "lore_prefix_not_allowed_for_formation": {
        "ru": (
            "- IDN с 01/ или 00/ бывает только у SOB, а указано формирование «{detail_formation}»."
        ),
        "en": (
            "- Only SOB can use a 01/ or 00/ IDN, but formation '{detail_formation}' was given."
        ),
    },
    "idn_bad_shape": {
        "ru": (
            "- IDN «{detail_raw}» неверного формата: нужно либо 4 цифры, либо 01/00NN или 00/00NN."
        ),
        "en": ("- IDN '{detail_raw}' has the wrong shape: either 4 digits, or 01/00NN or 00/00NN."),
    },
    "idn_nrp_not_allowed": {
        "ru": (
            "- NRP-IDN «{detail_raw}» можно указывать только вместе с 01/ или 00/ IDN, "
            "а не с обычным 4-значным."
        ),
        "en": (
            "- NRP-IDN '{detail_raw}' may only follow a 01/ or 00/ IDN, not a plain 4-digit one."
        ),
    },
    "idn_missing": {
        "ru": "- Не указан IDN после формирования.",
        "en": "- No IDN given after the formation.",
    },
    "rule_min_attachments": {
        "ru": "- Фото: нужно не меньше {detail_required}, найдено {detail_found}.",
        "en": "- Photos: need at least {detail_required}, found {detail_found}.",
    },
    "rule_max_attachments": {
        "ru": "- Фото: нужно не больше {detail_required}, найдено {detail_found}.",
        "en": "- Photos: need at most {detail_required}, found {detail_found}.",
    },
    "rule_exact_attachments": {
        "ru": "- Фото: нужно ровно {detail_required}, найдено {detail_found}.",
        "en": "- Photos: need exactly {detail_required}, found {detail_found}.",
    },
    "rule_required": {
        "ru": "- {detail_message}",
        "en": "- {detail_message}",
    },
    "rule_not_allowed": {
        "ru": "- {detail_message}",
        "en": "- {detail_message}",
    },
    "rule_wrong_type": {
        "ru": "- {detail_message}",
        "en": "- {detail_message}",
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
