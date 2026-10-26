#!/usr/bin/env python3
"""AutoMind safer console with bounded agent execution, feature synthesis, universal app hub, voice input, and natural conversational chat."""
import os
import sys
import time
import shutil
import traceback

try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE_DIR = os.getcwd()

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import ai_hub
import config
import executor
import memory_manager
import plugin_manager
import smart_updater
import auto_update

try:
    import agent_tools
except ImportError:
    agent_tools = None

try:
    import agent_self_repair
except ImportError:
    agent_self_repair = None

try:
    import agent_feature_synth
except ImportError:
    agent_feature_synth = None

try:
    import agent_app_hub
except ImportError:
    agent_app_hub = None

try:
    import agent_voice
except ImportError:
    agent_voice = None

try:
    from agent_runtime import AgentRuntime
except Exception:
    AgentRuntime = None

try:
    import agent_workflows
except ImportError:
    agent_workflows = None


HELP = """
AutoMind Commands & Features:
  bolo / mic              Aawaz se bolkar command dein (Voice Input)
  chat <message>          Samanya bhasha me AutoMind se seedhe baat karein
  help                    Show this help menu
  doctor                  Run full system local health check
  test                    Run 19 automated system diagnostics
  app register <name>     Register a new target application (Tally, Excel, YouTube)
  app run <name>          Start interactive data collection and execution session
  app list                Show all registered applications
  synth <tool> <desc>     Auto-generate, test & inject a new Python tool
  plan <task>             Decompose, track, and execute adaptive task steps
  learn <task> = <steps>  Directly teach AutoMind custom workflows
  calc <expression>       Safe math calculator
  write <file> <text>     Save text directly to a file
  read <file>             Read content of a file
  agent <command>         Execute bounded direct OS actions
  backup                  Create a full timestamped backup of the project
  selfrepair full         Check syntax integrity of all project files
  exit                    Close AutoMind
"""


def ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def confirm(question, default=False):
    ans = ask("%s (%s): " % (question, "Y/n" if default else "y/N")).lower()
    if not ans:
        return default
    if ans in ("y", "yes"):
        return True
    if ans in ("n", "no"):
        return False
    print("Unrecognized answer; treating it as NO.")
    return False


def conversational_chat(user_text, context=None):
    """Samanya bolchal me natural Hindi/Hinglish me jawab deta hai."""
    text = user_text.strip().lower()

    if any(g in text for g in ["kese ho", "kaise ho", "kya haal", "kya chal raha"]):
        return "Main bilkul badhiya hoon dost! AutoMind poora ready hai. Aaj YouTube, Tally, Excel ya coding me kya kaam karna hai?"
    
    if any(g in text for g in ["namaste", "hello", "hi", "hey", "salam"]):
        return "Namaste! Batayein, aaj kis kaam ya application par chalna hai? Aap mujhse normal bhasha me bolkar ya likhkar baat kar sakte hain."

    if any(k in text for k in ["tally"]):
        return "Tally ke liye aap bol sakte hain 'Tally me entry karo'! Main aapse Party name, Voucher type aur Amount poochkar direct entry file bana dunga."

    if any(k in text for k in ["excel"]):
        return "Excel ke liye aap bol sakte hain 'Excel sheet banao', aur main aapse rows aur columns poochkar sheet bana dunga."

    if any(k in text for k in ["youtube"]):
        return "YouTube ke liye aap seedhe bolein 'YouTube par video upload karni hai', main turant session start kar dunga!"

    if any(k in text for k in ["shukriya", "thanks", "dhanyawad"]):
        return "Arey bilkul koi baat nahi! Hamesha aapki madad ke liye tayyar hoon."

    return (
        "Main aapki baat samajh gaya! Aap chahein to mujhse seedhe sawal pooch sakte hain, "
        "ya kisi application par kaam shuru karne ke liye bol sakte hain."
    )


class Orchestrator:
    def __init__(self):
        self.memory = memory_manager.MemoryManager()
        self.context = {"memory": self.memory, "hub": ai_hub,
                        "executor": executor, "config": config}
        
        try:
            self.plugins = plugin_manager.PluginManager(config.PLUGINS_DIR, self.context)
        except Exception:
            try:
                self.plugins = plugin_manager.PluginManager()
            except Exception:
                self.plugins = None

        self.updater = None
        for init_call in (
            lambda: smart_updater.SmartUpdater(self.plugins),
            lambda: smart_updater.SmartUpdater(self),
            lambda: smart_updater.SmartUpdater(self.plugins, self.memory),
            lambda: smart_updater.SmartUpdater()
        ):
            try:
                self.updater = init_call()
                break
            except Exception:
                continue

        self.agent = AgentRuntime(max_steps=10, max_retries=2) if AgentRuntime else None
        self.offline = False
        self.running = True
        self._load_preferences()

    def _load_preferences(self):
        try:
            settings = getattr(config, "load_smart_settings", None)
            if callable(settings):
                loaded = settings()
                if isinstance(loaded, dict):
                    self.context.update(loaded)
        except Exception:
            pass

    def log(self, message):
        print(message)
    def dispatch(self, raw):
        raw = (raw or "").strip()
        if not raw:
            return
        lower = raw.lower()

        if lower in ("exit", "quit", "q"):
            self.running = False
            return
        if lower in ("help", "?"):
            print(HELP)
            return
        if lower in ("bolo", "mic", "voice", "listen"):
            self.cmd_voice()
            return
        if lower == "doctor":
            self.cmd_doctor()
            return
        if lower in ("test", "total-test", "checkall"):
            self.cmd_test()
            return
        if lower == "backup":
            self.cmd_backup()
            return
        if lower in ("selfrepair full", "selfrepair"):
            self.cmd_selfrepair()
            return
        if lower.startswith("selfrepair check "):
            self.cmd_selfrepair_check(raw[17:].strip())
            return
        if lower.startswith("learn "):
            self.cmd_learn(raw[6:].strip())
            return
        if lower.startswith("synth "):
            self.cmd_synth(raw[6:].strip())
            return
        if lower.startswith("app register "):
            self.cmd_app_register(raw[13:].strip())
            return
        if lower.startswith("app run "):
            self.cmd_app_run(raw[8:].strip())
            return
        if lower == "app list":
            self.cmd_app_list()
            return
        if lower.startswith("chat "):
            reply = conversational_chat(raw[5:].strip(), self.context)
            print("\nAutoMind:", reply)
            return
        if lower == "update":
            self.cmd_update()
            return
        if lower == "autoupdate":
            self.cmd_autoupdate()
            return
        if lower == "plugins":
            self.cmd_plugins()
            return
        if lower == "notes":
            self.cmd_notes()
            return
        if lower == "history":
            self.cmd_history()
            return
        if lower == "prefs":
            self.cmd_prefs()
            return
        if lower.startswith("set "):
            self.cmd_set(raw[4:])
            return
        if lower.startswith("remember "):
            self.cmd_remember(raw[9:])
            return
        if lower.startswith("forget "):
            self.cmd_forget(raw[7:])
            return
        if lower == "profile":
            self.cmd_profile()
            return
        if lower == "forgetinfo":
            self.cmd_forgetinfo()
            return
        if lower == "offline":
            self.offline = not self.offline
            print("Offline mode:", "ON" if self.offline else "OFF")
            return
        if lower.startswith("calc "):
            if agent_tools:
                ok, res = agent_tools.tool_calculate(raw[5:].strip())
                print(res)
            else:
                print("agent_tools module unavailable.")
            return
        if lower.startswith("write "):
            parts = raw[6:].strip().split(maxsplit=1)
            if len(parts) == 2 and agent_tools:
                ok, res = agent_tools.tool_write_file(parts[0], parts[1])
                print(res)
            else:
                print("Usage: write <filename> <content>")
            return
        if lower.startswith("read "):
            filename = raw[5:].strip()
            if filename and agent_tools:
                ok, res = agent_tools.tool_read_file(filename)
                print(res)
            else:
                print("Usage: read <filename>")
            return
        if lower == "plan" or lower.startswith("plan "):
            self.cmd_plan(raw[5:].strip() if len(raw) > 5 else "")
            return
        if lower == "agent" or lower.startswith("agent "):
            self.cmd_agent(raw[5:].strip() if len(raw) > 5 else "")
            return

        self.handle_intent(raw)

    def cmd_voice(self):
        if agent_voice:
            ok, spoken = agent_voice.listen_and_convert()
            if ok and spoken:
                print("\n[Executing Spoken Command]:", spoken)
                self.dispatch(spoken)
        else:
            print("[Voice]: agent_voice.py module nahi mila. Aap keyboard ke mic se bhi bol sakte hain.")

    def cmd_app_register(self, app_target):
        if not app_target:
            print("Usage: app register <application_name>")
            return
        if agent_app_hub:
            ok, profile = agent_app_hub.discover_and_register_app(app_target)
            print("[App Hub]: Registered '%s'" % profile["name"])
            print("[App Hub]: Protocol detected -> %s" % profile["protocol"])
            print("[App Hub]: Execution Mode   -> %s" % profile["mode"])
            print("[App Hub]: Description      -> %s" % profile["description"])
        else:
            print("agent_app_hub module unavailable.")

    def cmd_app_run(self, app_target):
        if not app_target:
            print("Usage: app run <application_name>")
            return
        if agent_app_hub:
            if confirm("Start interactive automation session for '%s'?" % app_target, default=True):
                ok, msg = agent_app_hub.execute_app_interactive_session(app_target, ask, print)
                print("[App Hub Execution]:", msg)
            else:
                print("Execution cancelled.")
        else:
            print("agent_app_hub module unavailable.")

    def cmd_app_list(self):
        if agent_app_hub:
            reg = agent_app_hub.load_registry()
            if not reg:
                print("No apps registered yet. Use: app register <name>")
            else:
                print("\nRegistered Universal Applications:")
                for k, v in reg.items():
                    print("  - %-12s | Protocol: %-18s | Mode: %s" % (v["name"], v["protocol"], v["mode"]))
        else:
            print("agent_app_hub module unavailable.")

    def cmd_synth(self, text):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            print("Usage: synth <tool_name> <what it does>")
            return
        tool_name, desc = parts[0], parts[1]
        if not agent_feature_synth:
            print("agent_feature_synth module unavailable.")
            return

        print("[Feature Synth]: Synthesizing tool '%s'..." % tool_name)
        fn_name, code = agent_feature_synth.build_tool_template(tool_name, desc)
        ok, res = agent_feature_synth.append_tool_safely(tool_name, code)
        print("[Feature Synth]:", res)
        if ok and agent_self_repair:
            agent_self_repair.update_agent_knowledge(
                "use tool " + tool_name,
                ["Check tool input data", "Call " + fn_name, "Verify execution output"]
            )
    def cmd_learn(self, text):
        if "=" not in text:
            print("Usage: learn <task description> = <step 1 | step 2 | step 3>")
            return
        task_part, steps_part = text.split("=", 1)
        task_name = task_part.strip()
        steps = [s.strip() for s in steps_part.split("|") if s.strip()]
        if not task_name or not steps:
            print("Error: Both task and at least one step are required.")
            return

        if agent_self_repair:
            ok, msg = agent_self_repair.update_agent_knowledge(task_name, steps)
            print("[Self-Update]", msg)
        else:
            print("agent_self_repair module unavailable.")

    def cmd_plan(self, task):
        task = task.strip()
        if not task:
            print("Usage: plan <task or goal description>")
            return

        print("\n--- AGENT PLANNER: Task Decomposition ---")
        steps = []
        source = "Built-in planner"
        safety_notice = "Standard boundary: AutoMind plans tasks and requires confirmation before any action."

        if agent_self_repair:
            learned = agent_self_repair.get_learned_task(task)
            if learned and "steps" in learned:
                steps = list(learned["steps"])
                source = "Learned Memory (Auto-Knowledge Base)"
                print("[Self-Update Engine]: Retrieved previously learned workflow!")

        if not steps and agent_workflows:
            wf_key = agent_workflows.choose_workflow(task)
            wf = agent_workflows.WORKFLOWS.get(wf_key)
            if wf:
                source = "Predefined Workflow: %s" % wf["title"]
                steps = list(wf["steps"])
                safety_notice = wf.get("safety", safety_notice)

        if not steps:
            clean_tag = task.replace(" ", "_")[:20]
            steps = [
                "Clarify specific requirements and constraints for: %s" % task,
                "Gather verified local context and preferences",
                "Draft strategy or content draft for: %s" % task,
                "Save action plan summary to local file (plan_%s.txt)" % clean_tag,
                "Seek final confirmation and report results"
            ]

        print("Source: %s" % source)
        print("Planned Steps:")
        for idx, step in enumerate(steps, 1):
            print("  %d. %s" % (idx, step))
        print("\nSafety boundary: %s" % safety_notice)
        print("Status: PLAN ONLY — awaiting user approval.")

        if self.agent and confirm("\nExecute plan tracking with AgentRuntime?", default=False):
            def execute_step(step_desc):
                print("  -> Tracking: %s" % step_desc)
                if "Save action plan summary to local file" in step_desc and agent_tools:
                    fname = "plan_%s.txt" % task.replace(" ", "_")[:20]
                    content = "AutoMind Plan for: %s\nGenerated: %s\n\nSteps:\n" % (task, time.ctime())
                    for i, s in enumerate(steps, 1):
                        content += "%d. %s\n" % (i, s)
                    ok, res = agent_tools.tool_write_file(fname, content)
                    print("     [Tool Action]: %s" % res)
                    return "completed" if ok else "failed"
                return "completed"

            def verify_step(step_desc, result):
                return result == "completed"

            print("\nExecuting tracked plan via AgentRuntime...")
            run_res = self.agent.run(steps, execute_step, verify_step)
            if run_res.get("success"):
                print("[OK] All steps verified and completed in tracking runtime.")
                if agent_self_repair:
                    ok_learn, msg_learn = agent_self_repair.update_agent_knowledge(task, steps)
                    print("[Self-Update Engine]: %s" % msg_learn)
            else:
                print("[WARN] Execution stopped: %s" % run_res.get("error", "Verification failed"))
            self._record_history("plan " + task, run_res)
        else:
            print("Execution skipped. Plan kept for informational reference.")
            self._record_history("plan " + task, {"status": "planned_only"})

    def cmd_selfrepair_check(self, filename):
        if not filename:
            print("Usage: selfrepair check <filename.py>")
            return
        target = os.path.join(BASE_DIR, filename)
        if not os.path.exists(target):
            print("File not found:", filename)
            return
        try:
            with open(target, "r", encoding="utf-8") as f:
                code = f.read()
            if agent_self_repair:
                ok, msg = agent_self_repair.validate_python_code(code)
                print("[VALID]" if ok else "[ERROR]", msg)
            else:
                compile(code, target, "exec")
                print("[OK] Syntax is valid.")
        except Exception as exc:
            print("[ERROR] File contains syntax issues:", exc)
    def cmd_doctor(self):
        import json
        print("AutoMind local health check:")
        modules = ["main.py", "config.py", "memory_manager.py", "plugin_manager.py",
                   "auto_update.py", "knowledge_base.json", "agent_runtime.py",
                   "agent_tools.py", "agent_self_repair.py", "agent_feature_synth.py",
                   "agent_app_hub.py", "agent_voice.py"]
        checks = 0
        for rel in modules:
            ok = os.path.isfile(os.path.join(BASE_DIR, rel))
            checks += int(ok)
            print("  [%s] %s" % ("OK" if ok else "MISSING", rel))
        
        dbpath = getattr(config, "KNOWLEDGE_BASE_PATH", os.path.join(BASE_DIR, "knowledge_base.json"))
        try:
            with open(dbpath, "r", encoding="utf-8") as f:
                db = json.load(f)
            valid = isinstance(db, dict)
            print("  [%s] memory JSON readable" % ("OK" if valid else "CHECK"))
            if valid:
                learned_count = len(db.get("learned_tasks", {}))
                print("       profile: %d; research: %d; learned tasks: %d; history: %d" %
                      (len(db.get("profile", {})), len(db.get("research", {})), learned_count, len(db.get("history", []))))
                checks += 1
        except Exception as exc:
            print("  [ERROR] memory JSON: %s: %s" % (type(exc).__name__, exc))

        try:
            probe = os.path.join(BASE_DIR, ".automind_write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            print("  [OK] project folder writable")
            checks += 1
        except Exception as exc:
            print("  [CHECK] project folder not writable: %s" % exc)
        print("Checks passed: %d (local checks only)" % checks)

    def cmd_test(self):
        try:
            import total_test
            total_test.run()
        except Exception as exc:
            print("TOTAL TEST could not start: %s: %s" % (type(exc).__name__, exc))

    def cmd_backup(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_root = os.path.join(BASE_DIR, "backups")
        dest_dir = os.path.join(backup_root, "AutoMind-full-backup-" + stamp)
        os.makedirs(dest_dir, exist_ok=True)
        count = 0
        excluded_dirs = {"backups", "__pycache__", ".git"}
        for root, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [d for d in dirs if d not in excluded_dirs]
            rel = os.path.relpath(root, BASE_DIR)
            target_root = dest_dir if rel == "." else os.path.join(dest_dir, rel)
            os.makedirs(target_root, exist_ok=True)
            for filename in files:
                src = os.path.join(root, filename)
                dst = os.path.join(target_root, filename)
                try:
                    if os.path.abspath(src) == os.path.abspath(dst):
                        continue
                    shutil.copy2(src, dst)
                    count += 1
                except Exception as exc:
                    print("[WARN] Could not back up", src, "-", exc)
        print("Backup complete:", dest_dir)
        print("Files copied:", count, "(original files were not changed)")

    def cmd_selfrepair(self):
        checked, errors = 0, 0
        for root, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [d for d in dirs if d not in {"backups", "__pycache__", ".git"}]
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(root, filename)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        compile(handle.read(), path, "exec")
                    checked += 1
                    print("[OK]", os.path.relpath(path, BASE_DIR))
                except Exception as exc:
                    errors += 1
                    print("[ERROR]", os.path.relpath(path, BASE_DIR), "-", exc)
        print("Syntax check finished:", checked, "checked;", errors, "error(s)")

    def cmd_update(self):
        if not self.updater:
            print("SmartUpdater is not available.")
            return
        for method_name in ("interactive", "run", "show_plugins"):
            method = getattr(self.updater, method_name, None)
            if callable(method):
                try:
                    method()
                    return
                except Exception as exc:
                    print("Update action failed:", exc)
                    return
        print("Smart updater loaded.")

    def cmd_autoupdate(self):
        try:
            status_fn = getattr(auto_update, "status", None)
            if callable(status_fn):
                policy = status_fn(os.path.join(BASE_DIR, "auto_update_policy.json"))
                print("Safe settings auto-update: %s" % policy.get("auto_safe_settings"))
                print("Online checks enabled: %s" % policy.get("online_check_enabled"))
                print("Manifest URL configured: %s" % bool(policy.get("manifest_url")))
            else:
                print("No auto_update status function detected.")
        except Exception as exc:
            print("Auto-update check failed:", exc)

    def cmd_plugins(self):
        if not self.plugins:
            print("Plugin manager is not loaded.")
            return
        for name in ("list_plugins", "list", "available_plugins"):
            method = getattr(self.plugins, name, None)
            if callable(method):
                try:
                    res = method()
                    print(res if res is not None else "No plugins found.")
                    return
                except Exception as exc:
                    print("Plugin listing failed:", exc)
                    return
        loaded = getattr(self.plugins, "plugins", None)
        if isinstance(loaded, dict):
            print("Active plugins:", list(loaded.keys()))
        else:
            print("Plugin manager loaded.")

    def cmd_notes(self):
        for name in ("list_research", "list_notes", "get_notes"):
            value = getattr(self.memory, name, None)
            if callable(value):
                try:
                    entries = value()
                    if isinstance(entries, list):
                        for e in entries[:10]:
                            print("- [%s] %s: %s" % (e.get("domain", ""), e.get("topic", ""), str(e.get("conclusion", ""))[:120]))
                    else:
                        print(entries)
                    return
                except Exception as exc:
                    print("Could not read notes:", exc)
                    return
        print("No compatible notes method found.")

    def cmd_history(self):
        for name in ("recent_history", "get_history", "history"):
            value = getattr(self.memory, name, None)
            if callable(value):
                try:
                    hist = value(15) if name == "recent_history" else value()
                    if isinstance(hist, list):
                        for h in hist:
                            print("  %s %-14s %s" % (str(h.get("ts", ""))[:19].replace("T", " "),
                                                     str(h.get("kind", "")), str(h.get("detail", ""))))
                    else:
                        print(hist)
                    return
                except Exception as exc:
                    print("Could not read history:", exc)
                    return
        print("No compatible history method found.")

    def cmd_prefs(self):
        db = getattr(self.memory, "db", {})
        prefs = db.get("preferences", self.context)
        if isinstance(prefs, dict):
            for k, v in sorted(prefs.items()):
                print("  %-22s %s" % (k, v))
        else:
            print(self.context if self.context else "No preferences loaded.")

    def cmd_set(self, text):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            print("Usage: set KEY VALUE")
            return
        key, value = parts
        set_pref = getattr(self.memory, "set_pref", None)
        if callable(set_pref):
            set_pref(key, value)
        self.context[key] = value
        print("Preference updated: %s = %s" % (key, value))

    def cmd_remember(self, text):
        if not text.strip():
            print("Usage: remember KEY=VALUE  or  remember TEXT")
            return
        method = getattr(self.memory, "remember", None)
        if callable(method):
            try:
                if "=" in text:
                    k, v = text.split("=", 1)
                    method(k.strip(), v.strip())
                else:
                    method("note", text.strip())
                print("Saved to memory.")
                return
            except Exception as exc:
                print("Could not save to memory:", exc)
        print("Memory save method unavailable.")

    def cmd_forget(self, text):
        for name in ("delete_research", "forget", "forget_profile"):
            method = getattr(self.memory, name, None)
            if callable(method):
                try:
                    print("Deleted." if method(text.strip()) else "No matching memory found.")
                    return
                except Exception as exc:
                    print("Could not delete from memory:", exc)
                    return
        print("Memory remove method unavailable.")

    def cmd_profile(self):
        method = getattr(self.memory, "profile_items", None)
        if callable(method):
            items = method("")
            if items:
                for key, val, updated in items:
                    print("- %s: %s" % (key, val))
                return
        print("No saved profile information found.")

    def cmd_forgetinfo(self):
        for name in ("clear_profile", "forget_profile"):
            method = getattr(self.memory, name, None)
            if callable(method):
                try:
                    method()
                    print("Profile cleared.")
                    return
                except Exception:
                    pass
        print("Cleared profile in context.")

    def cmd_agent(self, request):
        if not self.agent:
            print("Agent runtime unavailable. Ensure agent_runtime.py is beside main.py.")
            return
        if request.lower() == "test":
            try:
                result = self.agent.run(
                    steps=["local verification step"],
                    execute_fn=lambda step: "done",
                    verify_fn=lambda step, result: result == "done",
                )
                print(result)
            except Exception:
                traceback.print_exc()
            return
        if not request:
            print("Usage: agent <direct action request>")
            return

        is_direct = getattr(executor, "is_direct_command", None)
        if not callable(is_direct) or not is_direct(request):
            print("This agent mode only executes direct actions recognized by executor.")
            return

        planner = getattr(executor, "plan_actions", None)
        opener = getattr(executor, "open_app", None)
        result_type = getattr(executor, "ExecResult", None)
        if not callable(planner) or not callable(opener) or result_type is None:
            print("Executor is missing required functions for agent execution.")
            return

        try:
            actions = planner(request)
        except Exception:
            actions = planner(request, domain="general")

        print("Proposed actions:")
        for idx, act in enumerate(actions, 1):
            desc = getattr(act, "describe", None)
            print("  %d. %s" % (idx, desc() if callable(desc) else repr(act)))

        if not confirm("Dispatch these actions now?"):
            print("Cancelled; no action executed.")
            return

        safe_runner = AgentRuntime(max_steps=self.agent.max_steps, max_retries=0)
        use_custom = bool(self.context.get("use_custom_schemes", False))

        def execute_one(action):
            return opener(getattr(action, "app", ""), getattr(action, "query", None), use_custom_schemes=use_custom)

        def verify_one(action, res):
            return isinstance(res, result_type) and getattr(res, "ok", False) is True

        res = safe_runner.run(actions, execute_one, verify_one)
        print("Execution Result:", res)
        self._record_history("agent " + request, res)

    def _record_history(self, request, result):
        for name in ("log", "add_history", "record"):
            method = getattr(self.memory, name, None)
            if callable(method):
                try:
                    method("agent_action", request, "ok" if getattr(result, "get", lambda k: False)("success") else "done")
                    return
                except Exception:
                    pass

    def handle_intent(self, text):
        low = text.lower()

        # Samanya Bhasha: YouTube auto-detection
        if any(w in low for w in ["youtube par video", "video upload", "youtube upload", "video daal", "youtube pe video"]):
            self.cmd_app_run("youtube")
            return

        # Samanya Bhasha: Tally auto-detection
        if any(w in low for w in ["tally me entry", "tally entry", "tally chalao", "tally par entry"]):
            self.cmd_app_run("tally")
            return

        # Samanya Bhasha: Excel auto-detection
        if any(w in low for w in ["excel sheet", "excel me", "excel banao", "excel entry"]):
            self.cmd_app_run("excel")
            return

        # Direct app actions check
        direct = getattr(executor, "is_direct_command", None)
        if callable(direct) and direct(text):
            actions = executor.plan_actions(text)
            for idx, act in enumerate(actions, 1):
                desc = getattr(act, "describe", None)
                print("  %d. %s" % (idx, desc() if callable(desc) else repr(act)))
            if confirm("Dispatch these now?"):
                executor.execute_plan(actions, log=print)
            else:
                print("Skipped.")
            return

        # Natural Conversational Chat
        if any(w in low for w in ["kese", "kaise", "haal", "namaste", "hello", "hi", "hey", "bhai", "kya", "batao", "shukriya", "dost"]):
            reply = conversational_chat(text, self.context)
            print("\nAutoMind:", reply)
            return

        # Silent AI API path: never launch a browser or external AI app.
        try:
            prompt_fn = getattr(ai_hub, "build_expert_prompt", None)
            complete_fn = getattr(ai_hub, "complete_silent", None)
            if callable(prompt_fn) and callable(complete_fn):
                prompt = prompt_fn(text, "general", self.context)
                result = complete_fn(prompt, preferred=os.environ.get("AUTOMIND_PROVIDER"))
                if result.get("ok"):
                    print("\n[AutoMind AI / %s]\n%s" % (result.get("provider"), result.get("text", "")))
                else:
                    print("[AI offline/unconfigured] %s" % result.get("error", "Unknown provider error"))
                    print("[Provider attempts] %s" % result.get("attempts", []))
                return
        except Exception as exc:
            print("AI Hub error:", type(exc).__name__ + ": " + str(exc))
        print("Request received:", text)

    def run(self):
        print("=== AutoMind v2.5.0 Universal Agent & Voice Chat ===")
        print("Type 'help' for commands. Bolkar command dene ke liye 'bolo' likhein.")
        print("Normal bhasha me seedhe Hindi/Hinglish me baat karein.")
        while self.running:
            try:
                raw = input("\nai> ")
                self.dispatch(raw)
            except (EOFError, KeyboardInterrupt):
                print("\nExiting AutoMind.")
                break
            except Exception as exc:
                print("Error:", exc)
                traceback.print_exc()


def board_run_plan(text, progress=None):
    """Board adapter: return a safe plan preview; never block on input()."""
    task = str(text or "").strip()
    if not task:
        return {"ok": False, "status": "rejected", "error": "Task is empty", "steps": []}
    steps = []
    source = "built-in"
    if agent_self_repair:
        try:
            learned = agent_self_repair.get_learned_task(task)
            if isinstance(learned, dict) and isinstance(learned.get("steps"), list):
                steps, source = list(learned["steps"]), "learned-memory"
        except Exception:
            pass
    if not steps and agent_workflows:
        try:
            key = agent_workflows.choose_workflow(task)
            wf = agent_workflows.WORKFLOWS.get(key)
            if wf:
                steps, source = list(wf.get("steps", [])), "workflow:" + str(key)
        except Exception:
            pass
    if not steps:
        steps = ["Clarify requirements for: " + task,
                 "Collect and validate required inputs",
                 "Prepare implementation plan for: " + task,
                 "Run checks and verify the result",
                 "Report outcome and remaining limitations"]
    if callable(progress):
        progress("Plan generated with %d steps; preview only, no actions executed." % len(steps))
    return {"ok": True, "status": "planned_only", "task": task, "source": source, "steps": steps,
            "execution_requires_explicit_approval": True}


def board_codegen(task, progress=None):
    """Generate code through silent configured providers; does not execute it."""
    task = str(task or "").strip()
    if not task:
        return {"ok": False, "error": "Coding task is empty"}
    prompt = ("You are a careful Python engineer. Generate a complete solution for this task. "
              "Return code first, then a short explanation and tests. Do not claim tests ran. "
              "Avoid destructive actions, shell execution, credential exposure, or automatic installation.\n\nTASK:\n" + task)
    result = ai_hub.complete_silent(prompt, preferred=os.environ.get("AUTOMIND_PROVIDER"))
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "attempts": result.get("attempts", [])}
    if callable(progress):
        progress("Code draft generated by %s; not executed or saved automatically." % result.get("provider"))
    return {"ok": True, "provider": result.get("provider"), "model": result.get("model"),
            "code_draft": result.get("text"), "executed": False, "saved": False}


def board_run_evolution(goal=None):
    """Explicit board evolution adapter; uses the evolver's transactional API."""
    try:
        from agent_evolver import Evolver
        engine = Evolver()
        if goal:
            return engine.evolve_text(str(goal))
        return engine.run_cycle()
    except Exception as exc:
        return {"ok": False, "status": "failed", "error": type(exc).__name__ + ": " + str(exc)}


def main():
    try:
        app = Orchestrator()
        app.run()
    except Exception as exc:
        print("AutoMind could not start:", exc)
        traceback.print_exc()


if __name__ == "__main__":
    main()
