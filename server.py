import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import ai_hub

app = FastAPI()

class ChatRequest(BaseModel):
    message: str

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AutoMind AI Agent</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: sans-serif; background: #0f172a; color: #fff; margin: 0; padding: 20px; display: flex; flex-direction: column; height: 90vh; }
            #chat { flex: 1; overflow-y: auto; padding: 10px; border: 1px solid #334155; border-radius: 8px; margin-bottom: 10px; background: #1e293b; }
            .msg { margin: 8px 0; padding: 8px 12px; border-radius: 6px; max-width: 80%; }
            .user { background: #2563eb; align-self: flex-end; margin-left: auto; }
            .bot { background: #334155; align-self: flex-start; }
            .input-box { display: flex; gap: 8px; }
            input { flex: 1; padding: 12px; border-radius: 6px; border: none; background: #1e293b; color: white; outline: none; }
            button { padding: 12px 20px; border-radius: 6px; border: none; background: #2563eb; color: white; font-weight: bold; cursor: pointer; }
        </style>
    </head>
    <body>
        <h2>⚡ AutoMind Agent Live</h2>
        <div id="chat"></div>
        <div class="input-box">
            <input type="text" id="userInput" placeholder="Command ya chat likhein..." onkeydown="if(event.key==='Enter') send()">
            <button onclick="send()">Send</button>
        </div>
        <script>
            async function send() {
                const inp = document.getElementById('userInput');
                const text = inp.value.trim();
                if (!text) return;
                const chat = document.getElementById('chat');
                chat.innerHTML += `<div class='msg user'>${text}</div>`;
                inp.value = '';
                try {
                    const res = await fetch('/chat', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({message: text})
                    });
                    const data = await res.json();
                    chat.innerHTML += `<div class='msg bot'>${data.response}</div>`;
                } catch(e) {
                    chat.innerHTML += `<div class='msg bot'>Error: Server response nahi de raha.</div>`;
                }
                chat.scrollTop = chat.scrollHeight;
            }
        </script>
    </body>
    </html>
    """

@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    try:
        # ai_hub se response generate karein
        reply = ai_hub.chat_response(req.message) if hasattr(ai_hub, "chat_response") else f"Received: {req.message}"
        return {"response": str(reply)}
    except Exception as e:
        return {"response": f"Agent Error: {str(e)}"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run("server.py:app", host="0.0.0.0", port=port)
