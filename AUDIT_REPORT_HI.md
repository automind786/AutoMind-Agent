# AutoMind v3.0 Core Audit — Hindi/Hinglish

## Scope
Audited Python syntax, unit/regression tests, offline smoke tests, math self-test, evolver self-test, diagnostics, board SPECS resolution, browser-opening references, and silent-provider fallback behavior.

## Fixes applied
- Removed `webbrowser.open()` fallback from both executor implementations.
- Disabled automatic browser opening when starting the Web Canvas UI; it prints a localhost URL for manual opening.
- Changed YouTube Studio upload option to print the URL rather than launch a browser.
- Added `ai_hub.complete_silent()` with urllib-based API calls and configured-key fallback for Gemini, Anthropic Claude, Groq, and OpenRouter.
- Routed normal non-command requests in `main.py` through silent AI calls instead of launching Gemini.
- Added board adapter entry points for preview-only task plans, code drafting (not executed/saved automatically), and Evolver execution.
- Corrected SPECS candidates to match actual `main.py`, `agent_evolver.py`, and `agent_math_core.py` symbols.
- Added tests for silent fallback, no-key behavior, board entry resolution, and preview-only planning.

## Important limitations / release notes
- Live network calls and actual API credentials were not used in the audit; provider response parsing was checked by mock tests only.
- Android/Pydroid Kivy UI, microphone, native app intents, and real external account actions were not exercised.
- `agent_evolver.CodePolicy` is a static guardrail, not a sandbox. Generated code still runs in-process when imported. Do not expose the evolution API to untrusted callers.
- Explicit Android VIEW intents can still open another app if the user triggers a direct app action; automatic browser popups and Python `webbrowser.open()` calls have been removed.
- The board's codegen adapter returns a draft only; it deliberately does not execute or save model-generated code.
- Provider keys should be set in environment variables: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`. Optional model overrides: `GEMINI_MODEL`, `CLAUDE_MODEL`, `GROQ_MODEL`, `OPENROUTER_MODEL`. Optional preferred provider: `AUTOMIND_PROVIDER`.
