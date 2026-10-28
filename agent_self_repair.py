"""agent_self_repair.py - Safe Syntax Inspection, Rollback and Adaptive Knowledge Updater."""
import os
import ast
import json
import time
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KB_PATH = os.path.join(BASE_DIR, "knowledge_base.json")
BACKUP_DIR = os.path.join(BASE_DIR, "backups", "self_repair_backups")


def ensure_backup(file_name):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    src = os.path.join(BASE_DIR, file_name)
    if not os.path.exists(src):
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(BACKUP_DIR, "%s_%s.bak" % (file_name, stamp))
    shutil.copy2(src, backup_file)
    return backup_file


def validate_python_code(code_string):
    try:
        ast.parse(code_string)
        return True, "Syntax is valid."
    except SyntaxError as err:
        return False, "Syntax error at line %d: %s" % (err.lineno, err.msg)


def safe_apply_fix(file_name, new_code):
    ok, msg = validate_python_code(new_code)
    if not ok:
        return False, "Self-repair rejected changes: %s" % msg

    backup_path = ensure_backup(file_name)
    target_path = os.path.join(BASE_DIR, file_name)

    try:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(new_code)
        with open(target_path, "r", encoding="utf-8") as f:
            compile(f.read(), target_path, "exec")
        return True, "Successfully repaired and updated %s (Backup: %s)" % (file_name, os.path.basename(backup_path or ""))
    except Exception as exc:
        if backup_path and os.path.exists(backup_path):
            shutil.copy2(backup_path, target_path)
        return False, "Error during update. Rolled back to previous safe state: %s" % exc


def update_agent_knowledge(task_or_query, solution_steps):
    try:
        data = {}
        if os.path.exists(KB_PATH):
            with open(KB_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

        if "learned_tasks" not in data:
            data["learned_tasks"] = {}

        data["learned_tasks"][task_or_query.strip().lower()] = {
            "steps": solution_steps,
            "updated_at": time.ctime()
        }

        with open(KB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True, "Agent memory successfully updated for task: '%s'" % task_or_query
    except Exception as exc:
        return False, "Knowledge update failed: %s" % exc


def get_learned_task(task_or_query):
    try:
        if not os.path.exists(KB_PATH):
            return None
        with open(KB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        learned = data.get("learned_tasks", {})
        return learned.get(task_or_query.strip().lower())
    except Exception:
        return None
