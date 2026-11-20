"""Make the database match `forms/channels.yaml`.

Deliberately one-directional: the file is the source of truth for *which* channels are
served and with what form. Nothing is deleted, so a channel accidentally dropped from the
file keeps its history and can be restored by re-adding the entry.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsbot.modules.reports.authoring import import_forms
from dsbot.modules.reports.channels import (
    DEFAULT_CHANNELS_FILE,
    ChannelsFile,
    ChannelsSyncResult,
    load_channels_file,
)
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.storage import admin
from dsbot.storage.models import ChannelBinding

# Form key -> YAML file. Kept next to the form definitions so a new form is one entry.
FORM_FILES: dict[str, Path] = {
    "training_report": Path("forms/training.yaml"),
}


async def sync_channels(
    session: AsyncSession,
    spec: ChannelsFile,
    *,
    forms_dir: Path = Path("forms"),
) -> ChannelsSyncResult:
    """Apply the channels file to the database.

    Idempotent: running it twice changes nothing the second time. A paused channel in
    the file stays paused; a channel enabled in the file is enabled.
    """
    result = ChannelsSyncResult()

    for entry in spec.channels:
        form_path = _form_path(entry.form, forms_dir)
        schema = parse_form_file(form_path)
        payload = {"schemas": [schema.model_dump(mode="json")]}

        existing = await session.scalar(
            select(ChannelBinding).where(ChannelBinding.channel_id == entry.channel)
        )

        if existing is None:
            await import_forms(
                session,
                channel_id=entry.channel,
                payload=payload,
                guild_id=entry.guild,
                rank_source_guild_id=entry.rank_guild,
                locale=entry.locale,
            )
            result.created.append(entry.channel)
            # `enabled: false` on a brand-new entry still creates it, then pauses it.
            if not entry.enabled:
                await admin.set_binding_enabled(session, entry.channel, False)
            continue

        changed = False
        if existing.guild_id != entry.guild:
            existing.guild_id = entry.guild
            changed = True
        expected_rank_guild = entry.rank_guild
        if expected_rank_guild is not None and (
            existing.rank_source_guild_id != expected_rank_guild
        ):
            existing.rank_source_guild_id = expected_rank_guild
            changed = True
        if existing.locale != entry.locale:
            existing.locale = entry.locale
            changed = True
        if existing.enabled != entry.enabled:
            existing.enabled = entry.enabled
            changed = True

        await import_forms(
            session,
            channel_id=entry.channel,
            payload=payload,
            guild_id=entry.guild,
            rank_source_guild_id=entry.rank_guild,
            locale=entry.locale,
        )

        if changed:
            result.updated.append(entry.channel)
            if not entry.enabled:
                result.paused.append(entry.channel)
        else:
            result.unchanged.append(entry.channel)

    return result


async def load_channels_from_file(
    path: Path = DEFAULT_CHANNELS_FILE,
) -> ChannelsFile:
    return load_channels_file(path)


def _form_path(form_key: str, forms_dir: Path) -> Path:
    """Resolve a form key to its YAML file."""
    known = FORM_FILES.get(form_key)
    if known is not None and known.is_file():
        return known
    # Fall back to a file named after the key, so adding a form needs no code change.
    candidate = forms_dir / f"{form_key}.yaml"
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(
        f"form {form_key!r} not found: expected {candidate} or an entry in FORM_FILES"
    )
