"""Exercise the deploy checkout updater against disposable Git repositories."""

import subprocess
from pathlib import Path

import pytest

UPDATE_SCRIPT = Path(__file__).resolve().parents[1] / "deploy/update-checkout.sh"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    deployed = tmp_path / "deployed"
    git("init", "--bare", "-b", "master", str(remote), cwd=tmp_path)
    git("init", "-b", "master", str(seed), cwd=tmp_path)
    (seed / "file.txt").write_text("first\n")
    git("add", "file.txt", cwd=seed)
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "origin", "master", cwd=seed)
    git("clone", str(remote), str(deployed), cwd=tmp_path)
    (deployed / "local-only.txt").write_text("preserve me")
    return seed, deployed


def test_fast_forward_preserves_untracked_files(checkout):
    seed, deployed = checkout
    (seed / "file.txt").write_text("second\n")
    git("add", "file.txt", cwd=seed)
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "second", cwd=seed)
    git("push", "origin", "master", cwd=seed)

    subprocess.run(["bash", str(UPDATE_SCRIPT), str(deployed)], check=True, capture_output=True, text=True)

    assert git("rev-parse", "HEAD", cwd=deployed) == git("rev-parse", "HEAD", cwd=seed)
    assert (deployed / "local-only.txt").read_text() == "preserve me"


def test_history_rewrite_refuses_implicit_reset(checkout, tmp_path):
    seed, deployed = checkout
    original_head = git("rev-parse", "HEAD", cwd=deployed)
    rewritten = tmp_path / "rewritten"
    git("init", "-b", "master", str(rewritten), cwd=tmp_path)
    (rewritten / "file.txt").write_text("rewritten\n")
    git("add", "file.txt", cwd=rewritten)
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "rewrite", cwd=rewritten)
    git("remote", "add", "origin", git("remote", "get-url", "origin", cwd=seed), cwd=rewritten)
    git("push", "--force", "origin", "master", cwd=rewritten)

    result = subprocess.run(["bash", str(UPDATE_SCRIPT), str(deployed)], capture_output=True, text=True)

    assert result.returncode != 0
    assert "diverged" in result.stderr.lower()
    assert git("rev-parse", "HEAD", cwd=deployed) == original_head
    assert (deployed / "local-only.txt").read_text() == "preserve me"


def test_dirty_tracked_checkout_is_rejected(checkout):
    _seed, deployed = checkout
    (deployed / "file.txt").write_text("local change\n")

    result = subprocess.run(["bash", str(UPDATE_SCRIPT), str(deployed)], capture_output=True, text=True)

    assert result.returncode != 0
    assert "tracked changes" in result.stderr.lower()
    assert (deployed / "file.txt").read_text() == "local change\n"
