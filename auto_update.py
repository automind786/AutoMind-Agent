"""AutoMind safe update policy: checks only; never executes downloaded code.
Online manifests are informational until a trusted release-signing key is configured.
"""
import json, os, urllib.request, urllib.error, hashlib, shutil, time, tempfile

DEFAULT_POLICY = {
    "auto_safe_settings": True,
    "online_check_enabled": True,
    "manifest_url": "",
    "last_check_utc": "",
    "last_result": "Not checked",
    "require_approval_for_code": True,
    "require_approval_for_permissions": True,
    "backup_before_change": True,
}
SAFE_SETTINGS = {
    "default_ai": {"gemini", "chatgpt", "claude", "copilot", "groq"},
    "response_language": None,
    "currency": None,
    "offline_mode": {True, False},
    "auto_remember": {True, False},
    "cache_ttl_hours": None,
    "similarity_threshold": None,
    "use_custom_schemes": {True, False},
}

class UpdatePolicyError(ValueError): pass

def validate_safe_settings(changes):
    if not isinstance(changes, dict): raise UpdatePolicyError("Settings must be a JSON object")
    out = {}
    for key, value in changes.items():
        if key not in SAFE_SETTINGS: raise UpdatePolicyError("Setting not auto-updatable: " + str(key))
        allowed = SAFE_SETTINGS[key]
        if allowed is not None and value not in allowed: raise UpdatePolicyError("Invalid value for " + key)
        if key == "response_language" and (not isinstance(value, str) or not value.strip() or len(value)>40): raise UpdatePolicyError("Invalid response_language")
        if key == "currency" and (not isinstance(value, str) or len(value)!=3 or not value.isalpha()): raise UpdatePolicyError("Invalid currency")
        if key == "cache_ttl_hours" and (not isinstance(value, int) or isinstance(value,bool) or not 1 <= value <= 8760): raise UpdatePolicyError("cache_ttl_hours must be 1..8760")
        if key == "similarity_threshold" and (not isinstance(value,(int,float)) or isinstance(value,bool) or not 0 <= value <= 1): raise UpdatePolicyError("similarity_threshold must be 0..1")
        out[key] = value
    return out

def apply_safe_settings(settings_path, changes):
    """Atomic settings-only update with backup. Does not touch memory or code."""
    safe = validate_safe_settings(changes)
    old = {}
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as f: old = json.load(f)
        if not isinstance(old, dict): raise UpdatePolicyError("Existing settings file is invalid")
    merged = dict(old); merged.update(safe)
    folder = os.path.dirname(os.path.abspath(settings_path))
    os.makedirs(folder, exist_ok=True)
    backup = None
    if os.path.exists(settings_path):
        backup = settings_path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(settings_path, backup)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(merged,f,ensure_ascii=False,indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
        with open(tmp,encoding="utf-8") as f: json.load(f)
        os.replace(tmp, settings_path)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        raise
    return {"updated": list(safe), "backup": backup}

def check_manifest(url, timeout=8, max_bytes=65536):
    """Fetch an HTTPS JSON manifest for information only; never downloads or runs payloads."""
    if not isinstance(url,str) or not url.startswith("https://"): raise UpdatePolicyError("Manifest URL must use HTTPS")
    req=urllib.request.Request(url,headers={"User-Agent":"AutoMind-update-check/1"})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        raw=resp.read(max_bytes+1)
    if len(raw)>max_bytes: raise UpdatePolicyError("Manifest too large")
    data=json.loads(raw.decode("utf-8"))
    if not isinstance(data,dict) or not isinstance(data.get("version"),str): raise UpdatePolicyError("Manifest needs a version field")
    return {"version":data["version"],"notes":str(data.get("notes",""))[:1000],"sha256":str(data.get("sha256","")),"installable":False,"message":"Update metadata only. Code is not downloaded or installed because a trusted release-signature verifier is not configured."}

def status(policy_path):
    data=dict(DEFAULT_POLICY)
    if os.path.exists(policy_path):
        with open(policy_path,encoding="utf-8") as f: saved=json.load(f)
        if isinstance(saved,dict): data.update({k:v for k,v in saved.items() if k in DEFAULT_POLICY})
    return data
