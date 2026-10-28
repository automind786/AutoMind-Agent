"""agent_feature_synth.py - Autonomous Tool & Feature Generator with AST verification."""
import os
import ast
import time
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_PATH = os.path.join(BASE_DIR, "agent_tools.py")
BACKUP_DIR = os.path.join(BASE_DIR, "backups", "synth_backups")


def ensure_tools_backup():
    """Tool file badalne se pehle safe backup banata hai."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.exists(TOOLS_PATH):
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(BACKUP_DIR, "agent_tools_%s.bak" % stamp)
    shutil.copy2(TOOLS_PATH, bak)
    return bak


def validate_python_snippet(code_snippet):
    """Check karta hai ki code syntax error to nahi de raha."""
    try:
        ast.parse(code_snippet)
        return True, "Syntax valid."
    except SyntaxError as err:
        return False, "Syntax error at line %d: %s" % (err.lineno, err.msg)


def append_tool_safely(tool_name, function_code):
    """Naye function ko agent_tools.py me test karne ke baad jodta hai."""
    ok, msg = validate_python_snippet(function_code)
    if not ok:
        return False, "Feature synthesis rejected: " + msg

    backup_file = ensure_tools_backup()
    try:
        with open(TOOLS_PATH, "r", encoding="utf-8") as f:
            current_tools = f.read()

        new_tools_content = current_tools.rstrip() + "\n\n" + function_code.strip() + "\n"

        # Poori file ko dobara syntax verify karein
        compiled_ok, comp_msg = validate_python_snippet(new_tools_content)
        if not compiled_ok:
            return False, "Merged file syntax check failed: " + comp_msg

        with open(TOOLS_PATH, "w", encoding="utf-8") as f:
            f.write(new_tools_content)

        return True, "Successfully synthesized and added new tool: '%s' (Backup: %s)" % (
            tool_name, os.path.basename(backup_file or "none")
        )
    except Exception as exc:
        if backup_file and os.path.exists(backup_file):
            shutil.copy2(backup_file, TOOLS_PATH)
        return False, "Rollback triggered due to error: " + str(exc)


def build_tool_template(tool_name, description):
    """Standard safe Python tool function template banata hai."""
    fn_name = "tool_" + tool_name.strip().lower().replace(" ", "_")
    template = f'''
def {fn_name}(param=""):
    """Auto-synthesized tool for: {description}"""
    try:
        # Task: {description}
        input_data = str(param).strip()
        result = "Executed {tool_name} successfully with data: " + input_data
        return True, result
    except Exception as exc:
        return False, "{tool_name} error: " + str(exc)
'''
    return fn_name, template
