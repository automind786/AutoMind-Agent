"""agent_app_hub.py - Universal Application Discovery, Staging & YouTube Dispatcher."""
import os
import json
import time
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APPS_REGISTRY_PATH = os.path.join(BASE_DIR, "apps_registry.json")
SESSIONS_DIR = os.path.join(BASE_DIR, "app_sessions")


def load_registry():
    if not os.path.exists(APPS_REGISTRY_PATH):
        return {}
    try:
        with open(APPS_REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_registry(data):
    try:
        with open(APPS_REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def discover_and_register_app(app_name):
    app_key = app_name.strip().lower()
    registry = load_registry()

    blueprints = {
        "youtube": {
            "name": "YouTube Studio",
            "mode": "online",
            "protocol": "API_OR_BROWSER",
            "inputs_required": ["video_path", "title", "description", "tags", "privacy"],
            "description": "Video metadata structuring, studio staging and API upload."
        },
        "tally": {
            "name": "Tally Prime / ERP 9",
            "mode": "hybrid",
            "protocol": "XML_GATEWAY",
            "inputs_required": ["company_name", "voucher_type", "party_ledger", "amount", "narration"],
            "description": "Direct XML/ODBC batch transaction injection into Tally."
        },
        "excel": {
            "name": "MS Excel / Spreadsheet",
            "mode": "offline",
            "protocol": "FILE_OPENPYXL",
            "inputs_required": ["filename", "sheet_name", "headers", "rows_data"],
            "description": "Programmatic Excel workbook generation."
        }
    }

    profile = blueprints.get(app_key, {
        "name": app_name.capitalize(),
        "mode": "hybrid",
        "protocol": "GENERIC_ACTION_LOOP",
        "inputs_required": ["target_action", "payload_data"],
        "description": "Generic execution pipeline for " + app_name
    })

    profile["registered_at"] = time.ctime()
    registry[app_key] = profile
    save_registry(registry)
    return True, profile


def execute_app_interactive_session(app_key, ask_fn, print_fn):
    registry = load_registry()
    app_key = app_key.strip().lower()

    if app_key not in registry:
        discover_and_register_app(app_key)
        registry = load_registry()

    profile = registry.get(app_key, {})
    print_fn("\n--- STARTING SESSION: %s ---" % profile.get("name", app_key))
    print_fn("Jankari ek-ek karke enter karein:\n")

    collected_data = {}
    for req in profile.get("inputs_required", []):
        prompt_label = req.replace("_", " ").title()
        val = ask_fn("Enter %s: " % prompt_label)
        collected_data[req] = val if val else "N/A"

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    out_file = os.path.join(SESSIONS_DIR, "%s_action_%s.json" % (app_key, time.strftime("%Y%m%d_%H%M%S")))
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"profile": profile, "inputs": collected_data, "time": time.ctime()}, f, indent=2)

    print_fn("\n[Action Blueprint Ready & Saved]: " + os.path.basename(out_file))

    # YouTube Specific Dispatch Flow
    if app_key == "youtube":
        print_fn("\nUpload karne ke do vikalp hain:")
        print_fn("  1. Direct YouTube Studio Upload page browser me kholein")
        print_fn("  2. Background me direct API script generate karein")
        choice = ask_fn("Aap kya karna chahte hain? (1 ya 2): ")

        if choice == "1":
            print_fn("[NO-POPUP] YouTube Studio URL (manually open if desired): "
                     "https://studio.youtube.com/channel/videos/upload")
        elif choice == "2":
            script_path = os.path.join(BASE_DIR, "youtube_uploader.py")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write('''# AutoMind Background YouTube API Uploader
# Zaroorat: pip install google-api-python-client google-auth-oauthlib
import json

with open("''' + out_file + '''", "r") as f:
    payload = json.load(f)["inputs"]

print("Uploading to YouTube:", payload.get("title"))
print("File:", payload.get("video_path"))
print("Status: Client secrets file verify karke direct upload call initiate karein.")
''')
            print_fn("[OK] Background uploader script ban gayi: youtube_uploader.py")

    return True, "Session successfully processed."

# Auto-registered local backup tool
def _run_backup(prompt_fn=None, print_fn=print):
    import tool_backup
    res = tool_backup.create_project_backup()
    if callable(print_fn):
        print_fn(res)
    return True, res

try:
    if "backup" not in REGISTRY:
        REGISTRY["backup"] = {
            "name": "AutoMind Local Backup",
            "handler": _run_backup
        }
except Exception:
    pass
