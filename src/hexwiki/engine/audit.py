"""Append-only what/why/how audit and atomic persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def exclusive_text(path: Path, value: str) -> None:
    """Create one file durably and refuse an existing target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if path.exists():
            path.unlink()
        raise


def exclusive_json(path: Path, value: Any) -> None:
    exclusive_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class AuditLog:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._sequence = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"invalid append-only audit JSON on line {line_number}: {error}"
                    ) from error
                if record.get("run_id") != run_id:
                    raise ValueError(
                        f"audit log belongs to run {record.get('run_id')!r}, not {run_id!r}"
                    )
                sequence = record.get("sequence")
                if sequence != self._sequence + 1:
                    raise ValueError(
                        f"audit sequence is not continuous on line {line_number}: {sequence!r}"
                    )
                self._sequence = sequence

    def record(
        self,
        *,
        phase: str,
        action: str,
        what: str,
        why: str,
        how: str,
        status: str = "completed",
        details: dict[str, Any] | None = None,
    ) -> None:
        if not all(str(value).strip() for value in (phase, action, what, why, how)):
            raise ValueError("audit records require phase, action, what, why, and how")
        with self._lock:
            self._sequence += 1
            value = {
                "sequence": self._sequence,
                "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "run_id": self.run_id,
                "phase": phase,
                "action": action,
                "status": status,
                "what": what,
                "why": why,
                "how": how,
                "details": details or {},
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
