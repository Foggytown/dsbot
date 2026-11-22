"""SQLAlchemy models.

Phase 1 stores in SQLite but avoids SQLite-only types so a move to Postgres is a
URL change plus a migration run. JSON columns are deliberate (raw payloads and
schema definitions change shape); anything queried or aggregated gets a real column.

Two identity domains are kept strictly separate:
  * actors  -> Discord users in guilds, resolved to a rank tier
  * subjects -> characters, identified by IDN, not unique by design
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


# --- raw event log ---------------------------------------------------------


class RawEvent(Base):
    """Append-only ingest log. The replay and audit source of truth.

    external_id is the idempotency key: the same gateway event delivered twice, or
    delivered once live and once by backfill, is stored once.
    """

    __tablename__ = "raw_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = _ts()
    source: Mapped[str] = mapped_column(String(20), default="gateway")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


# --- guilds, channels, bindings -------------------------------------------


class Guild(Base):
    __tablename__ = "guilds"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    is_report_guild: Mapped[bool] = mapped_column(Boolean, default=False)
    is_rank_source: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = _ts()

    channels: Mapped[list[Channel]] = relationship(back_populates="guild")


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guilds.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    parent_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="text")
    first_seen_at: Mapped[datetime] = _ts()

    guild: Mapped[Guild] = relationship(back_populates="channels")
    binding: Mapped[ChannelBinding | None] = relationship(back_populates="channel", uselist=False)


class ChannelBinding(Base):
    """The unit of behaviour: which module runs in which channel, with what config.

    One module can serve many channels with different settings. Adding a second
    report channel is a new row, not a code change.
    """

    __tablename__ = "channel_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), unique=True, index=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    module_key: Mapped[str] = mapped_column(String(50), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Which guild's role ladder governs this channel. Reports and rank sources are
    # different guilds by design in this community.
    rank_source_guild_id: Mapped[int | None] = mapped_column(BigInteger)
    rank_ladder_id: Mapped[int | None] = mapped_column(ForeignKey("rank_ladders.id"))

    locale: Mapped[str] = mapped_column(String(5), default="ru")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()

    channel: Mapped[Channel] = relationship(back_populates="binding")


class BindingSchema(Base):
    """Which form schemas may appear in a binding, and in what priority."""

    __tablename__ = "binding_schemas"
    __table_args__ = (UniqueConstraint("binding_id", "schema_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(
        ForeignKey("channel_bindings.id", ondelete="CASCADE"), index=True
    )
    schema_id: Mapped[int] = mapped_column(
        ForeignKey("form_schemas.id", ondelete="CASCADE"), index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)


class FormSchema(Base):
    """A declarative report form. Definition JSON, never code.

    shape: {key, title, anchors[], fields[], attribution_field, locale}
    Each field: {id, path, labels[], ordinal, type, required, required_when, options}
    """

    __tablename__ = "form_schemas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    locale: Mapped[str] = mapped_column(String(5), default="ru")
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    template_text: Mapped[str | None] = mapped_column(Text)
    # Fingerprint of the YAML this form was last imported from.
    #
    # It is what makes an admin's in-Discord edit survivable. On every start the channels
    # file is re-imported; without a record of "what the file said last time", a stored
    # form that differs from the file is indistinguishable from a file that was just
    # edited, and the import blindly overwrote the admin's `add-value`. Comparing
    # fingerprints tells the three cases apart:
    #
    #   file == stored fingerprint, definition differs -> the admin edited it, keep it
    #   file != stored fingerprint                     -> git changed it, apply the file
    #
    # Hashed over the *compiled* definition, not the file bytes: reformatting YAML or
    # editing a comment must not count as a change.
    import_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --- rank ladders ----------------------------------------------------------


class RankLadder(Base):
    """Authority ordering for one guild, defined by admins, not Discord role order."""

    __tablename__ = "rank_ladders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    key: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200), default="")
    # "review" (who may approve) or "quota" (who owes reports). Declared, not inferred,
    # so an unfinished review ladder is never mistaken for a quota one.
    purpose: Mapped[str] = mapped_column(String(20), default="review")
    created_at: Mapped[datetime] = _ts()

    tiers: Mapped[list[RankTier]] = relationship(
        back_populates="ladder", order_by="RankTier.ordinal"
    )


class RankTier(Base):
    """A rung. ordinal answers one question only: whose verdict outranks whose.

    Capabilities (admin, review) are separate rows, so authority and permission
    cannot be confused.
    """

    __tablename__ = "rank_tiers"
    __table_args__ = (UniqueConstraint("ladder_id", "ordinal"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ladder_id: Mapped[int] = mapped_column(
        ForeignKey("rank_ladders.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(200), default="")
    ordinal: Mapped[int] = mapped_column(Integer, index=True)
    # Reports this tier must file each week, from the quota ladder. NULL means the tier
    # is not a quota tier. Kept on the tier because it is a property of the rank, so an
    # admin edits one place and the YAML file stays the source of truth.
    required_per_week: Mapped[int | None] = mapped_column(Integer)

    ladder: Mapped[RankLadder] = relationship(back_populates="tiers")
    roles: Mapped[list[RankTierRole]] = relationship(
        back_populates="tier", cascade="all, delete-orphan"
    )


class RankTierRole(Base):
    __tablename__ = "rank_tier_roles"
    __table_args__ = (UniqueConstraint("tier_id", "role_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tier_id: Mapped[int] = mapped_column(
        ForeignKey("rank_tiers.id", ondelete="CASCADE"), index=True
    )
    role_id: Mapped[int] = mapped_column(BigInteger, index=True)

    tier: Mapped[RankTier] = relationship(back_populates="roles")


class TierCapability(Base):
    """Permission granted by tier, never implied by ordinal."""

    __tablename__ = "tier_capabilities"
    __table_args__ = (UniqueConstraint("tier_id", "capability"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tier_id: Mapped[int] = mapped_column(
        ForeignKey("rank_tiers.id", ondelete="CASCADE"), index=True
    )
    capability: Mapped[str] = mapped_column(String(50), index=True)


class UserRankCache(Base):
    """Last resolved rank per (user, guild).

    Cross-server reactors arrive as User objects with no roles, so their rank must be
    resolved from their home guild. Caching keeps reaction handling off the REST path.
    """

    __tablename__ = "user_rank_cache"
    __table_args__ = (UniqueConstraint("guild_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ladder_id: Mapped[int | None] = mapped_column(ForeignKey("rank_ladders.id"))
    tier_id: Mapped[int | None] = mapped_column(ForeignKey("rank_tiers.id"))
    tier_ordinal: Mapped[int | None] = mapped_column(Integer)
    resolved_at: Mapped[datetime] = _ts()
    source: Mapped[str] = mapped_column(String(20), default="gateway")


# --- messages --------------------------------------------------------------


class Message(Base):
    """Full archive of bound channels. Text is always kept, even when parsing fails."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "message_id"),
        Index("ix_messages_author_created", "author_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_name: Mapped[str] = mapped_column(String(200), default="")
    author_display_name: Mapped[str | None] = mapped_column(String(200))
    author_is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referenced_message_id: Mapped[int | None] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    attachments: Mapped[list[Any]] = mapped_column(JSON, default=list)
    embeds: Mapped[list[Any]] = mapped_column(JSON, default=list)


class MessageReaction(Base):
    """One (message, emoji, user) triple.

    Verdicts are recomputed from this set, so removal is a delete and the previous
    verdict reappears naturally.
    """

    __tablename__ = "message_reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "emoji_key", "user_id"),
        Index("ix_reactions_message_user", "message_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger)
    emoji_key: Mapped[str] = mapped_column(String(80), index=True)
    emoji_id: Mapped[int | None] = mapped_column(BigInteger)
    emoji_name: Mapped[str | None] = mapped_column(String(120))
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # Rank snapshot at reaction time: a later promotion must not rewrite history.
    # `can_review` is snapshotted too, otherwise recomputing an old verdict would
    # silently apply today's ladder to a reaction made under yesterday's.
    tier_key: Mapped[str | None] = mapped_column(String(80))
    tier_ordinal: Mapped[int | None] = mapped_column(Integer)
    tier_label: Mapped[str | None] = mapped_column(String(200))
    can_review: Mapped[bool | None] = mapped_column(Boolean)
    added_at: Mapped[datetime] = _ts()


# --- reports ---------------------------------------------------------------


class Report(Base):
    """One parsed report, which may span several messages (photo in a follow-up)."""

    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("channel_id", "message_id"),
        Index("ix_reports_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    binding_id: Mapped[int | None] = mapped_column(ForeignKey("channel_bindings.id"))
    schema_id: Mapped[int | None] = mapped_column(ForeignKey("form_schemas.id"))
    schema_key: Mapped[str | None] = mapped_column(String(80))

    author_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # Attribution: who the report counts for. Not necessarily the author.
    attributed_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    attributed_char_id: Mapped[int | None] = mapped_column(ForeignKey("characters.id"))
    attributed_raw: Mapped[str | None] = mapped_column(String(300))
    attributed_field: Mapped[str | None] = mapped_column(String(80))
    attribution_method: Mapped[str] = mapped_column(String(20), default="self")

    status: Mapped[str] = mapped_column(String(30), default="received", index=True)
    verdict: Mapped[str | None] = mapped_column(String(30), index=True)
    verdict_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verdict_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    verdict_tier_ordinal: Mapped[int | None] = mapped_column(Integer)

    parsing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    issues: Mapped[list[Any]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    fields: Mapped[list[ReportField]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class ReportField(Base):
    """A single resolved form field. Raw text is preserved for re-derivation."""

    __tablename__ = "report_fields"
    __table_args__ = (UniqueConstraint("report_id", "field_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    field_id: Mapped[str] = mapped_column(String(80))
    path: Mapped[str | None] = mapped_column(String(40))
    label: Mapped[str | None] = mapped_column(String(200))
    raw_value: Mapped[str | None] = mapped_column(Text)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    present: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution: Mapped[str] = mapped_column(String(20), default="absent")

    report: Mapped[Report] = relationship(back_populates="fields")


class ReactionCacheEntry(Base):
    """Reactors fetched from Discord by a scan, never the live gateway's record.

    Deliberately a separate table from `message_reactions`. That one is the bot's own
    observation, carrying a rank snapshot taken when the reaction happened; this one is
    just "what the last scan saw", used to avoid re-fetching. Sharing one table meant a
    scan would delete a live snapshot and replace it with an empty one, silently
    destroying the authority of a real approval.
    """

    __tablename__ = "reaction_cache"
    __table_args__ = (
        UniqueConstraint("message_id", "emoji_key", "user_id"),
        Index("ix_reaction_cache_message", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    emoji_key: Mapped[str] = mapped_column(String(80))
    emoji_name: Mapped[str | None] = mapped_column(String(120))
    captured_at: Mapped[datetime] = _ts()


class ReportVerdictHistory(Base):
    """Every verdict change. Kept so overrides and reversions are auditable."""

    __tablename__ = "report_verdict_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    verdict: Mapped[str] = mapped_column(String(30))
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    tier_ordinal: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(40), default="reaction")
    recorded_at: Mapped[datetime] = _ts()


class ValidationMessage(Base):
    """A bot reply we posted for a malformed report, so it can be edited or deleted.

    Rows are removed once the report validates, which is what makes 'edit-on-fix'
    clear itself instead of leaving stale warnings in the channel.
    """

    __tablename__ = "validation_messages"
    __table_args__ = (UniqueConstraint("report_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="warning")
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    # The report this warning is about. Without it the row records only the bot's own
    # message, so "which report?" needs a join through reports.id and a manual lookup.
    report_message_id: Mapped[int | None] = mapped_column(BigInteger)
    issue_codes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _ts()


# --- characters (subjects) -------------------------------------------------


class Character(Base):
    """A soldier/character identity. IDN is never unique: >10k characters share 4 digits."""

    __tablename__ = "characters"
    __table_args__ = (
        Index("ix_characters_lookup", "formation", "idn_kind", "sub_unit", "idn_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    formation: Mapped[str | None] = mapped_column(String(40), index=True)
    idn_kind: Mapped[str] = mapped_column(String(20), default="nrp")
    idn_text: Mapped[str] = mapped_column(String(40), default="")
    sub_unit: Mapped[str | None] = mapped_column(String(20))
    idn_number: Mapped[str | None] = mapped_column(String(10), index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()


# --- quotas ----------------------------------------------------------------


class QuotaPolicy(Base):
    """Optional per-channel override of a tier's weekly requirement.

    The requirement itself lives on the rank tier, in the quota ladder file, because it
    is a property of the rank: "an instructor files three reports a week". This table
    exists only for the exception - one channel needing a different number - so an admin
    does not have to fork the whole ladder to tune a single channel.
    """

    __tablename__ = "quota_policies"
    __table_args__ = (UniqueConstraint("ladder_id", "tier_id", "binding_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ladder_id: Mapped[int] = mapped_column(
        ForeignKey("rank_ladders.id", ondelete="CASCADE"), index=True
    )
    tier_id: Mapped[int | None] = mapped_column(ForeignKey("rank_tiers.id"))
    binding_id: Mapped[int | None] = mapped_column(ForeignKey("channel_bindings.id"))
    required_count: Mapped[int] = mapped_column(Integer, default=0)
    period: Mapped[str] = mapped_column(String(20), default="week")
    # report_time: rank at report creation decides the quota.
    # week_end: rank at evaluation time decides it.
    rank_strategy: Mapped[str] = mapped_column(String(20), default="report_time")
    count_rejected: Mapped[bool] = mapped_column(Boolean, default=False)
    count_pending: Mapped[bool] = mapped_column(Boolean, default=False)


class RosterMember(Base):
    """Who is counted, scraped from role membership at one moment.

    Counting must be reproducible: a member who loses a role tomorrow must not
    silently disappear from last week's numbers. Each count therefore stores the
    roster it used, and `captured_at` says how old that snapshot is.

    The roster is taken from the *rank source* guild, not the report guild, because
    instructors are not members of the channel's server.
    """

    __tablename__ = "roster_members"
    __table_args__ = (UniqueConstraint("ladder_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ladder_id: Mapped[int] = mapped_column(
        ForeignKey("rank_ladders.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    tier_key: Mapped[str | None] = mapped_column(String(80))
    tier_label: Mapped[str | None] = mapped_column(String(200))
    tier_ordinal: Mapped[int | None] = mapped_column(Integer)
    # The weekly requirement that applied when this roster was taken. Stored rather than
    # re-read later, so changing a quota tomorrow does not rewrite last week's result.
    required_per_week: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = _ts()


class ScheduleJob(Base):
    """A registered callback plus its cron/interval config, editable from Discord."""

    __tablename__ = "schedule_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(80), unique=True)
    callback: Mapped[str] = mapped_column(String(120))
    trigger: Mapped[str] = mapped_column(String(20), default="cron")
    expression: Mapped[str] = mapped_column(String(120), default="")
    timezone: Mapped[str] = mapped_column(String(60), default="UTC")
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WeeklyRun(Base):
    """One evaluation of one period for one binding. Kept for audit and re-runs."""

    __tablename__ = "weekly_runs"
    __table_args__ = (UniqueConstraint("binding_id", "period_start", "period_end"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("channel_bindings.id"), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Where the published report lives, so a re-run can edit it instead of posting
    # a second copy into the channel.
    report_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    report_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # A long roster is split across several messages, so all of their ids are kept as
    # a list in `published_ids`. `report_message_id` is the first one, for convenience.
    published_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _ts()


class WeeklyResult(Base):
    """Per-person outcome of a count. The grain is a Discord user, not a character.

    Counting is per *person*: several characters belonging to one instructor are
    still one instructor's workload. `character_id` stays in the table for reports
    that name a soldier, but is not what determines the grain.
    """

    __tablename__ = "weekly_results"
    __table_args__ = (UniqueConstraint("run_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("weekly_runs.id", ondelete="CASCADE"), index=True
    )
    character_id: Mapped[int | None] = mapped_column(ForeignKey("characters.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    tier_id: Mapped[int | None] = mapped_column(ForeignKey("rank_tiers.id"))
    tier_label: Mapped[str | None] = mapped_column(String(200))
    tier_ordinal: Mapped[int | None] = mapped_column(Integer)
    required_count: Mapped[int] = mapped_column(Integer, default=0)
    approved_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0)
    # Approved by somebody whose reviewing rights could not be confirmed. Counted, but
    # stored separately: without the column the audit trail lost the distinction between
    # "nobody reviewed it", "a reviewer approved it", and "somebody approved it whose role
    # has since changed" - while `met` had already been computed with it.
    unknown_approval_count: Mapped[int] = mapped_column(Integer, default=0)
    met: Mapped[bool] = mapped_column(Boolean, default=False)


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(80), unique=True)
    keep_days: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class Template(Base):
    """Admin-editable bot text. Every user-visible string resolves through here."""

    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("key", "locale"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(120), index=True)
    locale: Mapped[str] = mapped_column(String(5), default="ru")
    body: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
