# Changelog

## AutoMind Ultimate 2.5.0 — Agent workflow and Android launch improvements
- Added a second Android Activity Manager launch attempt with explicit Android user 0 and BROWSABLE category. All calls remain argument-list subprocess calls; no shell execution.
- Updated Gemini browser fallback to the canonical `https://gemini.google.com/` entry point.
- Added `plan <task>` for local, offline, step-by-step Gmail, YouTube channel, video production/upload and investment research workflows.
- Added `investigate <question>` to prepare a structured research prompt and save it locally without falsely claiming live research.
- Added `vision` command and `AGENT_VISION_PROMPT_HI.md`, a detailed Hindi/Hinglish master prompt with transparent capability boundaries.
- Updated help text and selfrepair usage.
- Explicitly states that account creation, video publishing and trades need official interfaces, authorization and user approval; investment workflow is educational only.
- This patch does not add actual API-based AI answers, autonomous Gmail creation, video rendering/upload, or broker trading. These need separate supported integrations and real-device tests.
