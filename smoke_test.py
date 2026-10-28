"""Offline smoke test for AutoMind. Run: python smoke_test.py"""
import os
import tempfile
import config, ai_hub, executor, memory_manager, plugin_manager

assert config.VERSION == "2.5.0"
assert ai_hub.detect_domain("write Python code for Android") == "tech"
assert ai_hub.detect_domain("weather in Jaipur") == "general"
actions = executor.plan_actions("open youtube nifty analysis")
assert len(actions) == 1 and actions[0].app == "youtube"
sample = open(os.path.join(config.PLUGINS_DIR, "prompt_pack.py"), encoding="utf-8").read()
assert plugin_manager.inspect_source(sample)["name"] == "prompt_pack"
with tempfile.TemporaryDirectory() as td:
    m = memory_manager.MemoryManager(os.path.join(td, "kb.json"))
    m.save_research("general", "test topic", "test conclusion")
    assert m.find_similar("test topic", "general")["conclusion"] == "test conclusion"
    m.remember("language", "Hindi")
    assert m.profile_items("language")[0][1] == "Hindi"
    assert m.forget_profile("language")
print("AutoMind smoke tests passed.")
