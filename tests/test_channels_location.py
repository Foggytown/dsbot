"""The channels file lives in the data directory, not in the checkout.

The panel keeps only /data and replaces everything else on each deploy. Putting the
channel list in the checkout would mean every redeploy reset it; putting it in /data lets
an admin edit it on the server and have the bot apply it without a restart.

The checked-in `forms/channels.yaml` is a seed, copied once, never re-copied: a redeploy
must not overwrite what an admin wrote.
"""

from __future__ import annotations

import time
from pathlib import Path

from dsbot.config import Settings
from dsbot.modules.reports.channels import (
    ChannelsFile,
    file_signature,
    load_channels_file,
    seed_channels_file,
)


def settings(data_dir: Path) -> Settings:
    return Settings(_env_file=None, data_dir=str(data_dir))  # type: ignore[call-arg]


def test_the_channels_file_defaults_to_the_data_directory(tmp_path: Path) -> None:
    resolved = settings(tmp_path).resolved_channels_file()
    assert resolved == tmp_path / "channels.yaml", "must live on the persistent volume"


def test_an_explicit_path_still_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.yaml"
    resolved = Settings(_env_file=None, channels_file=str(explicit)).resolved_channels_file()  # type: ignore[call-arg]
    assert resolved == explicit


def test_seeding_copies_the_template_once(tmp_path: Path) -> None:
    template = tmp_path / "forms" / "channels.yaml"
    template.parent.mkdir(parents=True)
    template.write_text(
        "channels:\n  - guild: 1\n    channel: 2\n    form: training_report\n",
        encoding="utf-8",
    )
    target = tmp_path / "data" / "channels.yaml"

    assert seed_channels_file(target, template) is True
    assert target.is_file()
    assert len(load_channels_file(target).channels) == 1


def test_seeding_never_overwrites_an_admin_edit(tmp_path: Path) -> None:
    """The whole point of moving it: a redeploy must not undo a server-side change."""
    template = tmp_path / "forms" / "channels.yaml"
    template.parent.mkdir(parents=True)
    template.write_text("channels:\n  - guild: 1\n    channel: 2\n", encoding="utf-8")

    target = tmp_path / "data" / "channels.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("channels:\n  - guild: 9\n    channel: 99\n", encoding="utf-8")

    assert seed_channels_file(target, template) is False
    spec = load_channels_file(target)
    assert spec.channels[0].channel == 99, "the admin's edit was overwritten"


def test_a_missing_template_is_not_fatal(tmp_path: Path) -> None:
    assert seed_channels_file(tmp_path / "x.yaml", tmp_path / "absent.yaml") is False


def test_a_missing_file_means_no_channels_not_an_error(tmp_path: Path) -> None:
    spec: ChannelsFile = load_channels_file(tmp_path / "absent.yaml")
    assert spec.channels == []


def test_signature_changes_when_the_file_is_edited(tmp_path: Path) -> None:
    path = tmp_path / "channels.yaml"
    path.write_text("channels: []\n", encoding="utf-8")
    first = file_signature(path)
    assert first is not None

    time.sleep(0.01)
    path.write_text("channels:\n  - guild: 1\n    channel: 2\n", encoding="utf-8")
    second = file_signature(path)
    assert second != first, "an edit must be detectable so it can be applied live"


def test_signature_is_stable_when_nothing_changes(tmp_path: Path) -> None:
    path = tmp_path / "channels.yaml"
    path.write_text("channels: []\n", encoding="utf-8")
    assert file_signature(path) == file_signature(path)


def test_a_removed_file_has_no_signature(tmp_path: Path) -> None:
    assert file_signature(tmp_path / "gone.yaml") is None


def test_an_invalid_edit_is_rejected_by_the_loader(tmp_path: Path) -> None:
    """The bot logs and keeps running; the loader must refuse rather than half-apply."""
    path = tmp_path / "channels.yaml"
    path.write_text("channels:\n  - guild: 1\n    channel: 2\n  - guild: 1\n    channel: 2\n")
    try:
        load_channels_file(path)
    except ValueError as exc:
        assert "listed twice" in str(exc)
    else:
        raise AssertionError("a duplicate channel must be rejected")
