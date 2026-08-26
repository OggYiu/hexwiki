"""Privacy and artifact checks over every object in reachable Git history."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from tests.test_public_boundary import (
    ALLOWED_TOP_LEVEL,
    BANNED_HASHES,
    FORBIDDEN_ARTIFACT_SUFFIXES,
    FORBIDDEN_DIRECTORY_NAMES,
    MAX_FIXTURE_BYTES,
    MAX_SOURCE_BYTES,
    SKIPPED_DIRECTORIES,
    TEXT_SUFFIXES,
    ngram_hashes,
    privacy_findings,
)


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_ONLY_TOP_LEVEL = frozenset({".grok-plugin"})


def _git(*arguments: str, text: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=text,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _is_fixture(path: PurePosixPath) -> bool:
    return len(path.parts) >= 2 and path.parts[:2] == ("tests", "fixtures")


def _scan_text(label: str, text: str, offenders: list[str]) -> None:
    if ngram_hashes(text).intersection(BANNED_HASHES):
        offenders.append(f"{label}: private denylist hash")
    offenders.extend(f"{label}: {finding}" for finding in privacy_findings(text))


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="source archive has no Git history")
def test_complete_reachable_git_history_is_public_safe() -> None:
    assert shutil.which("git"), "Git is required when a checkout contains .git"
    assert HISTORICAL_ONLY_TOP_LEVEL.isdisjoint(ALLOWED_TOP_LEVEL)
    assert all(not (ROOT / name).exists() for name in HISTORICAL_ONLY_TOP_LEVEL)
    historical_allowed_top_level = ALLOWED_TOP_LEVEL.union(HISTORICAL_ONLY_TOP_LEVEL)
    commits = str(_git("rev-list", "--all", text=True)).splitlines()
    assert commits, "no reachable commits"

    offenders: list[str] = []
    blobs: dict[str, tuple[int, set[str]]] = {}
    historical_forbidden = FORBIDDEN_DIRECTORY_NAMES.union(
        SKIPPED_DIRECTORIES.difference({".git"})
    )

    for commit in commits:
        raw_commit = bytes(_git("cat-file", "commit", commit)).decode(
            "utf-8", errors="replace"
        )
        _scan_text(f"commit {commit[:12]}", raw_commit, offenders)

        tree = bytes(_git("ls-tree", "-r", "-l", "-z", commit))
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            fields = metadata.split()
            object_type = fields[1].decode("ascii")
            object_id = fields[2].decode("ascii")
            path = PurePosixPath(raw_path.decode("utf-8", errors="surrogateescape"))
            label = f"{commit[:12]}:{path.as_posix()}"

            if object_type != "blob":
                offenders.append(f"{label}: non-file tree entry {object_type}")
                continue
            size = int(fields[3])
            if not path.parts or path.parts[0] not in historical_allowed_top_level:
                offenders.append(f"{label}: unapproved top-level path")
            if historical_forbidden.intersection(path.parts):
                offenders.append(f"{label}: generated/private directory shape")
            if path.suffix.lower() in FORBIDDEN_ARTIFACT_SUFFIXES and not _is_fixture(path):
                offenders.append(f"{label}: generated/archive suffix")
            cap = MAX_FIXTURE_BYTES if _is_fixture(path) else MAX_SOURCE_BYTES
            if size > cap:
                offenders.append(f"{label}: exceeds public size cap")
            if ngram_hashes(path.as_posix()).intersection(BANNED_HASHES):
                offenders.append(f"{label}: private path hash")
            known = blobs.setdefault(object_id, (size, set()))
            known[1].add(path.as_posix())

    for object_id, (size, paths) in blobs.items():
        if size > MAX_SOURCE_BYTES:
            continue
        text_paths = sorted(
            path for path in paths if PurePosixPath(path).suffix.lower() in TEXT_SUFFIXES
        )
        if not text_paths:
            continue
        content = bytes(_git("cat-file", "blob", object_id)).decode(
            "utf-8", errors="replace"
        )
        _scan_text(f"blob {object_id[:12]} ({text_paths[0]})", content, offenders)

    refs = str(
        _git(
            "for-each-ref",
            "--format=%(objecttype)\t%(objectname)\t%(refname)",
            text=True,
        )
    ).splitlines()
    for record in refs:
        object_type, object_id, ref_name = record.split("\t", 2)
        _scan_text(ref_name, ref_name, offenders)
        if object_type == "tag":
            tag = bytes(_git("cat-file", "tag", object_id)).decode(
                "utf-8", errors="replace"
            )
            _scan_text(ref_name, tag, offenders)

    assert offenders == []
