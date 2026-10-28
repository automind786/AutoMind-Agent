"""
main_bridge.py - AutoMind Hybrid Execution Bridge
Handles lightweight local command routing and prepares actions
for native Android UI or serverless execution.
"""
import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Lazy loading to keep RAM usage minimal
_orchestrator = None

def get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        try:
            from main import Orchestrator
            _orchestrator = Orchestrator()
        except Exception as e:
            return None, str(e)
    return _orchestrator, None

def execute_request(json_payload):
    """
    Main communication port: Accepts JSON string and returns JSON string.
    Works natively inside Android APK or via lightweight HTTP webhook.
    """
    try:
        if isinstance(json_payload, str):
            request = json.loads(json_payload)
        else:
            request = json_payload
    except Exception as err:
        return json.dumps({
            "status": "error",
            "message": f"Invalid JSON input: {err}",
            "action_required": None
        })

    user_text = request.get("command", "").strip()
    session_id = request.get("session_id", "default_session")

    if not user_text:
        return json.dumps({
            "status": "empty",
            "reply": "Koi command prapt nahi hua.",
            "action_required": None
        })

    orch, err = get_orchestrator()
    if err:
        return json.dumps({
            "status": "error",
            "reply": f"AutoMind core load nahi ho saka: {err}",
            "action_required": None
        })

    # Command Classification (Local Intent Mapping)
    cmd_lower = user_text.lower()
    
    # 1. YouTube Action Check
    if any(k in cmd_lower for k in ["youtube", "yt", "video upload"]):
        return json.dumps({
            "status": "action_pending",
            "action_type": "YOUTUBE_TASK",
            "reply": "YouTube action trigger ho gaya hai. Video aur metadata ready karein.",
            "raw_command": user_text
        })

    # 2. Accounting / App Hub Check
    if any(k in cmd_lower for k in ["tally", "excel", "ledger", "hisab"]):
        return json.dumps({
            "status": "action_pending",
            "action_type": "APP_HUB_TASK",
            "reply": "Accounting Hub trigger hua hai.",
            "raw_command": user_text
        })

    # 3. Conversational / General Query
    try:
        from main import conversational_chat
        reply = conversational_chat(user_text, orch.context)
    except Exception:
        reply = f"AutoMind ne suna: '{user_text}'"

    return json.dumps({
        "status": "success",
        "reply": reply,
        "action_required": None
    })

if __name__ == "__main__":
    # Test run
    test_input = json.dumps({"command": "AutoMind namaste"})
    print("Testing Bridge Response:")
    print(execute_request(test_input))
