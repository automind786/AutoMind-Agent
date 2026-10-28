"""Safe offline audit for AutoMind. Run from console with: test
No network requests, external launches, destructive actions or live-data claims.
"""
import json
import os
import tempfile
import traceback
from unittest.mock import patch
import main

import ai_hub
import config
import executor
import memory_manager
import plugin_manager


def run():
    checks = []

    def check(name, fn):
        try:
            detail = fn()
            checks.append((name, True, str(detail or "OK")))
        except Exception as exc:
            checks.append((name, False, "%s: %s" % (type(exc).__name__, exc)))

    def truth(value, message):
        if not value:
            raise AssertionError(message)
        return message

    check("Python/runtime imports", lambda: truth(bool(config.APP_NAME), "imports passed"))
    check("Version metadata", lambda: truth(config.VERSION == "2.5.0", config.VERSION))
    check("AI provider registry", lambda: truth(all(k in config.AI_TARGETS for k in config.AI_ORDER),
                                                   "%d providers configured" % len(config.AI_ORDER)))
    check("AI provider web routes", lambda: truth(all(config.AI_TARGETS[k].get("web") for k in config.AI_ORDER),
                                                   "all configured providers have a web route"))
    check("Prompt generation", lambda: truth("OBJECTIVE:" in ai_hub.build_expert_prompt("test request"),
                                               "structured prompt created"))
    check("Domain detection: coding", lambda: truth(ai_hub.detect_domain("debug Python code") == "tech", "tech"))
    check("Domain detection: general", lambda: truth(ai_hub.detect_domain("weather tomorrow") == "general", "general"))
    check("URL encoding", lambda: truth("%20" in ai_hub.fit_quoted("hello world")[1], "spaces safely quoted"))
    check("App registry aliases", lambda: truth("youtube" in executor.detect_apps("open YouTube"), "YouTube detected"))
    check("Action planning", lambda: truth(executor.plan_actions("open youtube") [0].app == "youtube", "direct app action planned"))
    check("Offline settings JSON", lambda: truth(isinstance(config.load_smart_settings(), dict), "settings readable"))
    check("Knowledge base JSON", lambda: truth(isinstance(json.load(open(config.KNOWLEDGE_BASE_PATH, encoding="utf-8")), dict),
                                                  "knowledge base readable"))

    def memory_test():
        with tempfile.TemporaryDirectory() as td:
            m = memory_manager.MemoryManager(os.path.join(td, "kb.json"))
            m.save_research("general", "total test topic", "saved conclusion")
            found = m.find_similar("total test topic", "general")
            truth(bool(found) and found["conclusion"] == "saved conclusion", "save/retrieve passed")
            m.remember("test_language", "Hindi")
            truth(bool(m.profile_items("test_language")), "profile save/retrieve passed")
            return "temporary test database only; real data untouched"
    check("Memory save/retrieve/profile", memory_test)

    def plugin_test():
        path = os.path.join(config.PLUGINS_DIR, "prompt_pack.py")
        with open(path, encoding="utf-8") as fh:
            info = plugin_manager.inspect_source(fh.read())
        truth(info.get("name") == "prompt_pack", "sample plugin source valid")
        return "sample plugin syntax/metadata valid; plugins are not executed by this test"
    check("Plugin source validation", plugin_test)

    def confirm_test():
        with patch("builtins.input", return_value="y"):
            truth(main.confirm("test", False) is True, "y accepted")
        with patch("builtins.input", return_value="n"):
            truth(main.confirm("test", True) is False, "n accepted")
        with patch("builtins.input", return_value=""):
            truth(main.confirm("test", True) is True, "blank uses default")
        with patch("builtins.input", return_value="unexpected"):
            truth(main.confirm("test", True) is False, "invalid input safely declines")
        return "yes/no/default/invalid-input paths passed; no repeated prompt loop"
    check("Approval prompt input handling", confirm_test)

    check("Prompt output path writable", lambda: truth(os.access(config.BASE_DIR, os.W_OK), config.BASE_DIR))
    check("Android bridge availability", lambda: "Android=" + str(config.is_android()) +
          "; launch test intentionally not performed")

    def auto_update_test():
        import tempfile
        import auto_update
        safe = auto_update.validate_safe_settings({"offline_mode": True, "response_language": "Hindi"})
        truth(safe["offline_mode"] is True, "safe settings accepted")
        try:
            auto_update.validate_safe_settings({"main.py": "malicious code"})
        except auto_update.UpdatePolicyError:
            pass
        else:
            raise AssertionError("core-code update must be rejected")
        try:
            auto_update.check_manifest("http://example.com/manifest.json")
        except auto_update.UpdatePolicyError:
            pass
        else:
            raise AssertionError("non-HTTPS manifest must be rejected")
        with tempfile.TemporaryDirectory() as td:
            path=os.path.join(td,"settings.json")
            with open(path,"w",encoding="utf-8") as f: json.dump({"offline_mode":False},f)
            result=auto_update.apply_safe_settings(path,{"offline_mode":True})
            with open(path,encoding="utf-8") as f: saved=json.load(f)
            truth(saved["offline_mode"] is True, "atomic safe settings update")
            truth(bool(result["backup"]) and os.path.exists(result["backup"]), "backup created")
        return "safe settings, core-code rejection, HTTPS-only and backup passed"
    check("Safe auto-update policy", auto_update_test)

    def diagnostics_modules_test():
        import ast
        source = open(os.path.join(config.BASE_DIR, "main.py"), encoding="utf-8").read()
        tree = ast.parse(source)
        names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        truth("cmd_doctor" in names and "cmd_backup" in names, "doctor and backup commands present")
        truth("doctor" in source and "backup" in source, "help/dispatch wired")
        return "local diagnostics and timestamped data backup commands available"
    check("Final update diagnostics and backup", diagnostics_modules_test)

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = len(checks) - passed
    print("\n=== AutoMind TOTAL TEST ===")
    for name, ok, detail in checks:
        print("[%s] %s — %s" % ("PASS" if ok else "FAIL", name, detail))
    print("\nSUMMARY: %d passed, %d failed, %d total" % (passed, failed, len(checks)))
    print("NOT TESTED: real browser/app launch, internet access, AI responses, clipboard hardware and API services.")
    print("Reason: those require live Android/user interaction or provider credentials and are not safe to fake.")
    print("CONCLUSION: this report tests local code only; PASS does not mean every AI provider or app launch works.")
    return failed == 0


if __name__ == "__main__":
    run()
