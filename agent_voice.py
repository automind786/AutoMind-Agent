"""agent_voice.py - Universal Dual-Mode Voice Engine (Pydroid 3 + Android APK Ready)."""
import os
import sys

def _try_hardware_speech():
    """APK environment aur supported hardware ke liye direct audio recognition."""
    try:
        import speech_recognition as sr
        r = sr.Recognizer()
        with sr.Microphone() as source:
            print("\n[Voice Engine]: Microphone active... boliye (Speak now)")
            r.adjust_for_ambient_noise(source, duration=0.8)
            audio = r.listen(source, timeout=6, phrase_time_limit=10)

        print("[Voice Engine]: Processing audio...")
        # Hindi aur English dono mix pehchane ke liye
        text = r.recognize_google(audio, language="hi-IN")
        if text:
            print("[Aapne Kaha]:", text)
            return True, text.strip()
    except Exception:
        # Hardware driver ya permission na hone par silent switch
        return False, ""
    return False, ""


def _try_gui_speech_dialog():
    """Android GUI ya pop-up keyboard mic input trigger karta hai."""
    try:
        from tkinter import simpledialog, Tk
        root = Tk()
        root.withdraw()
        spoken_text = simpledialog.askstring("AutoMind Voice", "Boliye ya command likhein:")
        root.destroy()
        if spoken_text:
            print("[AutoMind Received]:", spoken_text)
            return True, spoken_text.strip()
    except Exception:
        return False, ""
    return False, ""


def listen_and_convert():
    """Primary wrapper jo system ki capacity ke mutabiq best mode select karta hai."""
    # 1. First priority: Direct hardware mic (APK build me auto chalega)
    ok, text = _try_hardware_speech()
    if ok and text:
        return True, text

    # 2. Second priority: GUI Dialog (agar screen server supported ho)
    ok, text = _try_gui_speech_dialog()
    if ok and text:
        return True, text

    # 3. Third priority (Pydroid 3 Safe Bridge): Terminal fallback prompt
    print("\n--- [AutoMind Voice Bridge (Safe Mode)] ---")
    print("Microphone hardware abhi standalone APK me activate hoga.")
    print("Aap yahan text likh sakte hain ya kisi dusri app se copy karke paste kar sakte hain:")

    try:
        user_input = input("\nai [Voice/Input]> ").strip()
        if user_input:
            print("[AutoMind Received]:", user_input)
            return True, user_input
    except (EOFError, KeyboardInterrupt):
        return False, ""

    return False, ""
