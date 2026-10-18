"""Offline integration checks for the backup service's shell script."""

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def backup_sandbox(tmp_path):
    if shutil.which("sqlite3") is None:
        pytest.skip("sqlite3 CLI not installed")

    source = (Path(__file__).resolve().parents[1] / "deploy/backup-db.sh").read_text()
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE example (id INTEGER)")
        conn.execute("INSERT INTO example VALUES (1)")

    backup_dir = tmp_path / "backups"
    script = tmp_path / "backup.sh"
    script.write_text(
        source.replace("DB=/opt/pokesketch/data/pokesketch.db", f"DB={database}")
        .replace("DEST_LOCAL=/opt/pokesketch/data/backups", f"DEST_LOCAL={backup_dir}")
    )
    env = {"PATH": os.defpath}
    return script, backup_dir, env


def test_required_remote_backup_rejects_missing_destination(backup_sandbox):
    script, backup_dir, env = backup_sandbox
    env["BACKUP_REQUIRE_REMOTE"] = "1"

    result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert "BACKUP_REMOTE_DEST" in result.stderr
    assert not list(backup_dir.glob("*.db"))


def test_local_backup_is_private_and_consistent(backup_sandbox):
    script, backup_dir, env = backup_sandbox

    result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert "local only" in result.stderr
    snapshots = list(backup_dir.glob("*.db"))
    assert len(snapshots) == 1
    assert snapshots[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(snapshots[0]) as conn:
        assert conn.execute("SELECT id FROM example").fetchone() == (1,)


def test_configured_remote_backup_invokes_rsync(backup_sandbox, tmp_path):
    script, _backup_dir, env = backup_sandbox
    fake_rsync = tmp_path / "rsync"
    fake_rsync.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RSYNC_LOG"\n')
    fake_rsync.chmod(0o755)
    env.update(
        PATH=f"{tmp_path}:{os.defpath}",
        RSYNC_LOG=str(tmp_path / "rsync.log"),
        BACKUP_REQUIRE_REMOTE="1",
        BACKUP_REMOTE_DEST="backup-user@backup-host.example:/path/to/backups/",
        BACKUP_SSH_KEY="/path/to/backup-key",
    )

    result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    invocation = (tmp_path / "rsync.log").read_text()
    assert env["BACKUP_REMOTE_DEST"] in invocation
    assert env["BACKUP_SSH_KEY"] in invocation
    assert "StrictHostKeyChecking=yes" in invocation
