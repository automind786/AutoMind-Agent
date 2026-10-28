# AutoMind Ultimate 2.5.0 — Pydroid 3 personal assistant

**Important:** this is a local Python helper/orchestrator, not a fully autonomous AI agent. It does not call model APIs, read AI responses, or perform background tasks. You open an AI service, use the prompt, and manually save a conclusion.

Talks to the AI apps already on your phone (Gemini, ChatGPT, Claude, Copilot)
or their web interfaces (incl. Groq). **No API keys, tokens or REST endpoints
anywhere.** Apps are opened using a safe Android VIEW intent attempt (argument-list subprocess, no shell string), optional Android bridge, then Python `webbrowser`. A manual URL fallback is shown if all launch methods fail. Android launch success still depends on the installed Pydroid environment and URL handler.

## Setup (Pydroid 3)
1. Copy `automind.zip` to the phone and extract it (Pydroid file
   manager, or run:
   `import zipfile; zipfile.ZipFile('/storage/emulated/0/Download/automind.zip').extractall('/storage/emulated/0/')`).
2. Open `main.py` in Pydroid 3 and press Run. Python 3.8+; standard library only.
3. Optional: install `pyperclip` in Pydroid's Pip screen if clipboard copy is unavailable.
4. Use the Run button in Pydroid 3. Keep the project folder writable so notes can be saved.
5. Type `doctor`, `test`, or `selfrepair full` at the `ai>` prompt to run local diagnostics. Use one command per prompt; do not paste several commands into confirmation questions. It does not launch apps or use the internet.
6. Approval prompts accept `y`/`yes`, `n`/`no`, or Enter for the shown default. Invalid input is treated as NO and will not repeat the same prompt indefinitely.

## Files
| File | Role |
|---|---|
| `config.py` | AI targets, app deep-link registry, preferences, paths |
| `smart_update.json` | Safe editable settings file; edit values and restart (never executes code) |
| `ai_hub.py` | Domain detection, structured prompt builder, clipboard + launch |
| `executor.py` | Safe app dispatcher, intent -> action planner, clipboard bridge |
| `memory_manager.py` | `knowledge_base.json` notes, fuzzy lookup, profile, history, prefs |
| `plugin_manager.py` | importlib hot loader (new/changed/deleted plugins, live) |
| `smart_updater.py` | `update` engine: paste / file / URL / inbox / remove |
| `plugins/prompt_pack.py` | Sample hot-plug skill (`prompt stock Reliance outlook`) |
| `main.py` | Two-step console |

## Flow
**Step 1** - memory lookup (fuzzy, so rephrased topics match). On a miss:
pick an AI -> structured expert prompt is built, copied to clipboard and the
app/web is launched -> you paste the answer's conclusion, which is saved.
**Step 2** - the planned native actions are listed; nothing opens until you
answer `y`.

## Honest limits
- Gemini and Groq links cannot carry a prompt: paste from the clipboard.
  ChatGPT, Claude and Copilot links carry the prompt (`?q=`).
- Clipboard access on Pydroid depends on what is available. If none works the
  prompt is shown on screen and saved to `last_prompt.txt`.
- Because there is no API, the program cannot read the AI's answer; that is
  why you record the conclusion yourself.
- Default routes are https App Links (Android may open the native app if installed,
  otherwise the browser). If Pydroid has no Android bridge or URL handler, launching
  can still fail; the error is shown and the URL can be opened manually. `chatgpt://chat?q=` and the Instagram/X schemes are tried first
  with `set use_custom_schemes true`. If a route does nothing, answer `n` at
  "Did it open correctly?" to try the next route.
- `https://groq.com` is the Groq entry point you specified; edit `config.py`
  if you prefer a direct chat URL.

## Persistent profile and offline use
- `remember language=Hindi` saves a key/value fact for future sessions.
- `profile setup` asks a few optional questions and saves answers locally.
- `profile` shows saved profile information; `forgetinfo language` removes one item.
- `offline on` disables the online-AI research launch path. Notes, saved profile,
  fuzzy memory lookup, task planning and local plugins still work without internet.
  Opening websites and asking a cloud AI still needs internet; no offline program can
  produce fresh cloud-AI answers while disconnected.
- Data is stored in `knowledge_base.json` beside the program. Keep the folder writable
  and back up this file before reinstalling or moving the project.

## Smart settings file
Edit `smart_update.json` with a text editor to change safe settings such as offline mode,
default AI, answer language and memory retention. Restart AutoMind to apply changes.
Only known settings are loaded; the JSON file is configuration, not executable code.
For adding actual features, use the `update` plugin manager below. Plugins can run Python
code with the app's permissions, so install only code you trust.

## Hot updates
`update` -> paste code, file path, URL, or inbox. Or one-step:
`update /storage/emulated/0/Download/my_skill.py`. Dropping a `.py` into
`plugins/` also loads it at the next prompt. Plugin contract:
```python
PLUGIN_INFO = {"name": "hello", "version": "1.0.0",
               "description": "Says hello", "commands": ["hello"]}
def run(args, context):   # context: memory, hub, executor, config
    return "Hello " + args
```
Plugins execute with full app permissions: install only code you trust. Bad
installs roll back automatically; replaced files are kept in `plugins/_backup`.

## Best platform
- **Android phone:** Pydroid 3 is the simplest match for this console-based project.
- **PC/laptop:** Python 3.10+ in VS Code or a terminal is better for debugging and logs.
- **Google Colab:** useful for testing/building the ZIP, but not ideal for persistent,
  always-on use because sessions can stop.
- This ZIP does **not** build an APK and is not an Android Studio/AIDE project.


## Ultimate final update: diagnostics and backup
- `doctor` checks required local files, readable memory JSON, and whether the project folder is writable. It does not test real AI/browser services.
- `backup` copies `knowledge_base.json`, `smart_update.json`, and `auto_update_policy.json` (when present) into a timestamped `backups/` folder. It does not overwrite the original data.
- Continue to keep a separate copy of `knowledge_base.json` before replacing project files. Extract this ZIP into a new folder first.


## AutoMind 2.3.0 update notes (Hindi/Hinglish)

### Pydroid 3 mein chalane ka tarika
1. Purane `automind` folder ka copy/ZIP backup bana lein. `knowledge_base.json`, `smart_update.json` aur `last_prompt.txt` ko khaas taur par bachayein.
2. Naya ZIP extract karein. `automind` folder ke andar ka `main.py` Pydroid 3 mein kholein.
3. Pydroid ko storage permission dein agar woh maange. Project folder writable hona chahiye.
4. Run karein; `ai>` prompt par `help`, `doctor`, aur `test` chalayein.
5. Gemini manually kholne ke liye URL `https://gemini.google.com/` browser mein paste kiya ja sakta hai. App/browser launch success device aur Pydroid integration par depend karta hai. AutoMind ab generic `android` module ko universal API samajhkar call nahi karta.
6. Agar launch fail ho, screen par dikhaya URL khud browser mein kholein. Prompt `last_prompt.txt` mein save hota hai jab prompt-save step chalta hai. Clipboard optional hai.

### Backup aur recovery
- Update se pehle poore purane folder ka backup lein.
- App ka `backup` command project ke Python/JSON files ki timestamped copy banata hai.
- Memory JSON corrupt milne par loader original ko `.corrupt` naam se preserve karke fresh database banane ki koshish karta hai. `.corrupt` file ko delete na karein; manual recovery ke liye use karein.
- Recovery: AutoMind band karein, current `knowledge_base.json` ko alag naam dein, phir backup se us file ko restore karein. Restore se pehle current file ki copy zaroor rakhein.
- `selfrepair full` backup aur syntax checks karta hai; yeh har bug ko automatically repair nahi karta.

### Dependencies / online status
Python standard library is sufficient for core local commands. `pyperclip` is optional only; it may not work on every Android/Pydroid setup. Browser launch and AI websites require internet. No AI provider API is called by this zero-key build, so it does not receive AI answers automatically. Offline mode prevents no network unless the selected workflow respects that setting; do not use online launch actions when you need strict offline operation.

See `CHANGELOG.md` and `TEST_REPORT.md` for the verified changes and test scope.


## Version 2.4.0 final bundle
- Run `selfrepair check` to scan Python source syntax without changing source files.
- Run `selfrepair backup` to create a timestamped backup of local settings/memory.
- Run `selfrepair full` to back up data and run syntax validation. It is conservative diagnostics, not a promise to automatically fix arbitrary bugs.
- Run `doctor`, `test`, and `backup` before moving or updating the project.
- Android launch failure is reported honestly and a manual HTTPS URL is shown. Gemini web handoff does not automatically retrieve the AI's answer.
- See `CHANGELOG.md`, `TEST_REPORT.md`, and `INSTALL_HINDI.txt`.

## New safe task workflows (2.5.0)
- `plan create a Gmail account` — local official signup checklist.
- `plan create a YouTube channel` — setup checklist.
- `plan make and upload a video` — script-to-publish workflow.
- `plan invest in a stock` — educational research checklist; never places trades.
- `investigate <question>` — saves/displays a structured prompt; it does not claim live research.
- `vision` — displays the detailed Hindi/Hinglish master agent prompt in `AGENT_VISION_PROMPT_HI.md`.

AutoMind does not autonomously create accounts, bypass verification, render/upload videos or trade investments. These require user-controlled official interfaces and, where appropriate, separately implemented authorized integrations. See `TEST_REPORT.md` for what has and has not been verified.
