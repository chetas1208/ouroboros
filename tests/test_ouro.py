# Ouroboros - Copyright (c) 2026 Chetas P. PolyForm Noncommercial 1.0.0.
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skill" / "scripts" / "ouro.py"
TMP_HOME = tempfile.mkdtemp(prefix="ouro-home-")
os.environ["OURO_HOME"] = TMP_HOME  # no laya-guard here: exercises the denylist-only path
os.environ.pop("OURO_LAYA_GUARD", None)
os.environ["OURO_OLLAMA_URL"] = "http://127.0.0.1:9"  # unreachable: planner must fall back, never hang

spec = importlib.util.spec_from_file_location("ouro", SCRIPT)
ouro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ouro)


class Cli:
    """Runs the real CLI against a throwaway workspace."""

    def __init__(self, root: str):
        self.root = root

    def __call__(self, *args, stdin: str = "", ok=(0,)):
        p = subprocess.run([sys.executable, str(SCRIPT), "--root", self.root, *args], input=stdin, capture_output=True, text=True, env=os.environ, timeout=60)
        assert p.returncode in ok, f"{args} -> {p.returncode}\n{p.stdout}\n{p.stderr}"
        return p.stdout

    def state(self):
        return json.loads(Path(self.root, ".ouro", "state.json").read_text())


class PureFunctions(unittest.TestCase):
    def test_is_task(self):
        self.assertFalse(ouro.is_task("fix it"))
        self.assertFalse(ouro.is_task("what does this function do in the codebase right now?"))
        self.assertTrue(ouro.is_task("add pagination to the users endpoint and cover it with tests please"))

    def test_denylist(self):
        for bad in ["git push", "git commit -m x", "curl http://x", "rm -rf /tmp/x", "sudo ls", "npm publish", "kubectl apply -f x", "./deploy.sh"]:
            self.assertFalse(ouro.safe_cmd(bad)[0], bad)
        for good in ["pytest -q", "npm test", "grep -q foo file.txt", "test -s out.mp4"]:
            self.assertTrue(ouro.safe_cmd(good)[0], good)

    def test_build_nodes_fallback_and_gates(self):
        nodes, acceptance, source = ouro.build_nodes(None)
        self.assertEqual(source, "fallback-template")
        self.assertEqual([n["id"] for n in nodes[-2:]], ["verify", "review"])
        self.assertTrue(acceptance)

    def test_build_nodes_filters_forbidden_and_keeps_dag_acyclic(self):
        plan = {"acceptance": ["a"], "nodes": [
            {"id": "a", "title": "Write the parser", "deps": [], "verify": "pytest -q"},
            {"id": "b", "title": "Deploy to production", "deps": ["a"], "verify": ""},
            {"id": "c", "title": "Add tests", "deps": ["a", "later"], "verify": ""},
            {"id": "later", "title": "Docs", "deps": ["c"], "verify": ""},
        ]}
        nodes, _, source = ouro.build_nodes(plan)
        ids = [n["id"] for n in nodes]
        self.assertEqual(source, "local-model")
        self.assertNotIn("b", ids)  # policy filter drops deploy nodes
        seen = set()
        for n in nodes:
            self.assertTrue(set(n["deps"]) <= seen, n)  # deps only reference earlier nodes
            seen.add(n["id"])
        self.assertTrue(all(n["verify"] is None for n in nodes if n["kind"] == "work"))  # model verify is only a hint
        self.assertEqual(nodes[0]["hint"], "pytest -q")

    def test_ready_nodes_respects_dependencies(self):
        st = {"nodes": [
            {"id": "a", "deps": [], "status": "pending"},
            {"id": "b", "deps": ["a"], "status": "pending"},
        ]}
        self.assertEqual([n["id"] for n in ouro.ready_nodes(st)], ["a"])
        st["nodes"][0]["status"] = "done"
        self.assertEqual([n["id"] for n in ouro.ready_nodes(st)], ["b"])


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ouro-ws-")
        self.ouro = Cli(self.d)
        self.ouro("start", "--no-model", "--quiet", "implement the thing and prove that it works end to end")
        self.hook = json.dumps({"workspace_roots": [self.d]})

    def finish_all(self):
        for n in self.ouro.state()["nodes"]:
            self.ouro("done", n["id"], "--evidence", "test")

    def test_start_creates_graph_artifacts(self):
        for f in ("state.json", "graph.svg", "graph.html", "graph.md", "log.jsonl"):
            self.assertTrue(Path(self.d, ".ouro", f).exists(), f)
        self.assertEqual(self.ouro.state()["status"], "active")

    def test_second_start_refuses_without_replace(self):
        self.ouro("start", "--no-model", "another goal", ok=(1,))
        self.ouro("start", "--no-model", "--replace", "--quiet", "another goal")

    def test_next_claim_and_done(self):
        self.assertIn("understand", self.ouro("next", "--claim"))
        self.assertEqual({n["id"]: n["status"] for n in self.ouro.state()["nodes"]}["understand"], "in_progress")
        self.ouro("done", "understand", "--evidence", "read the code")
        self.assertIn("design", self.ouro("next"))

    def test_set_verify_rejects_unsafe_command(self):
        self.ouro("set-verify", "understand", "curl http://example.com", ok=(3,))
        self.ouro("set-verify", "understand", "true")

    def test_add_inserts_before_gate(self):
        self.ouro("add", "Extra step", "--id", "extra", "--deps", "understand")
        gate = next(n for n in self.ouro.state()["nodes"] if n["id"] == "verify")
        self.assertIn("extra", gate["deps"])

    def test_stop_hook_loops_then_achieves(self):
        out = json.loads(self.ouro("hook-stop", stdin=self.hook))
        self.assertIn("Not achieved", out["followup_message"])
        self.finish_all()
        self.assertEqual(json.loads(self.ouro("hook-stop", stdin=self.hook)), {})
        self.assertEqual(self.ouro.state()["status"], "achieved")

    def test_verification_beats_self_report(self):
        self.ouro("set-verify", "understand", "false")
        self.finish_all()  # every node self-reported done, but the trusted verify command fails
        out = json.loads(self.ouro("hook-stop", stdin=self.hook))
        self.assertIn("followup_message", out)
        self.assertEqual({n["id"]: n["status"] for n in self.ouro.state()["nodes"]}["understand"], "failed")

    def test_stall_becomes_blocked(self):
        for _ in range(ouro.MAX_STALLS + 1):
            self.ouro("hook-stop", stdin=self.hook)
        self.assertEqual(self.ouro.state()["status"], "blocked")
        self.assertEqual(json.loads(self.ouro("hook-stop", stdin=self.hook)), {})  # blocked runs never loop again

    def test_aborted_session_never_loops(self):
        out = self.ouro("hook-stop", stdin=json.dumps({"workspace_roots": [self.d], "status": "aborted"}))
        self.assertEqual(json.loads(out), {})

    def test_shell_hook_allows_without_laya(self):
        out = json.loads(self.ouro("hook-shell", stdin=json.dumps({"command": "ls"})))
        self.assertEqual(out["permission"], "allow")

    def test_prompt_hook_queues_into_active_run(self):
        self.ouro("hook-prompt", stdin=json.dumps({"workspace_roots": [self.d], "prompt": "also please handle the edge case where the input is empty"}))
        self.assertEqual(len(self.ouro.state()["inbox"]), 1)

    def test_status_json_is_machine_readable(self):
        self.assertEqual(json.loads(self.ouro("status", "--json"))["status"], "active")


if __name__ == "__main__":
    unittest.main()
