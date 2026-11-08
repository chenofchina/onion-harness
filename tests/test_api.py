"""API layer tests - service composition, event mirroring, HTTP and SSE.

A fake provider is installed so no network call is ever made.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from onion_harness.api import OnionService
from onion_harness.api.server import OnionHTTPServer
from onion_harness.harness import runtime as runtime_module
from onion_harness.harness.bridge import EngineChatResponse, EngineToolCall, EngineUsage


class FakeProvider:
    def __init__(self, script=None):
        self.model = "fake-model"
        self.last_reasoning = ""
        self._script = script or [{"text": "done"}]
        self._calls = 0

    def get_model_name(self):
        return self.model

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        self._calls += 1
        step = self._script[min(self._calls - 1, len(self._script) - 1)]
        self.last_reasoning = step.get("reasoning", "")
        return EngineChatResponse(
            text=step.get("text", ""),
            tool_calls=step.get("tool_calls", []),
            usage=EngineUsage(prompt_tokens=11, completion_tokens=4, total_tokens=15),
            model=self.model,
        )


class ServiceTestBase(unittest.TestCase):
    script = [{"text": "all good"}]

    def setUp(self):
        self._original = runtime_module.build_provider
        runtime_module.build_provider = lambda *a, **k: FakeProvider(self.script)
        self.dir = Path(tempfile.mkdtemp(prefix="onion-api-"))
        self.service = OnionService(data_dir=self.dir)
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.start()
        self.server = OnionHTTPServer(self.service, static_dir=None)
        self.base = self.server.start()

    def tearDown(self):
        self.server.stop()
        self.service.stop()
        runtime_module.build_provider = self._original

    def wait_for_done(self, task_id, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.service.task_events(task_id)
            if any(event["type"] == "task-done" for event in events):
                return events
            time.sleep(0.05)
        raise AssertionError(f"task {task_id} never finished")

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response)

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def delete(self, path):
        request = urllib.request.Request(self.base + path, method="DELETE")
        with urllib.request.urlopen(request) as response:
            return json.load(response)


class BootstrapTest(ServiceTestBase):
    def test_bootstrap_payload(self):
        data = self.get("/api/bootstrap")
        self.assertEqual(len(data["providers"]), 11)
        self.assertEqual({e["name"] for e in data["executors"]}, {"python", "powershell", "cmd"})
        self.assertTrue(data["panels"])
        self.assertEqual(data["prompt_segments"][0]["id"], "core")
        self.assertTrue(data["prompt_segments"][0]["enabled"])
        self.assertEqual(data["history"], [])
        self.assertIn("data_dir", data)

    def test_bootstrap_never_includes_api_keys(self):
        blob = json.dumps(self.get("/api/bootstrap"))
        self.assertNotIn("sk-test", blob)
        self.assertIn("configured_providers", blob)

    def test_settings_roundtrip(self):
        updated = self.post("/api/settings", {"max_turns": 12, "thinking": "high"})
        self.assertEqual(updated["max_turns"], 12)
        self.assertEqual(updated["thinking"], "high")
        self.assertEqual(self.get("/api/settings")["max_turns"], 12)

    def test_api_key_save_and_clear(self):
        self.post("/api/settings/api-key", {"provider_id": "openai", "api_key": "sk-openai"})
        settings = self.get("/api/settings")
        self.assertIn("openai", settings["configured_providers"])
        self.delete("/api/settings/api-key?provider_id=openai")
        self.assertNotIn("openai", self.get("/api/settings")["configured_providers"])


class TaskApiTest(ServiceTestBase):
    script = [
        {"tool_calls": [EngineToolCall(id="c1", name="run_python", arguments={"code": "print('hi')"})]},
        {"text": "finished"},
    ]

    def test_create_task_and_stream_events(self):
        record = self.post("/api/tasks", {"prompt": "run a thing"})
        self.assertEqual(record["status"], "queued")
        events = self.wait_for_done(record["id"])
        types = [event["type"] for event in events]
        self.assertIn("tool-call", types)
        self.assertIn("tool-result", types)
        self.assertIn("assistant", types)
        self.assertEqual(types[-1], "task-done")

        tasks = self.get("/api/tasks")["tasks"]
        self.assertEqual(tasks[0]["status"], "completed")

    def test_events_are_persisted_to_disk(self):
        record = self.post("/api/tasks", {"prompt": "persist me"})
        self.wait_for_done(record["id"])
        path = self.dir / "tasks" / f"{record['id']}.jsonl"
        self.assertTrue(path.exists())
        # Transcripts are sealed line by line, so the file is read back through
        # the store rather than parsed directly.
        lines = self.service.store.load_events(record["id"])
        self.assertTrue(any(event["type"] == "task-done" for event in lines))
        # And the raw bytes must not contain the prompt.
        self.assertNotIn(b"persist me", path.read_bytes())

    def test_missing_prompt_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/tasks", {"prompt": "   "})
        self.assertEqual(ctx.exception.code, 400)

    def test_missing_api_key_is_401(self):
        self.delete("/api/settings/api-key?provider_id=deepseek")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/tasks", {"prompt": "no key"})
        self.assertEqual(ctx.exception.code, 401)
        payload = json.loads(ctx.exception.read().decode())
        self.assertIn("API key", payload["error"])

    def test_nonexistent_workspace_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/tasks", {"prompt": "x", "workspace": "C:/definitely/not/here"})
        self.assertEqual(ctx.exception.code, 400)

    def test_delete_task(self):
        record = self.post("/api/tasks", {"prompt": "temp"})
        self.wait_for_done(record["id"])
        self.delete(f"/api/tasks/{record['id']}")
        self.assertEqual(self.get("/api/tasks")["tasks"], [])

    def test_unknown_endpoint_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/does-not-exist")
        self.assertEqual(ctx.exception.code, 404)


class ChangeApiTest(ServiceTestBase):
    def test_propose_apply_revert_redo_via_http(self):
        proposed = self.post(
            "/api/changes",
            {
                "kind": "update_system_prompt",
                "target": "core",
                "reason": "test",
                "params": {"segment_id": "core", "content": "http-applied"},
            },
        )
        change_id = proposed["change_id"]
        self.assertEqual(len(self.get("/api/history")["proposals"]), 1)

        revision = self.post(f"/api/changes/{change_id}/apply", {})
        self.assertEqual(revision["version"], 1)

        history = self.get("/api/history")["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["change"]["status"], "applied")

        reverted = self.post("/api/history/1/revert", {})
        self.assertEqual(len(reverted["reverted"]), 1)

        redone = self.post("/api/history/redo", {})
        self.assertEqual(len(redone["redone"]), 1)

    def test_reject_change(self):
        proposed = self.post(
            "/api/changes",
            {"kind": "register_executor", "target": "node", "params": {"name": "node"}},
        )
        self.post(f"/api/changes/{proposed['change_id']}/reject", {"reason": "no"})
        self.assertEqual(self.get("/api/history")["proposals"], [])

    def test_apply_unknown_change_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/changes/nope/apply", {})
        self.assertEqual(ctx.exception.code, 400)

    def test_irreversible_change_needs_acknowledgement(self):
        proposed = self.post(
            "/api/changes",
            {
                "kind": "update_skill_registry",
                "target": "pdf",
                "params": {"action": "mount", "name": "pdf", "path": "skills/pdf"},
            },
        )
        change_id = proposed["change_id"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(f"/api/changes/{change_id}/apply", {})
        self.assertEqual(ctx.exception.code, 400)
        ok = self.post(f"/api/changes/{change_id}/apply", {"accept_irreversible": True})
        self.assertEqual(ok["version"], 1)

    def test_ledger_events_mirror_into_stream(self):
        proposed = self.post(
            "/api/changes",
            {"kind": "register_executor", "target": "node", "params": {"name": "node"}},
        )
        self.post(f"/api/changes/{proposed['change_id']}/apply", {})
        system_events = [
            event for event in self.service.bus.replay_for("system")
        ]
        types = [event["type"] for event in system_events]
        self.assertIn("change-proposal", types)
        self.assertIn("change-applied", types)


class MetaToolFlowTest(ServiceTestBase):
    """The meta tools a model can reach must only ever produce proposals."""

    script = [
        {"tool_calls": [EngineToolCall(
            id="m1",
            name="onion_update_system_prompt",
            arguments={"segment_id": "core", "content": "from the model", "reason": "improve"},
        )]},
        {"text": "proposed"},
    ]

    def test_model_initiated_change_is_only_proposed(self):
        record = self.post("/api/tasks", {"prompt": "improve yourself"})
        events = self.wait_for_done(record["id"])
        types = [event["type"] for event in events]
        self.assertIn("change-proposal", types)
        self.assertNotIn("change-applied", types)

        proposals = self.get("/api/history")["proposals"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["target"], "core")

        # Live state untouched until the user confirms.
        bootstrap = self.get("/api/bootstrap")
        self.assertFalse(
            any("from the model" in (segment.get("title", "") + str(segment.get("chars")))
                for segment in bootstrap["prompt_segments"])
        )


class StaticServingTest(unittest.TestCase):
    def test_missing_bundle_returns_actionable_hint(self):
        directory = Path(tempfile.mkdtemp(prefix="onion-static-"))
        service = OnionService(data_dir=directory)
        service.start()
        empty = directory / "no-static"
        empty.mkdir()
        server = OnionHTTPServer(service, static_dir=empty)
        base = server.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/")
            self.assertEqual(ctx.exception.code, 503)
            payload = json.loads(ctx.exception.read().decode())
            self.assertIn("npm run build", payload["hint"])
        finally:
            server.stop()
            service.stop()


if __name__ == "__main__":
    unittest.main()