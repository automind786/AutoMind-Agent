# AutoMind Background YouTube API Uploader
# Zaroorat: pip install google-api-python-client google-auth-oauthlib
import json

with open("/storage/emulated/0/AutoMind 2.0/AutoMind_Ultimate_2.5.0_AgentUpgrade_Pydroid/AutoMind_Ultimate_2.5.0/app_sessions/youtube_action_20261024_112912.json", "r") as f:
    payload = json.load(f)["inputs"]

print("Uploading to YouTube:", payload.get("title"))
print("File:", payload.get("video_path"))
print("Status: Client secrets file verify karke direct upload call initiate karein.")
