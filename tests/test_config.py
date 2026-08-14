"""Runtime configuration, redaction, and optional-observability regressions."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hexwiki.engine.config import (
    ConfigError,
    NonBlockingCallback,
    RuntimeConfig,
    RuntimeLimits,
    TranscriptRecorder,
    flush_observability,
    load_runtime_config,
)
from hexwiki.engine.audit import AuditLog
from hexwiki.engine.runtime import PreflightFailure, network_preflight


def _runtime(root: Path, *, observability: bool = False) -> RuntimeConfig:
    return RuntimeConfig(
        base_url="https://private-route.invalid/v1",
        model="synthetic-model",
        api_key="synthetic-secret-value",
        config_dir=root / "config",
        runs_dir=root / "runs",
        observability_enabled=observability,
        limits=RuntimeLimits(),
    )


class ConfigTests(unittest.TestCase):
    def test_working_directory_dotenv_is_never_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text(
                "HEXWIKI_API_KEY=must-not-be-loaded\n", encoding="utf-8"
            )
            previous = Path.cwd()
            os.chdir(root)
            try:
                runtime = load_runtime_config(
                    environ={"HEXWIKI_CONFIG_DIR": str(root / "config")},
                    require_network=False,
                )
            finally:
                os.chdir(previous)
            self.assertEqual(runtime.api_key, "offline")
            with self.assertRaisesRegex(ConfigError, "HEXWIKI_API_KEY"):
                load_runtime_config(
                    environ={"HEXWIKI_CONFIG_DIR": str(root / "config")},
                    require_network=True,
                )

    def test_config_rejects_secrets_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            config.mkdir()
            (config / "config.json").write_text(
                json.dumps({"api_key": "must-stay-in-the-environment"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "unknown runtime config fields"):
                load_runtime_config(
                    environ={"HEXWIKI_CONFIG_DIR": str(config)},
                    require_network=False,
                )

    def test_each_load_is_independent_and_has_no_import_time_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = []
            for index in (1, 2):
                config = root / f"config-{index}"
                config.mkdir()
                (config / "config.json").write_text(
                    json.dumps(
                        {
                            "base_url": f"https://route-{index}.invalid/v1",
                            "model": f"model-{index}",
                            "runs_dir": f"runs-{index}",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                values.append(
                    load_runtime_config(
                        environ={
                            "HEXWIKI_CONFIG_DIR": str(config),
                            "HEXWIKI_API_KEY": f"key-{index}",
                        }
                    )
                )
            self.assertEqual([item.model for item in values], ["model-1", "model-2"])
            self.assertNotEqual(values[0].config_dir, values[1].config_dir)
            self.assertNotEqual(values[0].binding()["route_id"], values[1].binding()["route_id"])

    def test_binding_and_transcripts_never_persist_the_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = _runtime(root)
            binding = json.dumps(runtime.binding(), sort_keys=True)
            self.assertNotIn(runtime.api_key, binding)
            self.assertNotIn(runtime.base_url, binding)

            recorder = TranscriptRecorder(
                root / "transcripts",
                "redaction-test",
                secrets=(runtime.api_key,),
            )
            recorder.append(
                "model-events",
                {
                    "api_key": runtime.api_key,
                    "nested": {"token": runtime.api_key},
                    "error": f"provider echoed {runtime.api_key}",
                },
            )
            text = (root / "transcripts" / "model-events.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(runtime.api_key, text)
            self.assertGreaterEqual(text.count("[REDACTED]"), 3)

    def test_observability_callback_and_flush_failures_are_contained(self) -> None:
        class FailingDelegate:
            def on_event(self, *_: object, **__: object) -> None:
                raise RuntimeError("synthetic observability outage")

        class FailingClient:
            def flush(self) -> None:
                raise RuntimeError("synthetic flush outage")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = TranscriptRecorder(root / "transcripts", "observability-test")
            callback = NonBlockingCallback(FailingDelegate(), recorder)
            self.assertIsNone(callback.on_event("payload"))

            runtime = _runtime(root, observability=True)
            fake_langfuse = types.ModuleType("langfuse")
            fake_langfuse.get_client = lambda: FailingClient()  # type: ignore[attr-defined]
            with patch.dict(sys.modules, {"langfuse": fake_langfuse}):
                flush_observability(runtime, recorder)
            records = (
                root / "transcripts" / "observability-events.jsonl"
            ).read_text(encoding="utf-8")
            self.assertIn("callback-failed", records)
            self.assertIn("flush-failed", records)

    def test_provider_preflight_failure_retains_each_component_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "preflight-run"
            runtime = _runtime(root)
            recorder = TranscriptRecorder(run_root / "transcripts", "preflight-test")
            audit = AuditLog(run_root / "actions.jsonl", "preflight-test")
            with (
                patch.dict(sys.modules, {"openai": None}),
                self.assertRaisesRegex(PreflightFailure, "model runtime is unavailable"),
            ):
                network_preflight(
                    runtime=runtime,
                    run_root=run_root,
                    audit=audit,
                    recorder=recorder,
                )
            report = json.loads(
                (run_root / "preflight" / "components.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["components"]["filesystem_sandbox"]["status"], "passed")
            self.assertEqual(report["components"]["model_runtime"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
