"""Make the database match `forms/channels.yaml`, including its ladders.

Everything in git is applied here: the form each channel parses, and the ladders named by
that channel. This runs at startup, so there is no separate "apply" step for ladders —
the file is the source of truth and the bot follows it.

Deliberately one-directional: nothing is deleted, so a channel dropped from the file keeps
its history and comes back by re-adding the entry.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsbot.modules.ranks.ladder import LadderSpec, ladder_problems, load_ladder
from dsbot.modules.reports.authoring import import_forms
from dsbot.modules.reports.channels import (
    ChannelsFile,
    ChannelsSyncResult,
)
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.storage import admin, ranks_repo
from dsbot.storage.models import ChannelBinding, RankLadder

# Form key -> YAML file. Kept next to the form definitions so a new form is one entry.
FORM_FILES: dict[str, Path] = {
    "training_report": Path("forms/training.yaml"),
}

LADDER_DIR = Path("forms/ladders")


async def sync_channels(
    session: AsyncSession,
    spec: ChannelsFile,
    *,
    forms_dir: Path = Path("forms"),
    ladders_dir: Path = LADDER_DIR,
) -> ChannelsSyncResult:
    """Apply the channels file, and every ladder it names, to the database.

    Idempotent: running it twice changes nothing the second time. A paused channel in the
    file stays paused; a channel enabled in the file is enabled.

    Ladders are applied first, because a binding points at a ladder row and that row must
    exist before the channel can reference it.
    """
    result = ChannelsSyncResult()

    ladder_ids = await _apply_ladders(session, spec, ladders_dir, result)

    for entry in spec.channels:
        form_path = _form_path(entry.form, forms_dir)
        schema = parse_form_file(form_path)
        payload = {"schemas": [schema.model_dump(mode="json")]}

        existing = await session.scalar(
            select(ChannelBinding).where(ChannelBinding.channel_id == entry.channel)
        )

        review_id = _ladder_id(ladder_ids, entry.rank_source_guild, entry.review_ladder)
        if entry.review_ladder is not None and review_id is None:
            raise ValueError(
                f"channel {entry.channel}: review_ladder {entry.review_ladder!r} not found "
                f"in guild {entry.rank_source_guild} or in {ladders_dir}"
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
            if not entry.enabled:
                await admin.set_binding_enabled(session, entry.channel, False)
            await _store_ladder_choice(session, entry, review_id)
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
        if await _store_ladder_choice(session, entry, review_id):
            changed = True

        if changed:
            result.updated.append(entry.channel)
            if not entry.enabled:
                result.paused.append(entry.channel)
        else:
            result.unchanged.append(entry.channel)

    return result


async def _apply_ladders(
    session: AsyncSession,
    spec: ChannelsFile,
    ladders_dir: Path,
    result: ChannelsSyncResult,
) -> dict[tuple[int, str], int]:
    """Apply every ladder named by any channel. Returns (guild, key) -> ladder id."""
    wanted: dict[tuple[int, str], Path] = {}
    for entry in spec.channels:
        guild = entry.rank_source_guild
        for key in (entry.review_ladder, entry.quota_ladder):
            if key:
                wanted[(guild, key)] = ladders_dir / f"{key}.yaml"

    applied: dict[tuple[int, str], int] = {}
    for (guild, key), path in wanted.items():
        if not path.is_file():
            # A channel may name a ladder that is already in the database and only in
            # someone's environment; look it up rather than failing immediately.
            continue
        parsed: LadderSpec = load_ladder(path)
        if parsed.key != key:
            raise ValueError(f"{path} declares key {parsed.key!r}, but a channel asked for {key!r}")
        if parsed.guild_id != guild:
            raise ValueError(
                f"{path} is for guild {parsed.guild_id}, but a channel asked for it in "
                f"guild {guild}. Roles must come from the guild the channel points at."
            )
        problems = ladder_problems(parsed)
        blocking = [p for p in problems if "no roles listed" not in p]
        if blocking:
            raise ValueError("; ".join(blocking))
        ladder = await ranks_repo.replace_ladder(session, parsed)
        applied[(guild, key)] = ladder.id
        result.ladders.append(f"{guild}/{key}")

    # Pick up ladders that exist in the database but not in this checkout.
    existing = (await session.scalars(select(RankLadder))).all()
    for row in existing:
        applied.setdefault((row.guild_id, row.key), row.id)

    return applied


def _ladder_id(ladder_ids: dict[tuple[int, str], int], guild: int, key: str | None) -> int | None:
    if not key:
        return None
    return ladder_ids.get((guild, key))


async def _store_ladder_choice(session: AsyncSession, entry: object, review_id: int | None) -> bool:
    """Persist which review ladder this channel uses, plus its quota ladder key."""
    from dsbot.modules.reports.channels import ChannelSpec

    assert isinstance(entry, ChannelSpec)
    binding = await session.scalar(
        select(ChannelBinding).where(ChannelBinding.channel_id == entry.channel)
    )
    if binding is None:
        return False
    changed = False
    if binding.rank_ladder_id != review_id:
        binding.rank_ladder_id = review_id
        changed = True
    config = dict(binding.config or {})
    if entry.quota_ladder is not None and config.get("quota_ladder") != entry.quota_ladder:
        config["quota_ladder"] = entry.quota_ladder
        binding.config = config
        changed = True
    return changed


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
