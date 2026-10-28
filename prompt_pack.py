PLUGIN_INFO = {
    "name": "prompt_pack",
    "version": "1.0.0",
    "description": "Generate ready-to-paste expert prompts: prompt <stock|crypto|tech|general> <topic>",
    "commands": ["prompt"],
}


def run(args, context):
    parts = args.split(None, 1)
    domains = ("stocks", "crypto", "tech", "general")
    if len(parts) < 2:
        return "Usage: prompt <stock|crypto|tech|general> <topic>\nExample: prompt stock Reliance quarterly outlook"
    domain = parts[0].lower()
    domain = {"stock": "stocks"}.get(domain, domain)
    if domain not in domains:
        return "Domain must be one of: stock, crypto, tech, general"
    hub, executor = context["hub"], context["executor"]
    text = hub.build_expert_prompt(parts[1], domain, context["memory"].db["preferences"])
    method = executor.copy_to_clipboard(text)
    note = "(copied to clipboard via %s)" % method if method else "(clipboard unavailable - select and copy manually)"
    return text + "\n\n" + note
