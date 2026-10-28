# AutoMind — API Keys Setup (Hindi)

AutoMind external AI API calls `urllib` se background mein karta hai; API key hard-code karke source code mein rakhna zaroori nahi. Pehle environment variables use karein. Pydroid 3 mein aasaan local setup ke liye project folder mein `secrets_local.py` bana sakte hain. Ye file `.gitignore` mein hai aur GitHub par commit nahi honi chahiye.

Example `secrets_local.py` (sirf apne phone par banayein):

```python
GEMINI_API_KEY = "paste-your-key-here"
ANTHROPIC_API_KEY = ""
GROQ_API_KEY = ""
OPENROUTER_API_KEY = ""

# Optional model overrides
GEMINI_MODEL = "gemini-2.5-flash"
CLAUDE_MODEL = "claude-3-7-sonnet-latest"
GROQ_MODEL = "llama-3.3-70b-versatile"
OPENROUTER_MODEL = "openai/gpt-4o-mini"
```

`AUTOMIND_PROVIDER` se preferred provider `gemini`, `claude`, `groq`, ya `openrouter` set kar sakte hain. Agar preferred provider fail ho, AutoMind configured key wale agle provider ko try karega. Model names/provider availability badal sakti hai; live API call ke liye internet, valid key, provider quota aur current model access zaroori hain.

**GitHub se pehle:** `secrets_local.py`, logs, local databases, account tokens, generated private data commit na karein. `secrets_local.py` ko Git status mein verify karein.
