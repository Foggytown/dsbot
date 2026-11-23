"""Bot-authored text, per locale.

Every user-visible string is overridable from the database (`templates` table), so a
Discord admin can reword warnings without a deploy. These are the seed defaults only.
"""

from __future__ import annotations

from typing import Final

DEFAULT_LOCALE: Final = "ru"
SUPPORTED_LOCALES: Final[tuple[str, ...]] = ("en", "ru")

# Top-level bot message for an invalid report.
#
# `{mention}` pings the author. `{contact}` pings the operator. The correct shape is NOT
# reproduced here: it belongs in the pinned template, which is the single place people
# should copy from.
REPORT_ISSUES: Final[dict[str, str]] = {
    "ru": (
        "{mention}, в рапорте есть ошибки формы:\n\n{lines}\n\n"
        "Правильный вид — в закреплённом сообщении.\n"
        "Исправьте сообщение — это предупреждение исчезнет само.\n"
        "Если бот ошибается — напишите {contact}."
    ),
    "en": (
        "{mention}, the report has form problems:\n\n{lines}\n\n"
        "The correct shape is in the pinned message.\n"
        "Edit the message and this warning will clear itself.\n"
        "If the bot is wrong, contact {contact}."
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
        "ru": (
            "- В пункте {detail_field_number} нет ссылки. Там нужна ссылка на "
            "**одобренный** рапорт, который даёт право на повышение квалификации."
        ),
        "en": (
            "- Field {detail_field_number} contains no link. It must be a link to an "
            "**approved** report that grants the qualification promotion."
        ),
    },
    "link_not_discord": {
        "ru": (
            "- Ссылка в пункте {detail_field_number} не ведёт на сообщение Discord. "
            "Нужна ссылка на **одобренный** рапорт, который даёт право на повышение "
            "квалификации."
        ),
        "en": (
            "- The link in field {detail_field_number} is not a Discord message link. "
            "It must link to an **approved** report that grants the qualification "
            "promotion."
        ),
    },
    "link_in_wrong_field": {
        "ru": (
            "- Ссылка на **одобренный** рапорт, дающий право на повышение квалификации, "
            "должна стоять в пункте {detail_expected}, а сейчас она в пункте "
            "{detail_found_in}. Перенесите её."
        ),
        "en": (
            "- The link to the **approved** report that grants the qualification "
            "promotion belongs in field {detail_expected}, but it is in field "
            "{detail_found_in}. Move it there."
        ),
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
    "nrp_missing": {
        "ru": (
            "- К лорному IDN «{detail_prefix}/{detail_lore_idn}» нужен системный "
            "(NRP) IDN из 4 цифр через |, например "
            "ФОРМИРОВАНИЕ | {detail_prefix}/{detail_lore_idn} | 0000."
        ),
        "en": (
            "- The lore IDN '{detail_prefix}/{detail_lore_idn}' needs its 4-digit "
            "system (NRP) IDN after a |, for example "
            "FORMATION | {detail_prefix}/{detail_lore_idn} | 0000."
        ),
    },
    "list_allowed_formations": {
        "ru": "- Допустимые формирования: {detail_formations}.",
        "en": "- Allowed formations: {detail_formations}.",
    },
    "identity_bad_format": {
        "ru": (
            "- Поле 1 должно быть строго «ФОРМИРОВАНИЕ | IDN», "
            "например: ARC | 0042 или SOB | 01/0016 | 0017."
        ),
        "en": (
            "- Field 1 must be exactly 'FORMATION | IDN', "
            "for example: ARC | 0042 or SOB | 01/0016 | 0017."
        ),
    },
    "formation_not_single_word": {
        "ru": "- В поле 1 должно быть только название формирования, без лишних слов.",
        "en": "- Field 1 must contain only the formation name, no extra words.",
    },
    "one_soldier_per_report": {
        "ru": "- В одном рапорте указывается только один боец.",
        "en": "- One report describes exactly one soldier.",
    },
    "idn_labelled_not_allowed": {
        "ru": (
            "- Не нужно писать NRP/RP/RC перед номером: только сам IDN из 4 цифр. "
            "Например: SOB | 01/0016 | 0017."
        ),
        "en": (
            "- Do not prefix the number with NRP/RP/RC: just the 4-digit IDN. "
            "For example: SOB | 01/0016 | 0017."
        ),
    },
    "brackets_not_allowed": {
        "ru": "- Скобки [ ] не нужны. Формат: «ФОРМИРОВАНИЕ | IDN», например SOB | 01/0038.",
        "en": "- No [ ] brackets. Format: 'FORMATION | IDN', for example SOB | 01/0038.",
    },
    "backslash_not_allowed": {
        "ru": "- Нужен обычный слеш «/», а не обратный «\\». Например: SOB | 01/0038.",
        "en": "- Use a normal slash '/', not a backslash '\\'. For example: SOB | 01/0038.",
    },
    "semicolon_not_allowed": {
        "ru": (
            "- Несколько бойцов разделяются запятой «,», а не точкой с запятой «;». "
            "Например: ARC | 0042, ARC | 0043."
        ),
        "en": (
            "- Separate several soldiers with a comma ',', not a semicolon ';'. "
            "For example: ARC | 0042, ARC | 0043."
        ),
    },
    "identity_empty_soldier": {
        "ru": (
            "- Поле 1: лишняя запятая, между бойцами не должно быть пустого места. "
            "Например: ARC | 0042, ARC | 0043."
        ),
        "en": (
            "- Field 1: there is a stray comma, with no empty entry allowed between soldiers. "
            "For example: ARC | 0042, ARC | 0043."
        ),
    },
    "list_allowed_qualifications": {
        "ru": "- Допустимые квалификации: {detail_options}.",
        "en": "- Allowed qualifications: {detail_options}.",
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


def issue_line(code: str, locale: str, **context: str) -> str:
    table = ISSUE_LINES.get(code)
    if table is None:
        return f"- {code}"
    body = table.get(locale) or table.get(DEFAULT_LOCALE) or f"- {code}"
    try:
        return body.format(**context)
    except KeyError:
        return body
