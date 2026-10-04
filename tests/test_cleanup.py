"""scripts/cleanup_data.sh: lists, then deletes, only regenerable working files."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_data.sh"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

REMOVED_BY_DEFAULT = [
    "data/utxo-900.dat",
    "data/utxo-900.targets.npy",
    ".btc_trace_cache/reveal-900/0000000-0000999.npy",
    ".btc_trace_cache/abc.scan.json",
    ".btc_trace_cache/abc.blocks.jsonl",
]
KEPT_UNLESS_ALL = ["data/utxo-900.revealed.npy", "data/utxo-900.revealed.json"]
ALWAYS_KEPT = [
    "data/utxo-900.json",
    "data/sanctioned_xbt.json",
    "reports/utxo-900.json",
    "docs/quantum/index.html",
    "findings/quantum.md",
    "SDN_ADVANCED.XML",
]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "cleanup_data.sh")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "btc-trace"\n')
    for name in REMOVED_BY_DEFAULT + KEPT_UNLESS_ALL + ALWAYS_KEPT:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 4096)
    return tmp_path


def run(project, *args, cwd=None):
    return subprocess.run(  # noqa: S603 - runs the project's own script on test files
        [BASH, str(project / "scripts" / "cleanup_data.sh"), *args],
        cwd=cwd or project,
        capture_output=True,
        text=True,
        check=False,
    )


def existing(project, names):
    return [n for n in names if (project / n).exists()]


def test_dry_run_deletes_nothing(project):
    result = run(project)
    assert result.returncode == 0, result.stderr
    assert "dry run" in result.stdout and "would be freed" in result.stdout
    assert "data/utxo-900.dat" in result.stdout
    assert "revealed.npy" in result.stdout  # mentioned as kept, not listed for deletion
    assert existing(project, REMOVED_BY_DEFAULT) == REMOVED_BY_DEFAULT


def test_yes_deletes_the_default_set_only(project):
    result = run(project, "--yes", cwd=project / "findings")  # works from a subfolder
    assert result.returncode == 0, result.stderr
    assert "freed" in result.stdout
    assert existing(project, REMOVED_BY_DEFAULT) == []
    assert existing(project, KEPT_UNLESS_ALL + ALWAYS_KEPT) == KEPT_UNLESS_ALL + ALWAYS_KEPT
    assert "Nothing to clean up" in run(project).stdout


def test_all_includes_the_reveal_results(project):
    assert run(project, "--all", "--yes").returncode == 0
    assert existing(project, KEPT_UNLESS_ALL) == []
    assert existing(project, ALWAYS_KEPT) == ALWAYS_KEPT


def test_refuses_outside_the_project(project):
    (project / "pyproject.toml").write_text('[project]\nname = "something-else"\n')
    result = run(project, "--yes")
    assert result.returncode == 1 and "does not look like" in result.stderr
    assert existing(project, REMOVED_BY_DEFAULT) == REMOVED_BY_DEFAULT


def test_unknown_option(project):
    assert run(project, "--yse").returncode == 2
