"""Safe local task planner for common personal-agent workflows.

Plans guide the user through official interfaces; this module does not create
accounts, upload videos, or place trades autonomously.
"""
import re

WORKFLOWS = {
    "gmail": {"title": "Create a Gmail account", "steps": [
        "Open the official Google Account signup page: https://accounts.google.com/signup",
        "Enter your real details and choose an available username.",
        "Use a strong unique password; never share it with AutoMind or an AI chat.",
        "Complete Google's verification and recovery setup yourself.",
        "Review the account security settings and save recovery information safely."],
        "safety": "Account creation requires your personal details and verification; AutoMind cannot bypass these."},
    "youtube": {"title": "Create and set up a YouTube channel", "steps": [
        "Sign in to YouTube with your Google account: https://www.youtube.com/",
        "Open profile menu → Settings → Add or manage channel(s) / Create a channel.",
        "Choose channel name, handle, profile image and description.",
        "Enable account security and review YouTube's current creator policies.",
        "Prepare a content calendar, thumbnail style, title, description and audience plan."],
        "safety": "Channel creation and identity/verification steps require your review and account access."},
    "video": {"title": "Plan, create and publish a video", "steps": [
        "Define the audience, topic, target duration and one clear viewer benefit.",
        "Draft a script with hook, main points, examples and call to action.",
        "Create or record visuals/audio you own or are licensed to use.",
        "Edit, caption and export in a format supported by the target platform.",
        "Review copyright, privacy, accuracy, thumbnail, title and description.",
        "Upload through YouTube Studio: https://studio.youtube.com/; preview visibility and audience settings.",
        "Publish only after you explicitly approve the final video and settings."],
        "safety": "AutoMind can help plan and draft, but does not claim to render/upload media unless a real tool is integrated."},
    "invest": {"title": "Investment research workflow (not automatic trading)", "steps": [
        "State your goal, time horizon, emergency-fund status and risk tolerance; do not share account credentials.",
        "Research the instrument using official exchange, fund, company and regulator sources.",
        "Compare fees, liquidity, diversification, valuation, downside scenarios and tax implications.",
        "Check whether the source data is current and record its date; identify what remains uncertain.",
        "Consider position sizing and the possibility of losing money; avoid leverage unless you fully understand it.",
        "Make the final decision yourself or consult a qualified, SEBI-registered adviser where appropriate."],
        "safety": "This is educational research only. AutoMind does not log in, move money, or place trades."},
    "research": {"title": "Investigate and research a question", "steps": [
        "Convert the request into a precise question and list assumptions.",
        "Search for current primary sources first; record publication dates and links.",
        "Cross-check important claims against at least two independent reliable sources when possible.",
        "Separate verified facts, estimates, opinions and unknowns.",
        "Summarize findings, limitations, practical next steps and open questions.",
        "Save only a conclusion you have reviewed and approved."],
        "safety": "If no live search/API is connected, AutoMind can prepare a prompt but cannot claim it performed live research."},
}

def choose_workflow(text):
    t = (text or "").lower()
    if any(x in t for x in ("gmail", "email account", "google account")): return "gmail"
    if any(x in t for x in ("youtube channel", "channel create", "channel banana")): return "youtube"
    if any(x in t for x in ("video", "upload", "youtube par dal", "video banana")): return "video"
    if any(x in t for x in ("invest", "stock", "share market", "trading", "sip", "mutual fund")): return "invest"
    return "research"

def render(key, request=""):
    item = WORKFLOWS[key]
    out = ["WORKFLOW: " + item["title"], "Request: " + (request or item["title"]), "", "Steps:"]
    out.extend("%d. %s" % (i, step) for i, step in enumerate(item["steps"], 1))
    out += ["", "Safety / capability boundary: " + item["safety"],
            "Status: PLAN ONLY — no account, upload, financial transaction or external action was performed."]
    return "\n".join(out)
