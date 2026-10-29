import os
import json
from dotenv import load_dotenv

load_dotenv()
if os.path.exists("/etc/secrets/.env"):
    load_dotenv("/etc/secrets/.env")

from fastapi import FastAPI
from pydantic import BaseModel
import ai_hub

app = FastAPI()

class ChatRequest(BaseModel):
    message: str

SYSTEM_INSTRUCTION = """
You are AutoMind, an intelligent OS agent for an Android smartphone.
Analyze user request and respond STRICTLY in JSON format:

1. If the user wants to open an app (YouTube, WhatsApp, Chrome, Camera, Settings, etc.):
{"type": "action", "action": "open_app", "package": "com.google.android.youtube", "reply": "Opening YouTube"}

2. For general chat or questions:
{"type": "chat", "action": "none", "package": "", "reply": "Aapka normal jawab"}

Do not add markdown backticks. Return valid JSON only.
"""

@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    try:
        full_prompt = f"{SYSTEM_INSTRUCTION}\n\nUser command: {req.message}"
        result = ai_hub.complete_silent(full_prompt)
        
        if result.get("ok"):
            raw_text = result.get("text", "").strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text.replace("```json", "", 1).replace("```", "").strip()
            elif raw_text.startswith("```"):
                raw_text = raw_text.replace("```", "").strip()
                
            try:
                parsed = json.loads(raw_text)
                return parsed
            except Exception:
                return {"type": "chat", "action": "none", "package": "", "reply": raw_text}
        else:
            return {"type": "chat", "action": "none", "package": "", "reply": f"AI Error: {result.get('error')}"}
    except Exception as e:
        return {"type": "chat", "action": "none", "package": "", "reply": f"Server Error: {str(e)}"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
    
