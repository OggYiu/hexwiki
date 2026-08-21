"""Public-safe runtime configuration and authoritative local model transcripts.

Document facts live in a profile.  Provider settings are loaded for each command
from explicit ``HEXWIKI_*`` environment values and, optionally, one private
``config.json`` below ``HEXWIKI_CONFIG_DIR``.  This module never loads ``.env``
from the current directory and never persists an API key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

try:
    from langchain_core.callbacks.base import BaseCallbackHandler
except ImportError:
    # Offline/base installs do not include the optional model runtime. The
    # callback is instantiated only after that runtime has been required.
    class BaseCallbackHandler:  # type: ignore[no-redef]
        pass


CONFIG_FILENAME = "config.json"
SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "secret_key",
    "token",
}


class ConfigError(ValueError):
    """Runtime configuration is missing, inconsistent, or unsafe."""


@dataclass(frozen=True)
class RuntimeLimits:
    smoke_max_seconds: int = 5_400
    build_max_seconds: int = 18_000
    child_margin_seconds: int = 60
    heartbeat_seconds: int = 15
    smoke_fresh_hours: int = 24
    smoke_recursion_limit: int = 150
    build_recursion_limit: int = 400
    stage_attempts: int = 3
    stage_retry_seconds: tuple[int, ...] = (30, 30)
    review_attempts: int = 5
    review_retry_seconds: tuple[int, ...] = (30, 90, 180, 300)


@dataclass(frozen=True)
class RuntimeConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    config_dir: Path
    runs_dir: Path
    observability_enabled: bool
    limits: RuntimeLimits

    def binding(self) -> dict[str, Any]:
        route = f"{self.base_url}\0{self.model}".encode("utf-8")
        return {
            "model": self.model,
            "route_id": hashlib.sha256(route).hexdigest(),
            "base_url_sha256": hashlib.sha256(self.base_url.encode("utf-8")).hexdigest(),
            "observability_enabled": self.observability_enabled,
            "limits": asdict(self.limits),
        }


def default_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    if values.get("HEXWIKI_CONFIG_DIR", "").strip():
        return Path(os.path.expandvars(values["HEXWIKI_CONFIG_DIR"])).expanduser().resolve()
    if os.name == "nt" and values.get("LOCALAPPDATA", "").strip():
        return (Path(values["LOCALAPPDATA"]) / "HexWiki").resolve()
    if values.get("XDG_CONFIG_HOME", "").strip():
        return (Path(values["XDG_CONFIG_HOME"]) / "hexwiki").expanduser().resolve()
    return (Path.home() / ".config" / "hexwiki").resolve()


def _duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate config key: {key!r}")
        result[key] = value
    return result


def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"), object_pairs_hook=_duplicates
        )
    except json.JSONDecodeError as error:
        raise ConfigError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain one JSON object")
    allowed = {"base_url", "model", "runs_dir", "observability", "limits"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"unknown runtime config fields: {', '.join(unknown)}")
    return value


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location} must be a non-empty string")
    if "\x00" in value:
        raise ConfigError(f"{location} may not contain a NUL character")
    return value.strip()


def _url(value: Any) -> str:
    candidate = _text(value, "base_url").rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("HEXWIKI_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ConfigError("HEXWIKI_BASE_URL may not contain embedded credentials")
    return candidate


def _positive_int(value: Any, location: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{location} must be an integer >= {minimum}")
    return value


def _integer_tuple(value: Any, location: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value
    ):
        raise ConfigError(f"{location} must be a list of non-negative integers")
    return tuple(value)


def _limits(value: Any) -> RuntimeLimits:
    if value is None:
        return RuntimeLimits()
    if not isinstance(value, dict):
        raise ConfigError("limits must be an object")
    allowed = set(RuntimeLimits.__dataclass_fields__)
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"unknown runtime limit fields: {', '.join(unknown)}")
    defaults = asdict(RuntimeLimits())
    defaults.update(value)
    for key in (
        "smoke_max_seconds",
        "build_max_seconds",
        "child_margin_seconds",
        "heartbeat_seconds",
        "smoke_fresh_hours",
        "smoke_recursion_limit",
        "build_recursion_limit",
        "stage_attempts",
        "review_attempts",
    ):
        defaults[key] = _positive_int(defaults[key], f"limits.{key}")
    defaults["stage_retry_seconds"] = _integer_tuple(
        list(defaults["stage_retry_seconds"]), "limits.stage_retry_seconds"
    )
    defaults["review_retry_seconds"] = _integer_tuple(
        list(defaults["review_retry_seconds"]), "limits.review_retry_seconds"
    )
    if len(defaults["stage_retry_seconds"]) < defaults["stage_attempts"] - 1:
        raise ConfigError("stage_retry_seconds needs one delay per retry")
    if len(defaults["review_retry_seconds"]) < defaults["review_attempts"] - 1:
        raise ConfigError("review_retry_seconds needs one delay per retry")
    return RuntimeLimits(**defaults)


def _environment_value(name: str, environ: Mapping[str, str]) -> str | None:
    value = environ.get(name)
    if value:
        return value
    if environ is not os.environ or os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            stored, _ = winreg.QueryValueEx(key, name)
            return str(stored) if stored else None
    except (FileNotFoundError, OSError):
        return None


def load_runtime_config(
    *,
    environ: Mapping[str, str] | None = None,
    config_dir: Path | None = None,
    runs_dir: Path | None = None,
    require_network: bool = True,
) -> RuntimeConfig:
    """Load one command's runtime state without import-time global caching."""
    values = os.environ if environ is None else environ
    resolved_config = (config_dir or default_config_dir(values)).expanduser().resolve()
    stored = _read_config(resolved_config / CONFIG_FILENAME)

    configured_runs = runs_dir
    if configured_runs is None:
        raw_runs = values.get("HEXWIKI_RUNS_DIR", "").strip() or stored.get("runs_dir")
        configured_runs = (
            Path(os.path.expandvars(str(raw_runs))).expanduser()
            if raw_runs
            else resolved_config / "runs"
        )
    if not configured_runs.is_absolute():
        configured_runs = resolved_config / configured_runs
    resolved_runs = configured_runs.resolve()

    base_raw = values.get("HEXWIKI_BASE_URL", "").strip() or stored.get("base_url", "")
    model_raw = values.get("HEXWIKI_MODEL", "").strip() or stored.get("model", "")
    key_raw = _environment_value("HEXWIKI_API_KEY", values) or ""
    if require_network:
        missing = [
            name
            for name, raw in (
                ("HEXWIKI_BASE_URL", base_raw),
                ("HEXWIKI_MODEL", model_raw),
                ("HEXWIKI_API_KEY", key_raw),
            )
            if not str(raw).strip()
        ]
        if missing:
            raise ConfigError("missing required runtime settings: " + ", ".join(missing))
    base_url = _url(base_raw) if str(base_raw).strip() else "http://offline.invalid"
    model = _text(model_raw, "model") if str(model_raw).strip() else "offline"
    api_key = _text(key_raw, "api_key") if str(key_raw).strip() else "offline"

    observability = stored.get("observability", {})
    if not isinstance(observability, dict):
        raise ConfigError("observability must be an object")
    unknown_observability = sorted(set(observability) - {"enabled"})
    if unknown_observability:
        raise ConfigError(
            "unknown observability fields: " + ", ".join(unknown_observability)
        )
    enabled = observability.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("observability.enabled must be true or false")

    return RuntimeConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        config_dir=resolved_config,
        runs_dir=resolved_runs,
        observability_enabled=enabled,
        limits=_limits(stored.get("limits")),
    )


def config_template() -> dict[str, Any]:
    """A secret-free private configuration template used by ``hexwiki init``."""
    return {
        "base_url": "https://provider.example/v1",
        "model": "replace-with-an-exact-model-id",
        "runs_dir": "runs",
        "observability": {"enabled": False},
        "limits": asdict(RuntimeLimits()),
    }


def _redacted_text(value: str, secrets: tuple[str, ...]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result


def _jsonable(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        return _redacted_text(value, secrets)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if str(key).casefold() in SENSITIVE_KEYS
            else _jsonable(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, secrets) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump(mode="json"), secrets)
        except Exception:
            pass
    return _redacted_text(repr(value), secrets)


class TranscriptRecorder:
    """Append-only, credential-redacted local records for model/tool boundaries."""

    def __init__(
        self,
        root: Path,
        run_id: str,
        *,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.root = Path(root).resolve()
        self.run_id = run_id
        self._secrets = tuple(secret for secret in secrets if secret)
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, stream: str, value: dict[str, Any]) -> None:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", stream).strip("-") or "events"
        record = {
            "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            **_jsonable(value, self._secrets),
        }
        path = self.root / f"{safe}.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())


class LocalPayloadCallback(BaseCallbackHandler):
    """LangChain-compatible callback that persists complete local payloads."""

    raise_error = False
    run_inline = True

    def __init__(self, recorder: TranscriptRecorder) -> None:
        super().__init__()
        self.recorder = recorder

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self.recorder.append(
            "model-events",
            {
                "event": "model-request",
                "model_run_id": str(run_id),
                "parent_run_id": str(parent_run_id) if parent_run_id else None,
                "serialized": serialized,
                "messages": messages,
                "invocation": kwargs,
            },
        )

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **_: Any,
    ) -> None:
        self.recorder.append(
            "model-events",
            {
                "event": "model-response",
                "model_run_id": str(run_id),
                "parent_run_id": str(parent_run_id) if parent_run_id else None,
                "response": response,
            },
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **_: Any,
    ) -> None:
        self.recorder.append(
            "model-events",
            {
                "event": "model-error",
                "model_run_id": str(run_id),
                "parent_run_id": str(parent_run_id) if parent_run_id else None,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self.recorder.append(
            "tool-events",
            {
                "event": "tool-request",
                "tool_run_id": str(run_id),
                "parent_run_id": str(parent_run_id) if parent_run_id else None,
                "serialized": serialized,
                "input": input_str,
                "metadata": kwargs,
            },
        )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **_: Any,
    ) -> None:
        self.recorder.append(
            "tool-events",
            {
                "event": "tool-response",
                "tool_run_id": str(run_id),
                "parent_run_id": str(parent_run_id) if parent_run_id else None,
                "output": output,
            },
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **_: Any,
    ) -> None:
        self.recorder.append(
            "tool-events",
            {
                "event": "tool-error",
                "tool_run_id": str(run_id),
                "parent_run_id": str(parent_run_id) if parent_run_id else None,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )


class NonBlockingCallback:
    """Contain every optional observability callback failure."""

    raise_error = False
    run_inline = False

    def __init__(self, delegate: Any, recorder: TranscriptRecorder) -> None:
        self.delegate = delegate
        self.recorder = recorder

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.delegate, name)
        if not callable(target):
            return target

        def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return target(*args, **kwargs)
            except BaseException as error:
                self.recorder.append(
                    "observability-events",
                    {
                        "event": "callback-failed",
                        "callback": name,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    },
                )
                return None

        return guarded


def langchain_callbacks(
    runtime: RuntimeConfig,
    recorder: TranscriptRecorder,
) -> list[Any]:
    callbacks: list[Any] = [LocalPayloadCallback(recorder)]
    if not runtime.observability_enabled:
        return callbacks
    try:
        from langfuse.langchain import CallbackHandler

        callbacks.append(NonBlockingCallback(CallbackHandler(), recorder))
    except BaseException as error:
        recorder.append(
            "observability-events",
            {
                "event": "initialization-failed",
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
    return callbacks


def flush_observability(runtime: RuntimeConfig, recorder: TranscriptRecorder) -> None:
    if not runtime.observability_enabled:
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except BaseException as error:
        recorder.append(
            "observability-events",
            {
                "event": "flush-failed",
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
