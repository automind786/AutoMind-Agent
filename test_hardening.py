"""Regression tests for Android URL fallback and safe local behavior."""
import os
import tempfile
import unittest
from unittest.mock import patch

import executor
import memory_manager
import config
import plugin_manager


class AndroidFallbackTests(unittest.TestCase):
    def test_no_browser_fallback_when_bridge_unavailable(self):
        url = "https://gemini.google.com/"
        with patch.object(executor.config, "is_android", return_value=False):
            ok, error = executor.open_url(url)
        self.assertFalse(ok)
        self.assertIn("browser launch is disabled", error)
        self.assertIn("gemini.google.com", url)

    def test_executor_has_no_webbrowser_open_call(self):
        with open(executor.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("webbrowser.open", source)

    def test_android_activity_manager_launch_success(self):
        from types import SimpleNamespace
        with patch.object(executor.config, "is_android", return_value=True), \
             patch.object(executor.shutil, "which", return_value="/system/bin/am"), \
             patch.object(executor.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            self.assertEqual(executor.open_url("https://gemini.google.com/"), (True, ""))
        self.assertEqual(run.call_args.args[0], ["/system/bin/am", "start", "-a", "android.intent.action.VIEW", "-d", "https://gemini.google.com/"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_android_activity_manager_failure_falls_back_honestly(self):
        from types import SimpleNamespace
        with patch.object(executor.config, "is_android", return_value=True), \
             patch.object(executor.shutil, "which", return_value="/system/bin/am"), \
             patch.object(executor.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="No activity found")), \
             patch.dict("sys.modules", {"androidhelper": None}):
            ok, error = executor.open_url("https://gemini.google.com/")
        self.assertFalse(ok)
        self.assertIn("No activity found", error)
        self.assertIn("automatic browser launch is disabled", error)

    def test_rejects_non_http_url(self):
        ok, message = executor.open_url("file:///etc/passwd")
        self.assertFalse(ok)
        self.assertIn("HTTP(S)", message)

    def test_no_generic_android_import_is_attempted(self):
        with open(executor.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("import android\n", source)

    def test_android_detected_from_activity_manager_path(self):
        with patch.object(config.os.path, "exists", side_effect=lambda p: p == "/system/bin/am"):
            self.assertTrue(config.is_android())


class MemorySafetyTests(unittest.TestCase):
    def test_corrupt_memory_is_preserved_and_recovered(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "memory.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{broken")
            manager = memory_manager.MemoryManager(path)
            self.assertTrue(os.path.exists(path + ".corrupt"))
            self.assertIsInstance(manager.db, dict)
            manager.remember("language", "Hindi")
            reloaded = memory_manager.MemoryManager(path)
            self.assertTrue(reloaded.profile_items("language"))

    def test_plugin_invalid_source_rejected(self):
        with self.assertRaises(plugin_manager.PluginError):
            plugin_manager.inspect_source("def run(args, context): return 'x'")

    def test_selfrepair_command_is_wired(self):
        # Test the actual dispatcher used by this version. Older builds used a
        # separate `elif cmd == ...` command parser; this build uses dispatch().
        import ast
        source_path = os.path.join(config.BASE_DIR, "main.py")
        with open(source_path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=source_path)
        orchestrator = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Orchestrator"
        )
        methods = {node.name: node for node in orchestrator.body if isinstance(node, ast.FunctionDef)}
        self.assertIn("cmd_selfrepair", methods)
        self.assertIn("dispatch", methods)
        dispatch_source = ast.get_source_segment(source, methods["dispatch"]) or ""
        self.assertIn('("selfrepair full", "selfrepair")', dispatch_source)
        self.assertIn("self.cmd_selfrepair()", dispatch_source)

if __name__ == "__main__":
    unittest.main(verbosity=2)
