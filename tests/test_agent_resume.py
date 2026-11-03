from pathlib import Path
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "rootfs/usr/local/lib/sleipnir-agent-resume"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resume = load("agent_resume", LIB / "agent_resume.py")
setup = load("sleipnir_setup", LIB / "setup.py")


class ResumeCommandTests(unittest.TestCase):
    def label(self, agent, session, args=()):
        return {"agent": agent, "session": session, "cwd": "/home/ubuntu/workspaces/app", "args": list(args)}

    def test_copilot_resume_keeps_setup_flags_only(self):
        args = ["--yolo", "--model", "gpt-5", "--add-dir", "/tmp", "-p", "hi", "--name", "x",
                "--allow-tool", "shell(git)", "write", "--resume=old"]
        groups = resume.launch_flags("copilot", args)
        self.assertEqual(groups, [["--yolo"], ["--model", "gpt-5"], ["--add-dir", "/tmp"],
                                  ["--allow-tool", "shell(git)", "write"]])
        label = self.label("copilot", "81af40d0-4702-43be-801f-a041320e4495", resume.flat(groups))
        self.assertEqual(resume.command(label),
                         "cd '/home/ubuntu/workspaces/app' && copilot --resume=81af40d0-4702-43be-801f-a041320e4495"
                         " --yolo --model gpt-5 --add-dir /tmp --allow-tool 'shell(git)' write")

    def test_opencode_resume(self):
        groups = resume.launch_flags("opencode", ["--auto", "--continue", "--session", "ses_old", "/elsewhere"])
        label = self.label("opencode", "ses_e5def9688ffeR76UfiEWeQ3Lj3", resume.flat(groups))
        self.assertEqual(resume.resume_argv(label), ["opencode", "--session", "ses_e5def9688ffeR76UfiEWeQ3Lj3", "--auto"])

    def test_label_accepts_new_agents(self):
        for agent, session in (("copilot", "81af40d0-4702-43be-801f-a041320e4495"),
                               ("opencode", "ses_e5def9688ffeR76UfiEWeQ3Lj3")):
            raw = json.dumps({"agent": agent, "session": session, "cwd": "/w", "args": []})
            self.assertEqual(resume.read_label({"agent-resume": raw})["agent"], agent)

    def test_default_transcripts(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COPILOT_HOME", None)
            os.environ.pop("XDG_DATA_HOME", None)
            self.assertEqual(resume.default_transcript("copilot", "abc", "/w", home="/h"),
                             "/h/.copilot/session-state/abc/events.jsonl")
            self.assertEqual(resume.default_transcript("opencode", "ses_x", "/w", home="/h"),
                             "/h/.local/share/opencode/opencode.db")


class OpenCodeDatabaseTests(unittest.TestCase):
    def test_has_conversation_reads_session_table(self):
        with tempfile.TemporaryDirectory() as folder:
            database = os.path.join(folder, "opencode.db")
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO session_v2 VALUES ('ses_present000')")
            db.commit()
            db.close()
            label = {"agent": "opencode", "session": "ses_present000", "transcript": database}
            self.assertTrue(resume.has_conversation(label))
            self.assertFalse(resume.has_conversation({**label, "session": "ses_missing000"}))
            self.assertFalse(resume.has_conversation({**label, "transcript": database + ".gone"}))

    def test_unknown_schema_does_not_block(self):
        with tempfile.TemporaryDirectory() as folder:
            database = os.path.join(folder, "opencode.db")
            sqlite3.connect(database).close()
            self.assertTrue(resume.opencode_has("ses_anything00", database))


class HookTests(unittest.TestCase):
    def run_hook(self, agent, payload, names):
        """Runs the hook as if `names` were the agent process above it, in terminal t1."""
        labels = []
        terminal = {"state": "live", "pid": 100, "labels": {}}

        def lookup(pid):
            return {200: {"ppid": 100, "names": names, "start": 1}}.get(pid)

        with mock.patch.object(resume.find_agent, "__defaults__", (lookup,)), \
                mock.patch.object(resume.placement, "__defaults__", (lookup,)), \
                mock.patch.object(resume.os, "getppid", return_value=200), \
                mock.patch.object(resume, "hc_json", return_value=terminal), \
                mock.patch.object(resume, "hc", side_effect=lambda args, **_: labels.append(args)), \
                mock.patch.object(resume, "proc_argv", return_value=None), \
                mock.patch.object(resume.subprocess, "Popen"):
            outcome = resume.hook(agent, json.dumps(payload), env={"HC_TERMINAL_ID": "t1"})
        saved = json.loads(labels[0][2].split("=", 1)[1]) if labels else None
        return outcome, saved

    def test_copilot_session_start(self):
        outcome, saved = self.run_hook("copilot", {
            "hook_event_name": "SessionStart", "session_id": "81af40d0-4702-43be-801f-a041320e4495",
            "cwd": "/w", "source": "new"}, {"MainThread", "copilot"})
        self.assertEqual(outcome, "saved")
        self.assertEqual((saved["agent"], saved["pid"], saved["shell"]), ("copilot", 200, 100))
        self.assertTrue(saved["transcript"].endswith("/session-state/81af40d0-4702-43be-801f-a041320e4495/events.jsonl"))

    def test_copilot_subagent_ignored(self):
        outcome, saved = self.run_hook("copilot", {
            "hook_event_name": "SessionStart", "session_id": "81af40d0-4702-43be-801f-a041320e4495",
            "agent_id": "sub", "cwd": "/w"}, {"copilot"})
        self.assertEqual((outcome, saved), ("subagent", None))

    def test_opencode_tui_report(self):
        outcome, saved = self.run_hook("opencode", {
            "hook_event_name": "SessionStart", "session_id": "ses_e5def9688ffeR76UfiEWeQ3Lj3", "cwd": "/w"},
            {"opencode"})
        self.assertEqual(outcome, "saved")
        self.assertEqual((saved["agent"], saved["cwd"]), ("opencode", "/w"))
        self.assertTrue(saved["transcript"].endswith("/opencode/opencode.db"))


class SetupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "SLEIPNIR_ENABLE_AGENT_RESUME": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("XDG_CONFIG_HOME", "CODEX_HOME", "COPILOT_HOME", "OPENCODE_CONFIG_DIR"):
            os.environ.pop(key, None)

    def test_installs_only_where_agents_are_set_up(self):
        setup.main()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_installs_new_agents_idempotently(self):
        (self.home / ".copilot").mkdir()
        (self.home / ".gemini/config").mkdir(parents=True)
        (self.home / ".config/opencode").mkdir(parents=True)
        agy = self.home / ".gemini/config/hooks.json"
        agy.write_text(json.dumps({"moshi-hook": {"Stop": []}}))
        agy.chmod(0o640)
        setup.main()
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        setup.main()
        after = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

        copilot = json.loads((self.home / ".copilot/hooks/sleipnir-agent-resume.json").read_text())
        self.assertEqual(copilot["hooks"]["SessionStart"][0]["command"],
                         "/usr/local/bin/sleipnir-agent-resume hook copilot")
        config = json.loads(agy.read_text())
        self.assertIn("moshi-hook", config)
        self.assertEqual(config["sleipnir-agent-resume"]["PreInvocation"][0]["command"],
                         "/usr/local/bin/sleipnir-agent-resume hook agy")
        self.assertEqual(agy.stat().st_mode & 0o777, 0o640)
        self.assertTrue((self.home / ".gemini/config/hooks.json.before-sleipnir-agent-resume").exists())
        plugin = self.home / ".config/opencode/plugins/sleipnir-agent-resume/tui.js"
        self.assertEqual(plugin.read_text(), (LIB / "opencode_tui.js").read_text())

    def test_agy_refuses_foreign_block(self):
        (self.home / ".gemini/config").mkdir(parents=True)
        agy = self.home / ".gemini/config/hooks.json"
        agy.write_text(json.dumps({"sleipnir-agent-resume": {"Stop": [{"command": "other"}]}}))
        with mock.patch.object(sys, "stderr"):
            setup.main()
        self.assertEqual(json.loads(agy.read_text())["sleipnir-agent-resume"], {"Stop": [{"command": "other"}]})

    def test_claude_and_codex_unchanged(self):
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        setup.main()
        setup.main()
        for path, agent in ((".claude/settings.json", "claude"), (".codex/hooks.json", "codex")):
            groups = json.loads((self.home / path).read_text())["hooks"]["SessionStart"]
            self.assertEqual(groups, [{"hooks": [{"type": "command", "timeout": 10,
                                                  "command": f"/usr/local/bin/sleipnir-agent-resume hook {agent}"}]}])


if __name__ == "__main__":
    unittest.main()
