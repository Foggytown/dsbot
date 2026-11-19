"""Fixtures transcribed from real messages in the report channel.

Everything here is real text (redacted only where it identifies a live Discord id).
The parser is developed against these, not against imagined formats.
"""

from __future__ import annotations

# The pinned template. Must NOT be parsed as a report: it contains the explanatory
# branch that real reports never include.
TEMPLATE = """# <:emodzibookmark:1323500003699724368> Формуляр рапорта на обучение:

```cs
1. Формирование | RP-IDN | NRP-IDN обучаемого

Для SOB/ARC используйте подобный формат:
        - Alpha: 01/00**
        - Null: 00/00**
        - RC: Полный IDN
        - Reaper: полный IDN
        - 666th: полный IDN
        - ARC: полный IDN

2. Название квалификации

3. Доказательства:
     - 3.1. Фото в инж. блоке.
     - 3.2. Фото практики во время обучения.

4. Для ENG|INF+:
    - Приложить ссылку на одобренное повышение квалификации.
```
"""

# A real filled report. Note field 3 left completely empty: the photos were sent
# in a follow-up message by the same author.
REAL_LORE_IDN = """1. SOB | 01/0016
2. INF
3.
4. https://discord.com/channels/1274402776696488108/1485791744917241938/1570351536259731550"""

# Plain 4-digit NRP form, no lore prefix.
REAL_NRP = """1. SOB | 0016
2. INF
3. 
4. """

# Inline-label form: same content, different typography.
INLINE_LABELS = """1. Формирование: SOB | 0016
2. Название квалификации: MED
3. Доказательства:
    - фото в инж. блоке
    - фото практики во время обучения"""

# Dash used as an explicit "nothing here" marker, as described by the user.
DASH_MARKERS = """1. SOB | 0016
2. INF
3. -
4. -"""

# All numbering dropped; only the labels remain.
NO_NUMBERS = """Формирование: SOB | 0016
Название квалификации: INF"""

# A report for a qualification that requires the promotion link, with it missing.
MISSING_CONDITIONAL = """1. SOB | 0016
2. INF+
3."""

# Barely a report at all.
GARBAGE = """ребята сегодня тренировали новичка, всё прошло хорошо"""

# Minimum viable valid report with both evidence photos attached in-message.
VALID_WITH_PHOTOS = """1. SOB | 0016
2. MED ready
3. Доказательства:"""

VALID = """1. SOB | 0016
2. MED
3. Доказательства:
     - 3.1. Фото в инж. блоке.
     - 3.2. Фото практики во время обучения.
4."""
