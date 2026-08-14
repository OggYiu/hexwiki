"""Bounded smoke/build orchestration, exact binding, and terminal-state records."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from hexwiki import __version__

from . import agent as agent_engine
from . import review, source
from .audit import (
    AuditLog,
    atomic_json,
    atomic_text,
    exclusive_json,
    exclusive_text,
    sha256_file,
)
from .config import ConfigError, RuntimeConfig, TranscriptRecorder, load_runtime_config
from .finalize import prepare_wiki, publish_candidate, seal_wiki, verify_checksums
from .profile import (
    DocumentProfile,
    load_profile,
    load_profile_lock,
    sha256_json,
)


DEPENDENCIES = (
    "hexwiki",
    "PyMuPDF",
    "Pillow",
    "deepagents",
    "langchain-core",
    "langchain-openai",
    "langgraph",
    "openai",
    "PyYAML",
    "langfuse",
)
REQUIRED_STAGE_PREFIXES = (
    "survey:",
    "survey-audit:",
    "structure:",
    "cases:",
    "concepts:",
    "people:",
    "synthesis:",
    "navigation:",
)
NETWORK_TOOL_MARKERS = ("browse", "fetch", "http", "internet", "web_search")
MEMORY_TOOL_MARKERS = ("checkpoint", "memory", "recall", "remember")


class RunFailure(RuntimeError):
    category = "runtime"
    exit_code = 4


class ConfigurationFailure(RunFailure):
    category = "configuration"
    exit_code = 2


class PreflightFailure(RunFailure):
    category = "preflight"
    exit_code = 3


class WorkflowFailure(RunFailure):
    category = "runtime"
    exit_code = 4


class ValidationFailure(RunFailure):
    category = "validation"
    exit_code = 5


@dataclass(frozen=True)
class RuntimeServices:
    """Injectable provider boundary used by the network-free integration suite."""

    preflight: Callable[..., dict[str, Any]]
    executor_factory: Callable[..., agent_engine.StageExecutor]
    reviewer_factory: Callable[..., review.ReviewClient]
    sleep: Callable[[float], None] = time.sleep
    provider_kind: str = "stub"


def _canonical_hash(value: Any) -> str:
    return sha256_json(value)


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def dependency_versions() -> dict[str, str]:
    return {name: _distribution_version(name) for name in DEPENDENCIES}


def _resource_inventory() -> list[dict[str, Any]]:
    root = resources.files("hexwiki")
    records: list[dict[str, Any]] = []

    def walk(node: Any, prefix: PurePosixPath) -> None:
        for child in sorted(node.iterdir(), key=lambda item: item.name):
            relative = prefix / child.name
            if child.is_dir():
                if child.name != "__pycache__":
                    walk(child, relative)
                continue
            if child.name.endswith((".pyc", ".pyo")):
                continue
            content = child.read_bytes()
            records.append(
                {
                    "path": relative.as_posix(),
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )

    walk(root, PurePosixPath("hexwiki"))
    return records


def compute_binding(
    profile: DocumentProfile,
    lock: dict[str, Any],
    runtime: RuntimeConfig,
) -> dict[str, Any]:
    """Bind smoke validity to installed code, source, profile, route, and limits."""
    package_tree = _resource_inventory()
    value = {
        "hexwiki_version": __version__,
        "package_tree_sha256": _canonical_hash(package_tree),
        "package_tree": package_tree,
        "dependencies": dependency_versions(),
        "profile_id": profile.data["profile_id"],
        "profile_sha256": profile.profile_sha256,
        "profile_lock_sha256": sha256_json(lock),
        "source_pdf_sha256": lock["source"]["pdf_sha256"],
        "canonical_scope_sha256": lock["scope"]["canonical_sha256"],
        "route": runtime.binding(),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    value["binding_sha256"] = _canonical_hash(value)
    return value


def _timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")


def _create_run_root(path: Path, kind: str) -> tuple[str, Path]:
    root = Path(path).expanduser().resolve()
    if root.exists():
        raise FileExistsError(f"explicit run directory already exists: {root}")
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(exist_ok=False)
    return f"hexwiki-{kind}-{_timestamp()}-{os.getpid()}", root


def _write_progress(run_root: Path, run_id: str, status: str, **details: Any) -> None:
    atomic_json(
        run_root / "progress.json",
        {
            "run_id": run_id,
            "status": status,
            "heartbeat": datetime.now().astimezone().isoformat(timespec="seconds"),
            **details,
        },
    )


def _write_review_index(run_root: Path) -> None:
    review_root = run_root / "review"
    review_root.mkdir(exist_ok=True)
    records: list[dict[str, Any]] = []
    for path in sorted(run_root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or review_root in path.parents:
            continue
        records.append(
            {
                "path": path.relative_to(run_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    index = review_root / "index.json"
    exclusive_json(index, {"status": "complete", "artifacts": records})
    exclusive_text(review_root / "index.sha256", f"{sha256_file(index)}  index.json\n")


def _filesystem_preflight(
    run_root: Path,
    recorder: TranscriptRecorder,
) -> dict[str, Any]:
    root = run_root / "preflight" / "sandbox"
    root.mkdir(parents=True, exist_ok=False)
    (root / "_ingest").mkdir()
    atomic_text(root / "_ingest" / "source.md", "immutable\n")
    backend, delete_tool = agent_engine._guarded_backend(root, recorder)
    allowed = backend.write("notes/probe.md", "probe")
    if getattr(allowed, "error", None) or not (root / "notes" / "probe.md").is_file():
        raise PreflightFailure("filesystem sandbox rejected an in-root write")
    immutable = backend.write("_ingest/source.md", "changed")
    if not getattr(immutable, "error", None):
        raise PreflightFailure("filesystem sandbox allowed an immutable evidence write")
    traversal_refused = False
    try:
        traversal = backend.write("../escape.md", "escape")
        traversal_refused = bool(getattr(traversal, "error", None))
    except (ValueError, agent_engine.SandboxViolation):
        traversal_refused = True
    if not traversal_refused or (root.parent / "escape.md").exists():
        raise PreflightFailure("filesystem sandbox allowed a traversal write")
    deleted = delete_tool.invoke({"file_path": "notes/probe.md"})
    if (root / "notes" / "probe.md").exists() or "Deleted" not in str(deleted):
        raise PreflightFailure("guarded delete did not remove one mutable in-root file")
    refused = delete_tool.invoke({"file_path": "_ingest/source.md"})
    if not (root / "_ingest" / "source.md").is_file() or "Refused" not in str(refused):
        raise PreflightFailure("guarded delete did not protect immutable evidence")
    return {
        "status": "passed",
        "in_root_write": "passed",
        "traversal_write": "refused",
        "immutable_write": "refused",
        "guarded_delete": "passed",
    }


def network_preflight(
    *,
    runtime: RuntimeConfig,
    run_root: Path,
    audit: AuditLog,
    recorder: TranscriptRecorder,
) -> dict[str, Any]:
    """Probe route discovery, chat, tool calling, sandbox, and observability separately."""
    try:
        from openai import OpenAI
    except ImportError as error:
        raise PreflightFailure(
            "model runtime is unavailable; install HexWiki with the 'model' extra"
        ) from error
    components: dict[str, Any] = {}
    started = time.monotonic()
    try:
        components["filesystem_sandbox"] = _filesystem_preflight(run_root, recorder)
        client = OpenAI(
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            timeout=30,
            max_retries=0,
        )
        models = [item.id for item in client.models.list().data]
        if runtime.model not in models:
            raise PreflightFailure(
                f"configured model {runtime.model!r} was not returned by model discovery"
            )
        components["model_discovery"] = {
            "status": "passed",
            "target": runtime.model,
            "model_count": len(models),
        }
        response = client.chat.completions.create(
            model=runtime.model,
            messages=[
                {
                    "role": "user",
                    "content": "Reply with exactly HEXWIKI_PREFLIGHT_OK.",
                }
            ],
            max_tokens=24,
            temperature=0,
        )
        content = response.choices[0].message.content or ""
        if "HEXWIKI_PREFLIGHT_OK" not in content:
            raise PreflightFailure("chat preflight returned an unexpected response")
        components["chat"] = {"status": "passed", "response_marker": "matched"}
        tool_response = client.chat.completions.create(
            model=runtime.model,
            messages=[
                {
                    "role": "user",
                    "content": "Call hexwiki_probe with value ready; do not answer in prose.",
                }
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "hexwiki_probe",
                        "description": "Return the requested preflight marker.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": "hexwiki_probe"}},
            max_tokens=64,
            temperature=0,
        )
        calls = tool_response.choices[0].message.tool_calls or []
        if not calls or calls[0].function.name != "hexwiki_probe":
            raise PreflightFailure("tool-calling preflight produced no required tool call")
        arguments = json.loads(calls[0].function.arguments)
        if arguments.get("value") != "ready":
            raise PreflightFailure("tool-calling preflight returned the wrong arguments")
        components["tool_calling"] = {"status": "passed", "tool": "hexwiki_probe"}
    except PreflightFailure:
        raise
    except BaseException as error:
        raise PreflightFailure(
            f"provider preflight failed: {type(error).__name__}: {error}"
        ) from error

    if runtime.observability_enabled:
        try:
            from langfuse import get_client

            client = get_client()
            authenticated = bool(client.auth_check())
            components["observability"] = {
                "status": "passed" if authenticated else "failed",
                "required": False,
            }
        except BaseException as error:
            components["observability"] = {
                "status": "failed",
                "required": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
    else:
        components["observability"] = {"status": "disabled", "required": False}

    report = {
        "status": "passed",
        "route": runtime.binding(),
        "components": components,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    audit.record(
        phase="preflight",
        action="pass_runtime_preflight",
        what="Passed model discovery, chat, tool calling, and filesystem sandbox probes.",
        why="An expensive workflow must not start until each required runtime capability works.",
        how="Ran each bounded probe independently and recorded optional observability separately.",
        details={"components": components, "duration_seconds": report["duration_seconds"]},
    )
    return report


def _perform_preflight(
    *,
    runtime: RuntimeConfig,
    run_root: Path,
    audit: AuditLog,
    recorder: TranscriptRecorder,
    services: RuntimeServices | None,
) -> dict[str, Any]:
    try:
        report = (
            services.preflight(
                runtime=runtime,
                run_root=run_root,
                audit=audit,
                recorder=recorder,
            )
            if services is not None
            else network_preflight(
                runtime=runtime,
                run_root=run_root,
                audit=audit,
                recorder=recorder,
            )
        )
    except PreflightFailure:
        raise
    except BaseException as error:
        raise PreflightFailure(
            f"preflight failed: {type(error).__name__}: {error}"
        ) from error
    if not isinstance(report, dict) or report.get("status") != "passed":
        raise PreflightFailure(f"preflight did not pass: {report!r}")
    output = run_root / "preflight" / "report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    exclusive_json(output, report)
    return report


def _activity(run_root: Path, console: Path) -> dict[str, Any]:
    paths = [console]
    transcript = run_root / "stage-transcripts"
    if transcript.is_dir():
        paths.extend(path for path in transcript.rglob("*") if path.is_file())
    existing = [path for path in paths if path.is_file()]
    return {
        "console_bytes": console.stat().st_size if console.is_file() else 0,
        "transcript_bytes": sum(path.stat().st_size for path in existing if path != console),
        "latest_artifact_mtime": max(
            (path.stat().st_mtime for path in existing), default=None
        ),
    }


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=20,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)


def _launch_child(
    *,
    run_root: Path,
    run_id: str,
    profile_path: Path,
    lock_path: Path,
    raw_path: Path,
    extraction_root: Path,
    candidate: Path,
    mode: str,
    runtime: RuntimeConfig,
    absolute_deadline: float,
    audit: AuditLog,
) -> dict[str, Any]:
    console = run_root / "agent-console.log"
    remaining = max(1, int(absolute_deadline - time.monotonic()))
    child_seconds = max(1, remaining - runtime.limits.child_margin_seconds)
    recursion_limit = (
        runtime.limits.smoke_recursion_limit
        if mode == "smoke"
        else runtime.limits.build_recursion_limit
    )
    command = [
        sys.executable,
        "-u",
        "-m",
        "hexwiki.engine.agent",
        "--profile",
        str(profile_path),
        "--profile-lock",
        str(lock_path),
        "--raw",
        str(raw_path),
        "--extraction",
        str(extraction_root),
        "--wiki-dir",
        str(candidate),
        "--run-dir",
        str(run_root),
        "--run-id",
        run_id,
        "--mode",
        mode,
        "--recursion-limit",
        str(recursion_limit),
        "--deadline-seconds",
        str(child_seconds),
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "HEXWIKI_BASE_URL": runtime.base_url,
            "HEXWIKI_MODEL": runtime.model,
            "HEXWIKI_API_KEY": runtime.api_key,
            "HEXWIKI_CONFIG_DIR": str(runtime.config_dir),
            "HEXWIKI_RUNS_DIR": str(runtime.runs_dir),
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    audit.record(
        phase="agent",
        action="launch_guarded_compilation_child",
        what=f"Launched the {mode} compiler in a separately bounded child process.",
        why="The parent must enforce wall-clock limits independently of provider timeouts.",
        how="Used an exact process group, child self-deadline, parent deadline, and heartbeat.",
        details={
            "mode": mode,
            "child_deadline_seconds": child_seconds,
            "recursion_limit": recursion_limit,
        },
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    started = time.monotonic()
    process: subprocess.Popen[Any] | None = None
    with console.open("x", encoding="utf-8", errors="replace", newline="\n") as handle:
        try:
            process = subprocess.Popen(
                command,
                cwd=run_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                start_new_session=os.name != "nt",
            )
            last_heartbeat = 0.0
            while True:
                code = process.poll()
                now = time.monotonic()
                if now - last_heartbeat >= runtime.limits.heartbeat_seconds:
                    _write_progress(
                        run_root,
                        run_id,
                        "running" if code is None else "child-exited",
                        phase=f"agent-{mode}",
                        pid=process.pid,
                        child_exit_code=code,
                        elapsed_seconds=round(now - started, 3),
                        absolute_seconds_remaining=round(
                            max(0, absolute_deadline - now), 3
                        ),
                        **_activity(run_root, console),
                    )
                    last_heartbeat = now
                if code is not None:
                    break
                if now >= absolute_deadline:
                    _terminate_process_tree(process)
                    raise WorkflowFailure(
                        f"compilation child exceeded the {mode} absolute deadline"
                    )
                time.sleep(min(5, runtime.limits.heartbeat_seconds))
        finally:
            if process is not None and process.poll() is None:
                _terminate_process_tree(process)
    if process is None or process.returncode != 0:
        tail = console.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        raise WorkflowFailure(
            f"compilation child exited {None if process is None else process.returncode}; "
            f"console tail: {' | '.join(tail)}"
        )
    result_path = run_root / "child-result.json"
    if not result_path.is_file():
        raise WorkflowFailure("compilation child exited zero without child-result.json")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    audit.record(
        phase="agent",
        action="complete_guarded_compilation_child",
        what=f"The {mode} compilation child exited successfully.",
        why="Only an explicit zero exit and retained result may advance to validation.",
        how="Observed the exact PID and retained console, stage, model, and tool records.",
        details={"duration_seconds": round(time.monotonic() - started, 3)},
    )
    return result


def _merge_child_audit(run_root: Path, audit: AuditLog) -> int:
    path = run_root / "child-actions.jsonl"
    if not path.is_file():
        return 0
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for record in records:
        audit.record(
            phase=f"child/{record['phase']}",
            action=f"child/{record['action']}",
            what=record["what"],
            why=record["why"],
            how=record["how"],
            status=record.get("status", "completed"),
            details={
                "child_sequence": record.get("sequence"),
                **record.get("details", {}),
            },
        )
    return len(records)


def _model_metrics(
    run_root: Path,
    child_result: dict[str, Any],
    provider_kind: str,
) -> dict[str, Any]:
    def events(name: str) -> list[dict[str, Any]]:
        path = run_root / "stage-transcripts" / name
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    model_events = events("model-events.jsonl")
    tool_events = events("tool-events.jsonl")
    labels = child_result.get("stage_labels", [])
    if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
        raise ValidationFailure("child result has no valid stage label inventory")
    missing = [
        prefix for prefix in REQUIRED_STAGE_PREFIXES if not any(label.startswith(prefix) for label in labels)
    ]
    if missing:
        raise ValidationFailure(f"workflow did not exercise required stage families: {missing}")
    if provider_kind == "network":
        requests = sum(item.get("event") == "model-request" for item in model_events)
        responses = sum(item.get("event") == "model-response" for item in model_events)
        tool_requests = sum(item.get("event") == "tool-request" for item in tool_events)
        if not requests or not responses or not tool_requests:
            raise ValidationFailure(
                "full local provider callbacks are incomplete: "
                f"requests={requests}, responses={responses}, tools={tool_requests}"
            )
    else:
        requests = len(labels)
        responses = len(labels)
        tool_requests = len(labels)
    return {
        "provider_kind": provider_kind,
        "stage_count": len(labels),
        "stage_labels": labels,
        "model_attempts": requests,
        "model_responses": responses,
        "model_errors": sum(item.get("event") == "model-error" for item in model_events),
        "tool_attempts": tool_requests,
        "full_local_payload_logging": True,
        "observability_required": False,
    }


def observe_isolation(
    run_root: Path,
    candidate: Path,
    preflight: dict[str, Any],
    audit: AuditLog,
) -> dict[str, Any]:
    tool_names: set[str] = set()
    path = run_root / "stage-transcripts" / "tool-events.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            serialized = record.get("serialized")
            if isinstance(serialized, dict):
                name = serialized.get("name") or serialized.get("id")
                if isinstance(name, list) and name:
                    name = name[-1]
                if name:
                    tool_names.add(str(name).casefold())
    sandbox_report = preflight.get("components", {}).get("filesystem_sandbox", {})
    observed = {
        "sandbox_root": candidate.name,
        "filesystem_sandbox_passed": sandbox_report.get("status") == "passed",
        "immutable_source_writes_refused": sandbox_report.get("immutable_write") == "refused",
        "traversal_writes_refused": sandbox_report.get("traversal_write") == "refused",
        "network_tools_invoked": any(
            marker in name for name in tool_names for marker in NETWORK_TOOL_MARKERS
        ),
        "memory_tools_invoked": any(
            marker in name for name in tool_names for marker in MEMORY_TOOL_MARKERS
        ),
        "gold_or_prior_wiki_mounted": False,
        "shared_skill_mounted": False,
        "tool_names": sorted(tool_names),
    }
    if (
        not observed["filesystem_sandbox_passed"]
        or not observed["immutable_source_writes_refused"]
        or not observed["traversal_writes_refused"]
        or observed["network_tools_invoked"]
        or observed["memory_tools_invoked"]
    ):
        raise ValidationFailure(f"run isolation evidence is not clean: {observed}")
    audit.record(
        phase="validation",
        action="observe_run_isolation",
        what="Recorded sandbox, immutability, traversal, network-tool, and memory-tool evidence.",
        why="Isolation has to be observed from capabilities and transcripts, not asserted.",
        how="Combined the live sandbox preflight with the invoked tool-name inventory.",
        details=observed,
    )
    return observed


def _compile(
    *,
    services: RuntimeServices | None,
    run_root: Path,
    run_id: str,
    profile_path: Path,
    lock_path: Path,
    staged: dict[str, Any],
    candidate: Path,
    mode: str,
    runtime: RuntimeConfig,
    deadline: float,
    audit: AuditLog,
    recorder: TranscriptRecorder,
) -> dict[str, Any]:
    if services is None:
        return _launch_child(
            run_root=run_root,
            run_id=run_id,
            profile_path=profile_path,
            lock_path=lock_path,
            raw_path=staged["raw_path"],
            extraction_root=staged["profile"].extraction_root
            if "profile" in staged
            else staged["extraction_root"],
            candidate=candidate,
            mode=mode,
            runtime=runtime,
            absolute_deadline=deadline,
            audit=audit,
        )
    recursion_limit = (
        runtime.limits.smoke_recursion_limit
        if mode == "smoke"
        else runtime.limits.build_recursion_limit
    )
    executor = services.executor_factory(
        wiki_dir=candidate,
        runtime=runtime,
        recorder=recorder,
        recursion_limit=recursion_limit,
    )
    reviewer_client = services.reviewer_factory(runtime=runtime, recorder=recorder)
    result = agent_engine.compile_wiki(
        executor=executor,
        reviewer=reviewer_client,
        wiki_dir=candidate,
        raw_path=staged["raw_path"],
        extraction_root=staged["extraction_root"],
        profile=staged["runtime"],
        audit=audit,
        recorder=recorder,
        runtime=runtime,
        smoke=mode == "smoke",
        sleep=services.sleep,
    )
    exclusive_json(run_root / "child-result.json", result)
    return result


def _start_run(
    *,
    kind: str,
    profile_path: Path,
    lock_path: Path,
    run_dir: Path,
    runtime: RuntimeConfig,
) -> tuple[
    str,
    Path,
    AuditLog,
    TranscriptRecorder,
    DocumentProfile,
    dict[str, Any],
    dict[str, Any],
    float,
    float,
]:
    try:
        profile = load_profile(profile_path)
        lock = load_profile_lock(lock_path)
        source.verify_profile_lock(profile, lock)
        binding = compute_binding(profile, lock, runtime)
    except (OSError, ValueError) as error:
        raise ConfigurationFailure(f"profile/lock binding failed: {error}") from error
    run_id, run_root = _create_run_root(run_dir, kind)
    audit = AuditLog(run_root / "actions.jsonl", run_id)
    recorder = TranscriptRecorder(run_root / "stage-transcripts", run_id)
    started = time.monotonic()
    maximum = (
        runtime.limits.smoke_max_seconds
        if kind == "smoke"
        else runtime.limits.build_max_seconds
    )
    deadline = started + maximum
    exclusive_json(
        run_root / "run.json",
        {
            "run_id": run_id,
            "kind": kind,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "absolute_limit_seconds": maximum,
            "binding": binding,
            "profile_file": profile.path.name,
            "profile_lock_file": Path(lock_path).name,
        },
    )
    audit.record(
        phase="run",
        action="bind_fresh_bounded_run",
        what=f"Created fresh {kind} run {run_id} with an exact identity binding.",
        why="Smoke validity requires exact code, source, route, dependency, and limit identity.",
        how="Hashed installed package resources, profile lock, source identity, route, and runtime.",
        details={
            "binding_sha256": binding["binding_sha256"],
            "maximum_seconds": maximum,
        },
    )
    return (
        run_id,
        run_root,
        audit,
        recorder,
        profile,
        lock,
        binding,
        started,
        deadline,
    )


def _record_failure(
    *,
    run_id: str,
    run_root: Path,
    audit: AuditLog,
    binding: dict[str, Any],
    error: BaseException,
    started: float,
) -> None:
    category = getattr(error, "category", "runtime")
    exit_code = int(getattr(error, "exit_code", 4))
    audit.record(
        phase="run",
        action="record_terminal_failure",
        what=f"Stopped the run in terminal {category} failure.",
        why="No failed component, validator, or deadline may be retried above the run level.",
        how="Retained the candidate and exact traceback without publication or automatic rerun.",
        status="failed",
        details={"error_type": type(error).__name__, "error": str(error)},
    )
    exclusive_json(
        run_root / "failure.json",
        {
            "status": "failed",
            "run_id": run_id,
            "category": category,
            "exit_code": exit_code,
            "binding_sha256": binding["binding_sha256"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
        },
    )
    _write_progress(
        run_root,
        run_id,
        "failed",
        category=category,
        exit_code=exit_code,
        error_type=type(error).__name__,
        error=str(error),
    )
    exclusive_json(
        run_root / "terminal.json",
        {
            "run_id": run_id,
            "state": "failed",
            "category": category,
            "exit_code": exit_code,
            "failure_sha256": sha256_file(run_root / "failure.json"),
        },
    )
    _write_review_index(run_root)


def _smoke_report(path: Path, binding: dict[str, Any]) -> tuple[dict[str, Any], str]:
    report_path = Path(path).expanduser().resolve()
    checksum = report_path.with_suffix(".sha256")
    if not report_path.is_file() or not checksum.is_file():
        raise ConfigurationFailure("smoke report or its checksum is missing")
    line = checksum.read_text(encoding="utf-8").strip()
    expected, separator, name = line.partition("  ")
    if not separator or name != report_path.name or expected != sha256_file(report_path):
        raise ConfigurationFailure("smoke report checksum does not match")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigurationFailure(f"smoke report is invalid JSON: {error}") from error
    digest = sha256_file(report_path)
    terminal_path = report_path.parent / "terminal.json"
    run_path = report_path.parent / "run.json"
    if not terminal_path.is_file() or not run_path.is_file():
        raise ConfigurationFailure("smoke terminal or run binding is missing")
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if terminal.get("state") != "passed" or terminal.get("kind") != "smoke":
        raise ConfigurationFailure("smoke terminal state is not passed")
    if terminal.get("smoke_report_sha256") != digest:
        raise ConfigurationFailure("smoke terminal does not bind this report")
    if report.get("status") != "passed" or report.get("corrected") is not False:
        raise ConfigurationFailure("smoke report is failed or marked corrected")
    if report.get("creates_published_wiki") is not False:
        raise ConfigurationFailure("smoke report publication declaration is invalid")
    try:
        expires = datetime.fromisoformat(str(report["expires_at"]))
    except (KeyError, ValueError) as error:
        raise ConfigurationFailure("smoke report expiry is invalid") from error
    if expires <= datetime.now().astimezone():
        raise ConfigurationFailure("smoke report expired")
    if run.get("binding", {}).get("binding_sha256") != report.get("binding_sha256"):
        raise ConfigurationFailure("smoke run binding and report disagree")
    if report.get("binding_sha256") != binding["binding_sha256"]:
        raise ConfigurationFailure("smoke report does not match the current exact binding")
    return report, digest


def run_smoke(
    *,
    profile_path: Path,
    lock_path: Path,
    run_dir: Path,
    runtime: RuntimeConfig | None = None,
    services: RuntimeServices | None = None,
) -> Path:
    try:
        runtime = runtime or load_runtime_config(require_network=True)
    except ConfigError as error:
        raise ConfigurationFailure(str(error)) from error
    state = _start_run(
        kind="smoke",
        profile_path=profile_path,
        lock_path=lock_path,
        run_dir=run_dir,
        runtime=runtime,
    )
    (
        run_id,
        run_root,
        audit,
        recorder,
        profile,
        lock,
        binding,
        started,
        deadline,
    ) = state
    candidate = run_root / "work" / "diagnostic-wiki"
    try:
        preflight = _perform_preflight(
            runtime=runtime,
            run_root=run_root,
            audit=audit,
            recorder=recorder,
            services=services,
        )
        staged = source.stage_source(
            profile=profile,
            lock=lock,
            work_root=run_root / "work" / "source-stage",
            audit=audit,
        )
        staged["extraction_root"] = profile.extraction_root
        prepare_wiki(
            wiki_dir=candidate,
            pages=staged["pages"],
            profile=staged["runtime"],
            audit=audit,
            run_id=run_id,
            extraction_root=profile.extraction_root,
        )
        child_result = _compile(
            services=services,
            run_root=run_root,
            run_id=run_id,
            profile_path=profile.path,
            lock_path=Path(lock_path).resolve(),
            staged=staged,
            candidate=candidate,
            mode="smoke",
            runtime=runtime,
            deadline=deadline,
            audit=audit,
            recorder=recorder,
        )
        if services is None:
            _merge_child_audit(run_root, audit)
        provider_kind = services.provider_kind if services else "network"
        metrics = _model_metrics(run_root, child_result, provider_kind)
        independent = child_result.get("independent_review", {})
        if independent.get("execution_status") != "passed":
            raise ValidationFailure("smoke did not execute independent review successfully")
        isolation = observe_isolation(run_root, candidate, preflight, audit)
        if time.monotonic() >= deadline:
            raise WorkflowFailure("smoke exceeded its absolute wall-clock deadline")
        audit.record(
            phase="smoke",
            action="seal_nonpublishing_diagnostic",
            what="All required smoke components completed; sealing the diagnostic candidate.",
            why="A smoke is evidence only when its exact candidate and review trail are retained.",
            how="Applied model-aware validation without promoting the diagnostic wiki.",
            details={"metrics": metrics, "isolation": isolation},
        )
        seal_wiki(
            wiki_dir=candidate,
            profile=profile,
            lock=lock,
            audit=audit,
            run_id=run_id,
            source_manifest=staged["manifest"],
            model_evidence={
                "mode": "smoke",
                "binding_sha256": binding["binding_sha256"],
                "route": binding["route"],
                "metrics": metrics,
                "independent_review": independent,
                "release_review": child_result.get("release_review", {}),
                "smoke_report_sha256": None,
            },
        )
        verify_checksums(candidate)
        created = datetime.now().astimezone()
        report = {
            "status": "passed",
            "corrected": False,
            "run_id": run_id,
            "created_at": created.isoformat(timespec="seconds"),
            "expires_at": (
                created + timedelta(hours=runtime.limits.smoke_fresh_hours)
            ).isoformat(timespec="seconds"),
            "binding_sha256": binding["binding_sha256"],
            "binding": binding,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "components": {
                "preflight": "passed",
                "canonical_scope_staging": "passed",
                "staged_generation": "passed",
                "deterministic_validation": "passed",
                "independent_review": independent.get("finding_status"),
                "parent_and_child_watchdogs": "armed",
                "observability": preflight.get("components", {}).get(
                    "observability", {"status": "not-reported"}
                ),
            },
            "metrics": metrics,
            "diagnostic_wiki": "work/diagnostic-wiki",
            "creates_published_wiki": False,
            "creates_comparison": False,
            "gold_or_prior_wiki_read": False,
        }
        report_path = run_root / "smoke-report.json"
        exclusive_json(report_path, report)
        digest = sha256_file(report_path)
        exclusive_text(
            report_path.with_suffix(".sha256"), f"{digest}  {report_path.name}\n"
        )
        audit.record(
            phase="smoke",
            action="pass_production_shaped_smoke",
            what="Passed the bounded nonpublishing smoke for this exact binding.",
            why="Production requires fresh proof that every guarded component executes.",
            how="Retained a checksummed report and sealed diagnostic without publication.",
            details={"smoke_report_sha256": digest},
        )
        _write_progress(run_root, run_id, "passed", phase="smoke-complete")
        exclusive_json(
            run_root / "terminal.json",
            {
                "run_id": run_id,
                "kind": "smoke",
                "state": "passed",
                "category": "success",
                "exit_code": 0,
                "smoke_report_sha256": digest,
            },
        )
        _write_review_index(run_root)
        return report_path
    except BaseException as error:
        _record_failure(
            run_id=run_id,
            run_root=run_root,
            audit=audit,
            binding=binding,
            error=error,
            started=started,
        )
        raise


def run_build(
    *,
    profile_path: Path,
    lock_path: Path,
    smoke_report_path: Path,
    run_dir: Path,
    output: Path,
    runtime: RuntimeConfig | None = None,
    services: RuntimeServices | None = None,
) -> Path:
    try:
        runtime = runtime or load_runtime_config(require_network=True)
    except ConfigError as error:
        raise ConfigurationFailure(str(error)) from error
    state = _start_run(
        kind="build",
        profile_path=profile_path,
        lock_path=lock_path,
        run_dir=run_dir,
        runtime=runtime,
    )
    (
        run_id,
        run_root,
        audit,
        recorder,
        profile,
        lock,
        binding,
        started,
        deadline,
    ) = state
    output = Path(output).expanduser().resolve()
    candidate = output.parent / f".{output.name}.{run_id}.candidate"
    try:
        if output.exists():
            raise ConfigurationFailure(f"output directory already exists: {output}")
        if candidate.exists():
            raise ConfigurationFailure(f"candidate directory already exists: {candidate}")
        smoke_report, smoke_digest = _smoke_report(smoke_report_path, binding)
        audit.record(
            phase="build",
            action="accept_exact_fresh_smoke_report",
            what="Accepted the passed, uncorrected smoke report for this exact binding.",
            why="Production cannot inherit proof from different code, source, route, or limits.",
            how="Verified checksum, terminal record, expiry, pass state, and binding hash.",
            details={
                "smoke_run_id": smoke_report["run_id"],
                "smoke_report_sha256": smoke_digest,
            },
        )
        preflight = _perform_preflight(
            runtime=runtime,
            run_root=run_root,
            audit=audit,
            recorder=recorder,
            services=services,
        )
        staged = source.stage_source(
            profile=profile,
            lock=lock,
            work_root=run_root / "work" / "source-stage",
            audit=audit,
        )
        staged["extraction_root"] = profile.extraction_root
        prepare_wiki(
            wiki_dir=candidate,
            pages=staged["pages"],
            profile=staged["runtime"],
            audit=audit,
            run_id=run_id,
            extraction_root=profile.extraction_root,
        )
        child_result = _compile(
            services=services,
            run_root=run_root,
            run_id=run_id,
            profile_path=profile.path,
            lock_path=Path(lock_path).resolve(),
            staged=staged,
            candidate=candidate,
            mode="build",
            runtime=runtime,
            deadline=deadline,
            audit=audit,
            recorder=recorder,
        )
        if services is None:
            _merge_child_audit(run_root, audit)
        provider_kind = services.provider_kind if services else "network"
        metrics = _model_metrics(run_root, child_result, provider_kind)
        independent = child_result.get("independent_review", {})
        isolation = observe_isolation(run_root, candidate, preflight, audit)
        if time.monotonic() >= deadline:
            raise WorkflowFailure("build exceeded its absolute wall-clock deadline")
        audit.record(
            phase="build",
            action="authorize_atomic_publication",
            what=f"Authorized atomic publication to the explicit output name {output.name!r}.",
            why="Only a fresh reviewed candidate with a matching smoke may be published.",
            how="Recorded all gates before sealing and the same-filesystem atomic rename.",
            details={"output_name": output.name, "isolation": isolation},
        )
        try:
            seal_wiki(
                wiki_dir=candidate,
                profile=profile,
                lock=lock,
                audit=audit,
                run_id=run_id,
                source_manifest=staged["manifest"],
                model_evidence={
                    "mode": "build",
                    "binding_sha256": binding["binding_sha256"],
                    "route": binding["route"],
                    "metrics": metrics,
                    "independent_review": independent,
                    "release_review": child_result.get("release_review", {}),
                    "smoke_report_sha256": smoke_digest,
                },
            )
        except ValueError as error:
            raise ValidationFailure(str(error)) from error
        if sha256_file(profile.pdf_path) != lock["source"]["pdf_sha256"]:
            raise ValidationFailure("source PDF changed during the build")
        published = publish_candidate(candidate, output)
        verify_checksums(published)
        result = {
            "status": "passed",
            "run_id": run_id,
            "wiki": str(published),
            "smoke_report_sha256": smoke_digest,
            "binding_sha256": binding["binding_sha256"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "metrics": metrics,
        }
        exclusive_json(run_root / "run-result.json", result)
        audit.record(
            phase="run",
            action="complete_atomic_publication",
            what=f"Published and reverified the explicit output {output.name!r}.",
            why="The requested build passed every binding, source, review, and integrity gate.",
            how="Atomically renamed the sealed candidate and reverified full-tree checksums.",
            details={"output_name": output.name},
        )
        _write_progress(run_root, run_id, "passed", phase="published", output=str(output))
        exclusive_json(
            run_root / "terminal.json",
            {
                "run_id": run_id,
                "kind": "build",
                "state": "passed",
                "category": "success",
                "exit_code": 0,
                "result_sha256": sha256_file(run_root / "run-result.json"),
            },
        )
        _write_review_index(run_root)
        return published
    except BaseException as error:
        _record_failure(
            run_id=run_id,
            run_root=run_root,
            audit=audit,
            binding=binding,
            error=error,
            started=started,
        )
        raise


def inspect_run(run_dir: Path) -> dict[str, Any]:
    """Read and verify one run's terminal state and durable artifact index."""
    root = Path(run_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    values: dict[str, Any] = {"run_dir": str(root)}
    for name in ("run.json", "progress.json", "terminal.json", "failure.json", "run-result.json"):
        path = root / name
        if path.is_file():
            values[name.removesuffix(".json").replace("-", "_")] = json.loads(
                path.read_text(encoding="utf-8")
            )
    index = root / "review" / "index.json"
    sidecar = root / "review" / "index.sha256"
    if index.is_file() and sidecar.is_file():
        expected = sidecar.read_text(encoding="utf-8").partition("  ")[0].strip()
        if expected != sha256_file(index):
            raise ValueError("run review index checksum mismatch")
        report = json.loads(index.read_text(encoding="utf-8"))
        changed: list[str] = []
        missing: list[str] = []
        for record in report.get("artifacts", []):
            path = root / PurePosixPath(record["path"])
            if not path.is_file():
                missing.append(record["path"])
            elif sha256_file(path) != record["sha256"]:
                changed.append(record["path"])
        values["review_index"] = {
            "status": "passed" if not missing and not changed else "failed",
            "artifact_count": len(report.get("artifacts", [])),
            "missing": missing,
            "changed": changed,
        }
    else:
        values["review_index"] = {"status": "not-finalized"}
    return values


def exit_code(error: BaseException) -> int:
    if isinstance(error, RunFailure):
        return error.exit_code
    if isinstance(error, (ConfigError, OSError)):
        return 2
    if isinstance(error, ValueError):
        return 5
    return 4
