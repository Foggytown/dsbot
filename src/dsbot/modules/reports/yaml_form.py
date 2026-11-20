"""Human-editable form rules.

The problem: a form like

    1) Formation | lore IDN | optional NRP IDN
    2) Qualification (finite list)
    3) Photos: 1 if T, otherwise 2+
    4) Link to a previous report, required unless qualification is T

is genuinely hard to express, because the requirements on fields 3 and 4 *depend on
the value of field 2*. No amount of clicking in a form builder makes that easy; what
makes it easy is a file with one line per rule, that can be read and edited by hand.

So forms live in YAML. The CLI compiles YAML into the stored schema, validating it
first so a typo cannot break a channel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dsbot.modules.reports.schema import FormSchemaDef

EXAMPLE_HEADER = """\
# dsbot report form
#
# Edit this file, then apply it:
#     uv run dsbot form-apply --channel <CHANNEL_ID> --file forms/<name>.yaml
#
# Check it without applying:
#     uv run dsbot form-check --file forms/<name>.yaml
#
# FIELD TYPES
#   identity          formation | lore IDN | optional NRP IDN   (see lore_prefixes)
#   enum              a value from a finite list
#   attachment        photos/evidence; counts attachments
#   discord_message_ref   a link to a message
#   text              free text
#   count_fraction    "7/8"
#   name_ref          a person's name
#
# RULE OPERATORS: eq, ne, in, not_in, gt, gte, lt, lte
"""


def load_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def parse_form_file(path: Path) -> FormSchemaDef:
    """Read and validate a YAML form. Raises with a readable message on errors."""
    return FormSchemaDef.model_validate(load_yaml(path))


def form_to_yaml(schema: FormSchemaDef) -> str:
    """Serialise a form back to YAML, with the explanatory header preserved."""
    body = schema.model_dump(mode="json", exclude_defaults=False)
    rendered = yaml.safe_dump(
        body, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
    return EXAMPLE_HEADER + "\n" + rendered


def compile_problems(schema: FormSchemaDef) -> list[str]:
    """Catch the mistakes a human actually makes when writing these files."""
    problems: list[str] = []
    ids = {f.id for f in schema.fields}

    if not schema.fields:
        problems.append("form has no fields")
        return problems

    for field in schema.fields:
        if field.type == "enum" and not field.options:
            problems.append(f"field '{field.id}': enum has no options list")
        if field.type == "identity" and not field.formations:
            problems.append(f"field '{field.id}': identity has no formations list")
        if field.path is None and not field.labels:
            problems.append(
                f"field '{field.id}': needs either a path (e.g. '1') or labels, "
                "otherwise it can never be found in a message"
            )
        for sub_unit, prefix in field.lore_prefixes.items():
            if not prefix.isdigit():
                problems.append(
                    f"field '{field.id}': lore prefix for {sub_unit} must be digits, got {prefix!r}"
                )

    for rule in schema.rules:
        if rule.field not in ids:
            problems.append(f"rule '{rule.id}': refers to unknown field '{rule.field}'")
        for condition in rule.when:
            if condition.field not in ids:
                problems.append(
                    f"rule '{rule.id}': condition refers to unknown field '{condition.field}'"
                )
        if (
            rule.min_attachments is None
            and not rule.allowed
            and rule.type is None
            and not rule.when
        ):
            problems.append(
                f"rule '{rule.id}': has no effect "
                "(no min_attachments, allowed, type, or when conditions)"
            )
        if not rule.message:
            problems.append(f"rule '{rule.id}': add a message so the reporter knows what is wrong")

    return problems
