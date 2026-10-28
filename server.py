
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
            #chat { flex: 1; overflow-y: auto; padding: 12px; border: 1px solid #334155; border-radius: 8px; margin-bottom: 12px; background: #1e293b; display: flex; flex-direction: column; gap: 10px; }
            .msg { padding: 10px 14px; border-radius: 8px; max-width: 80%; line-height: 1.5; font-size: 15px; }
            .user { background: #2563eb; align-self: flex-end; }
            .bot { background: #334155; align-self: flex-start; white-space: pre-wrap; }
            .input-box { display: flex; gap: 8px; }
            input { flex: 1; padding: 14px; border-radius: 8px; border: none; background: #1e293b; color: white; outline: none; font-size: 15px; }
            button { padding: 14px 24px; border-radius: 8px; border: none; background: #2563eb; color: white; font-weight: bold; cursor: pointer; }
        </style>
    </head>
    <body>
        <h2>⚡ AutoMind Agent Live</h2>
        <div id="chat">
            <div class="msg bot">Namaste! Main AutoMind AI hoon. Aap mujhse koi bhi sawal puch sakte hain.</div>
        </div>
        <div class="input-box">
            <input type="text" id="userInput" placeholder="Kuch bhi likhein..." onkeydown="if(event.key==='Enter') send()">
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
                chat.scrollTop = chat.scrollHeight;

                const loadingId = 'loading-' + Date.now();
                chat.innerHTML += `<div class='msg bot' id='${loadingId}'>Soch raha hoon...</div>`;
                chat.scrollTop = chat.scrollHeight;

                try {
                    const res = await fetch('/chat', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({message: text})
                    });
                    const data = await res.json();
                    document.getElementById(loadingId).innerText = data.response;
                } catch(e) {
                    document.getElementById(loadingId).innerText = 'Error: Server se connect nahi ho paya.';
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
        # ai_hub ke real function ko call karein
        result = ai_hub.complete_silent(req.message)
        if result.get("ok"):
            return {"response": result.get("text")}
        else:
            return {"response": f"AI Error: {result.get('error')}"}
    except Exception as e:
        return {"response": f"Server Error: {str(e)}"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
