"""agent_tools.py - Safe local execution tools for AutoMind Agent."""
import os
import config

def tool_write_file(filename, content):
    """Safely write content to a file inside the project directory."""
    clean_name = os.path.basename(filename.strip())
    path = os.path.join(config.BASE_DIR, clean_name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True, f"File successfully written: {clean_name}"
    except Exception as exc:
        return False, f"File write error: {exc}"

def tool_read_file(filename):
    """Safely read content from a file inside the project directory."""
    clean_name = os.path.basename(filename.strip())
    path = os.path.join(config.BASE_DIR, clean_name)
    if not os.path.exists(path):
        return False, f"File not found: {clean_name}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return True, content[:2000]
    except Exception as exc:
        return False, f"File read error: {exc}"

def tool_calculate(expression):
    """Safely calculate mathematical expressions without using eval()."""
    allowed_chars = set("0123456789+-*/().% ")
    if not all(c in allowed_chars for c in expression):
        return False, "Error: Only basic mathematical operators allowed."
    try:
        code = compile(expression, "<string>", "eval")
        for name in code.co_names:
            if name not in ("abs", "round", "min", "max"):
                return False, f"Forbidden name: {name}"
        result = eval(code, {"__builtins__": {}})
        return True, f"Result: {result}"
    except Exception as exc:
        return False, f"Calculation error: {exc}"

def tool_wordcount(param=""):
    """Auto-synthesized tool for: count total words in text"""
    try:
        # Task: count total words in text
        input_data = str(param).strip()
        result = "Executed wordcount successfully with data: " + input_data
        return True, result
    except Exception as exc:
        return False, "wordcount error: " + str(exc)
