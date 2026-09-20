"""Chitti - a hands-free voice assistant.

Tap the orb once. After that Chitti listens all the time, answers out loud,
translates, switches language, fixes grammar and tells the weather - by voice only.
"""

import asyncio
import base64
import concurrent.futures
import io
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import edge_tts
import requests
import streamlit as st
import streamlit.components.v1 as components
from google import genai
from google.genai import types
from gtts import gTTS

st.set_page_config(page_title="Chitti", page_icon="🎙️")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")  # use the model name from your old app.py if different


def _api_key():
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    try:
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return None


API_KEY = _api_key()
if not API_KEY:
    st.error("Set your Gemini key first:  export GEMINI_API_KEY='your-key'  then restart.")
    st.stop()
client = genai.Client(api_key=API_KEY)

# Browser speech-recognition codes for the language you speak.
LANGUAGES = {
    "English": {"stt": "en-IN"},
    "Telugu": {"stt": "te-IN"},
    "Hindi": {"stt": "hi-IN"},
    "Tamil": {"stt": "ta-IN"},
    "Kannada": {"stt": "kn-IN"},
    "Malayalam": {"stt": "ml-IN"},
}

# Natural neural voices for Chitti (edge-tts). Anything missing falls back to gTTS.
VOICES = {
    "en-IN": "en-IN-NeerjaNeural",
    "en-US": "en-US-JennyNeural",
    "hi-IN": "hi-IN-SwaraNeural",
    "te-IN": "te-IN-ShrutiNeural",
    "ta-IN": "ta-IN-PallaviNeural",
    "kn-IN": "kn-IN-SapnaNeural",
    "ml-IN": "ml-IN-SobhanaNeural",
}

VOICE_STYLES = {
    "Friendly": {"rate": "+0%", "pitch": "+0Hz"},
    "Calm": {"rate": "-10%", "pitch": "-2Hz"},
    "Energetic": {"rate": "+12%", "pitch": "+4Hz"},
}

CHITTI_PERSONA = """You are Chitti, a warm, cheerful, quick voice assistant. The user talks to you by voice and your reply is spoken aloud, so answer immediately and directly in one to three short, natural sentences. No markdown, no lists, no emojis, no stage directions. You are always listening - never ask the user to press or click anything.

How to handle requests:
- Greetings and small talk ("good morning Chitti", "how are you"): answer warmly and directly, like a friend.
- Translation ("translate that to Telugu", "how do I say thank you in Hindi"): reply with only the translation, written in the target language's own script, and set reply_lang to that language. "that" or "it" means the last thing you or the user said - use the conversation so far.
- Talking in another language ("talk to me in Telugu / Hindi / English"): set set_reply_language to that language and answer in it right away, starting with a short confirmation in that language. Keep using it until the user changes it. If they say they will now SPEAK in a language, set set_speak_language too.
- Grammar: if the user's sentence has a real grammar or word-choice mistake, put the corrected sentence in "correction", still answer their request, and add one short spoken note with the corrected sentence. If they say "fix my grammar", the corrected sentence is your reply. Ignore punctuation, capitalisation and speech-recognition noise. If the sentence is fine, correction must be an empty string.
- Weather: set intent to "weather" and fill city and tomorrow. If the user gave no city and no default city is set, ask which city instead (intent "chat").
- Time and date: answer from the current time given below.
- If the user says to clear, reset or start over, set intent to "clear" and confirm briefly.
"""

RESPONSE_FORMAT = """
Return ONLY a JSON object, no other text, with exactly these keys:
{
  "reply": "what you say out loud (empty string only for weather)",
  "reply_lang": "BCP-47 code of the language of reply, e.g. en-IN, te-IN, hi-IN, ta-IN, kn-IN, ml-IN",
  "correction": "corrected version of the user's sentence if it had a grammar mistake, else empty string",
  "set_reply_language": null, or a language name like "Telugu", or "auto" to go back to replying in whatever language the user speaks,
  "set_speak_language": null, or a language name if the user says they will now speak in it,
  "intent": "chat" or "weather" or "clear",
  "city": city name for weather, else null,
  "tomorrow": true if tomorrow's weather was asked, else false
}
"""

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

DEFAULTS = {
    "turns": [],
    "conversation_lang": None,   # language Chitti was asked to talk in (None = same as user)
    "reply_text": "",
    "reply_id": 0,
    "reply_lang": "en-IN",
    "reply_audio": "",
    "last_event_id": None,
    "client_tz": "Asia/Kolkata",
    "notice": "",
    "pending_speak_lang": None,
}
for _key, _value in DEFAULTS.items():
    st.session_state.setdefault(_key, _value)

# Voice command "I'll speak Telugu now" changes the sidebar - must happen before the widget exists.
if st.session_state.pending_speak_lang:
    st.session_state.speak_language = st.session_state.pending_speak_lang
    st.session_state.pending_speak_lang = None

COMPONENT_HTML = r'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>
  :root { --fg: #31333f; --glow: rgba(91, 69, 255, .55); }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: transparent; color: var(--fg);
    font-family: "Source Sans Pro", system-ui, -apple-system, sans-serif; }
  #wrap { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 10px 8px 0; }
  #orb { width: 92px; height: 92px; border: 0; border-radius: 50%; cursor: pointer; font-size: 34px;
    color: #fff; outline: none; transition: transform .15s;
    background: radial-gradient(circle at 30% 30%, #9aa8ff, #5b45ff); }
  #orb:hover { transform: scale(1.05); }
  #orb.idle, #orb.paused { background: radial-gradient(circle at 30% 30%, #b8bdcf, #6b7186); }
  #orb.listening { --glow: rgba(91, 69, 255, .55); animation: pulse 1.8s infinite; }
  #orb.thinking { background: radial-gradient(circle at 30% 30%, #ffd98a, #ff9a1f); animation: breathe .9s infinite alternate; }
  #orb.speaking { --glow: rgba(23, 178, 106, .55); animation: pulse 1.1s infinite;
    background: radial-gradient(circle at 30% 30%, #8ff0b5, #17b26a); }
  #orb.error { background: radial-gradient(circle at 30% 30%, #ff9a9a, #d92d2d); }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 var(--glow); }
    70% { box-shadow: 0 0 0 24px rgba(0, 0, 0, 0); }
    100% { box-shadow: 0 0 0 0 rgba(0, 0, 0, 0); }
  }
  @keyframes breathe { from { transform: scale(.96); } to { transform: scale(1.06); } }
  #status { font-size: 14px; opacity: .75; }
  #caption { min-height: 46px; max-width: 94%; text-align: center; font-size: 18px; line-height: 1.35; }
</style>
</head>
<body>
  <div id="wrap">
    <button id="orb" class="idle" aria-label="Chitti microphone">🎙️</button>
    <div id="status">Tap once to wake Chitti</div>
    <div id="caption"></div>
  </div>

<script>
(function () {
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var orb = document.getElementById("orb");
  var statusEl = document.getElementById("status");
  var captionEl = document.getElementById("caption");

  var args = {};
  var started = false, paused = false, busy = false, speaking = false;
  var rec = null, buffer = "", sendTimer = null, guardTimer = null;
  var lastReplyId = null, curAudio = null, finishSpeaking = null, eventCount = 0;

  var ICONS = { idle: "🎙️", listening: "🎙️", thinking: "💭", speaking: "🔊", paused: "⏸️", error: "⚠️" };

  function post(type, data) {
    window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, data), "*");
  }
  function setValue(v) { post("streamlit:setComponentValue", { value: v, dataType: "json" }); }
  function setHeight(h) { post("streamlit:setFrameHeight", { height: h }); }

  function setState(state, text) {
    orb.className = state;
    orb.textContent = ICONS[state] || "🎙️";
    statusEl.textContent = text;
  }
  function caption(text) { captionEl.textContent = text || ""; }

  // ---------------------------------------------------------------- listening
  function buildRec() {
    rec = new SR();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = args.lang || "en-IN";

    rec.onstart = function () {
      if (started && !paused && !busy) setState("listening", "Listening - just talk");
    };

    rec.onresult = function (ev) {
      var interim = "";
      for (var i = ev.resultIndex; i < ev.results.length; i++) {
        var r = ev.results[i];
        if (r.isFinal) buffer += (buffer ? " " : "") + r[0].transcript.trim();
        else interim += r[0].transcript;
      }
      caption("You: " + (buffer + " " + interim).trim());
      clearTimeout(sendTimer);
      // When the speaker pauses (~1s after the last finished phrase), send it to Chitti.
      if (buffer && !interim) sendTimer = setTimeout(flush, 1000);
    };

    rec.onerror = function (ev) {
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
        started = false;
        setState("error", "Microphone is blocked - allow it in the address bar, then tap the orb");
      }
      // "no-speech", "aborted", "network": onend restarts listening.
    };

    rec.onend = function () {
      if (started && !paused && !busy) setTimeout(startRec, 300);
    };
  }

  function startRec() {
    if (!SR || !started || paused || busy) return;
    if (!rec) buildRec();
    rec.lang = args.lang || rec.lang;
    try { rec.start(); } catch (e) { /* already running - onend will retry */ }
  }

  function stopRec() {
    if (rec) { try { rec.abort(); } catch (e) {} }
  }

  function flush() {
    var text = buffer.trim();
    buffer = "";
    if (!text) return;
    busy = true;
    stopRec();
    setState("thinking", "Chitti is thinking...");
    caption("You: " + text);
    eventCount += 1;
    setValue({
      id: Date.now() + "-" + eventCount,
      text: text,
      tz: (Intl.DateTimeFormat().resolvedOptions().timeZone || "")
    });
    clearTimeout(guardTimer);
    guardTimer = setTimeout(resume, 30000);   // never get stuck if no reply arrives
  }

  function resume() {
    clearTimeout(guardTimer);
    busy = false; speaking = false;
    if (started && !paused) startRec();
    else if (paused) setState("paused", "Paused - tap to resume");
  }

  // ----------------------------------------------------------------- speaking
  function browserSpeak(text, lang, done) {
    if (!window.speechSynthesis) { done(); return; }
    var u = new SpeechSynthesisUtterance(text);
    u.lang = lang || "en-IN";
    u.onend = done; u.onerror = done;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  }

  function speakOut(a) {
    speaking = true;
    setState("speaking", "Chitti is talking (tap to interrupt)");
    caption("Chitti: " + a.reply);
    var finished = false;
    var safety = null;
    finishSpeaking = function () {
      if (finished) return;
      finished = true;
      clearTimeout(safety);
      curAudio = null;
      setTimeout(resume, 350);   // small gap so the mic never hears the tail of Chitti's voice
    };
    safety = setTimeout(finishSpeaking, 60000);

    if (a.reply_audio) {
      curAudio = new Audio("data:audio/mp3;base64," + a.reply_audio);
      curAudio.onended = finishSpeaking;
      curAudio.onerror = function () { browserSpeak(a.reply, a.reply_lang, finishSpeaking); };
      curAudio.play().catch(function () { browserSpeak(a.reply, a.reply_lang, finishSpeaking); });
    } else {
      browserSpeak(a.reply, a.reply_lang, finishSpeaking);
    }
  }

  function interrupt() {
    if (curAudio) { try { curAudio.pause(); } catch (e) {} }
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    if (finishSpeaking) finishSpeaking();
  }

  function onReply(a) {
    clearTimeout(guardTimer);
    var text = (a.reply || "").trim();
    if (!text) { resume(); return; }
    caption("Chitti: " + text);
    if (!a.speak) { resume(); return; }
    speakOut(a);
  }

  // -------------------------------------------------------------------- orb
  orb.addEventListener("click", function () {
    if (!SR) return;
    if (!started) { started = true; paused = false; startRec(); return; }   // the one and only tap
    if (speaking) { interrupt(); return; }
    if (busy) return;
    paused = !paused;
    if (paused) {
      stopRec(); buffer = ""; caption("");
      setState("paused", "Paused - tap to resume");
    } else {
      startRec();
    }
  });

  // ----------------------------------------------------- Streamlit plumbing
  window.addEventListener("message", function (e) {
    if (!e.data || e.data.type !== "streamlit:render") return;
    var a = e.data.args || {};
    var theme = e.data.theme;
    if (theme && theme.textColor) document.documentElement.style.setProperty("--fg", theme.textColor);
    args = a;

    if (lastReplyId === null) {
      lastReplyId = a.reply_id;            // first render: never replay an old reply
    } else if (a.reply_id !== lastReplyId) {
      lastReplyId = a.reply_id;
      onReply(a);
    }

    if (rec && a.lang && rec.lang !== a.lang) {   // "I usually speak in" changed
      rec.lang = a.lang;
      if (started && !busy && !paused) stopRec();  // onend restarts with the new language
    }
    setHeight(230);
  });

  if (!SR) {
    setState("error", "This browser has no speech recognition - open the app in Chrome or Edge");
  }
  post("streamlit:componentReady", { apiVersion: 1 });
  setHeight(230);
})();
</script>
</body>
</html>
'''

# The always-listening mic lives in a tiny web page. Chitti writes it next to this file
# automatically, so you only ever need this one Python file.
COMPONENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "listener_component")
COMPONENT_FILE = os.path.join(COMPONENT_DIR, "index.html")
os.makedirs(COMPONENT_DIR, exist_ok=True)
_current = ""
if os.path.exists(COMPONENT_FILE):
    with open(COMPONENT_FILE, encoding="utf-8") as _f:
        _current = _f.read()
if _current != COMPONENT_HTML:
    with open(COMPONENT_FILE, "w", encoding="utf-8") as _f:
        _f.write(COMPONENT_HTML)

listener = components.declare_component("chitti_listener", path=COMPONENT_DIR)

# ---------------------------------------------------------------------------
# Time + weather
# ---------------------------------------------------------------------------


def now_local() -> datetime:
    try:
        return datetime.now(ZoneInfo(st.session_state.client_tz))
    except Exception:
        return datetime.now()


WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


def get_weather(city: str, tomorrow: bool = False):
    """Return a plain-English weather fact string, or None if the city can't be found."""
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1},
            timeout=8,
        ).json()
        place = (geo.get("results") or [None])[0]
        if not place:
            return None
        data = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 2,
            },
            timeout=8,
        ).json()
    except Exception:
        return None

    label = ", ".join(p for p in (place.get("name"), place.get("admin1")) if p)
    daily = data["daily"]
    i = 1 if tomorrow else 0
    high, low = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]
    rain = daily["precipitation_probability_max"][i] or 0
    day_desc = WEATHER_CODES.get(daily["weather_code"][i], "mixed weather")
    if tomorrow:
        return f"Weather in {label} tomorrow: {day_desc}, high {high:.0f}°C, low {low:.0f}°C, {rain}% chance of rain."
    cur = data["current"]
    now_desc = WEATHER_CODES.get(cur["weather_code"], "mixed weather")
    return (
        f"Weather in {label} right now: {now_desc}, {cur['temperature_2m']:.0f}°C, "
        f"wind {cur['wind_speed_10m']:.0f} km/h. Today's high {high:.0f}°C, low {low:.0f}°C, "
        f"{rain}% chance of rain."
    )


# ---------------------------------------------------------------------------
# Speech + Gemini helpers
# ---------------------------------------------------------------------------


def voice_for(code: str):
    if code in VOICES:
        return VOICES[code]
    prefix = code.split("-")[0]
    return next((v for k, v in VOICES.items() if k.startswith(prefix + "-")), None)


def _edge_speak(text: str, voice: str, style: dict) -> bytes:
    async def run() -> bytes:
        communicate = edge_tts.Communicate(text, voice, rate=style["rate"], pitch=style["pitch"])
        buffer = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buffer.write(chunk["data"])
        return buffer.getvalue()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, run()).result(timeout=25)


def speak(text: str, lang_code: str, style_name: str) -> str:
    """Return base64 mp3 of Chitti's voice, or '' (the browser voice takes over)."""
    text = text.strip()[:600]
    if not text:
        return ""
    voice = voice_for(lang_code)
    if voice:
        try:
            audio = _edge_speak(text, voice, VOICE_STYLES[style_name])
            if audio:
                return base64.b64encode(audio).decode()
        except Exception:
            pass
    try:
        buffer = io.BytesIO()
        gTTS(text=text, lang=lang_code.split("-")[0]).write_to_fp(buffer)
        return base64.b64encode(buffer.getvalue()).decode()
    except Exception:
        return ""


def generate(contents, system_instruction: str, json_mode: bool = True) -> str:
    base = {"system_instruction": system_instruction}
    if json_mode:
        base["response_mime_type"] = "application/json"
    last_error = None
    # First try with "thinking" switched off (much faster for voice); fall back if the model refuses.
    for extra in ({"thinking_config": types.ThinkingConfig(thinking_budget=0)}, {}):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=types.GenerateContentConfig(**base, **extra),
            )
            return (response.text or "").strip()
        except Exception as exc:
            last_error = exc
    raise last_error


def parse_json(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(cleaned)
    except Exception:
        return {"reply": raw}
    return data if isinstance(data, dict) else {"reply": raw}


def build_instruction(default_city: str) -> str:
    now = now_local()
    forced = st.session_state.conversation_lang
    if forced:
        lang_rule = f"The user asked you to talk in {forced}. Reply in {forced} until they change it."
    else:
        lang_rule = "Reply in the same language the user just spoke."
    context = (
        f"\nCurrent local date and time: {now:%A, %d %B %Y, %I:%M %p} ({st.session_state.client_tz}).\n"
        f"Default city for weather: {default_city or '(not set)'}.\n{lang_rule}\n"
    )
    return CHITTI_PERSONA + context + RESPONSE_FORMAT


def ask_chitti(history: list, default_city: str) -> dict:
    contents = []
    for turn in history[-14:]:
        role = "user" if turn["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part(text=turn["text"])]))
    while contents and contents[0].role == "model":
        contents.pop(0)
    return parse_json(generate(contents, build_instruction(default_city)))


def weather_reply(city: str, tomorrow: bool, lang_code: str) -> str:
    facts = get_weather(city, tomorrow) or f"No weather data could be found for '{city}'."
    return generate(
        f"Tell the user this in one or two friendly spoken sentences, in the language with code {lang_code}: {facts}",
        "You are Chitti, a friendly voice assistant. Plain text only, no markdown or emojis.",
        json_mode=False,
    )


# ---------------------------------------------------------------------------
# One spoken sentence in -> Chitti's answer out
# ---------------------------------------------------------------------------


def _same(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^\w\s]", "", s.lower()).strip()
    return norm(a) == norm(b)


def process_command(text: str, default_city: str, speak_replies: bool, style_name: str):
    turns = st.session_state.turns
    turns.append({"role": "user", "text": text})
    reply, reply_lang, audio = "", st.session_state.reply_lang, ""

    try:
        result = ask_chitti(turns, default_city)
        reply_lang = result.get("reply_lang") or reply_lang

        fix = (result.get("correction") or "").strip()
        if fix and not _same(fix, text):
            turns[-1]["fix"] = fix

        new_lang = result.get("set_reply_language")
        if new_lang:
            new_lang = str(new_lang).strip().title()
            st.session_state.conversation_lang = None if new_lang in ("Auto", "None", "Same") else new_lang

        speak_lang = str(result.get("set_speak_language") or "").strip().title()
        if speak_lang in LANGUAGES:
            st.session_state.pending_speak_lang = speak_lang

        if result.get("intent") == "weather" and (result.get("city") or default_city):
            city = (result.get("city") or default_city).strip()
            reply = weather_reply(city, bool(result.get("tomorrow")), reply_lang)
        else:
            reply = (result.get("reply") or "").strip()

        if result.get("intent") == "clear":
            st.session_state.turns = turns = []
            st.session_state.conversation_lang = None
    except Exception as exc:
        st.session_state.notice = f"Chitti had a problem: {exc}"
        turns.pop()

    if reply:
        turns.append({"role": "chitti", "text": reply})
        if speak_replies:
            audio = speak(reply, reply_lang, style_name)

    # Always bump reply_id (even on errors) so the mic component starts listening again.
    st.session_state.reply_text = reply
    st.session_state.reply_id += 1
    st.session_state.reply_lang = reply_lang
    st.session_state.reply_audio = audio


# ---------------------------------------------------------------------------
# Sidebar settings
# ---------------------------------------------------------------------------

st.sidebar.header("Settings")
speak_language = st.sidebar.selectbox("I usually speak in", list(LANGUAGES), key="speak_language")
my_city = st.sidebar.text_input("My city (for weather)", placeholder="e.g. Bengaluru")
speak_replies = st.sidebar.toggle("Chitti answers out loud", value=True)
voice_style = st.sidebar.radio("Chitti's voice", list(VOICE_STYLES), index=0, horizontal=True)
if st.session_state.conversation_lang:
    st.sidebar.caption(f"🗣️ Chitti is talking in **{st.session_state.conversation_lang}**")

# ---------------------------------------------------------------------------
# Header + always-listening component
# ---------------------------------------------------------------------------

st.title("Chitti")
st.caption("Tap the orb once, then just talk. No buttons - Chitti answers by voice.")

event = listener(
    lang=LANGUAGES[speak_language]["stt"],
    reply=st.session_state.reply_text,
    reply_id=st.session_state.reply_id,
    reply_lang=st.session_state.reply_lang,
    reply_audio=st.session_state.reply_audio,
    speak=speak_replies,
    key="listener",
    default=None,
)

if event and event.get("id") != st.session_state.last_event_id:
    st.session_state.last_event_id = event["id"]
    if event.get("tz"):
        st.session_state.client_tz = event["tz"]
    st.session_state.notice = ""
    heard = (event.get("text") or "").strip()
    if heard:
        process_command(heard, my_city.strip(), speak_replies, voice_style)
        st.rerun()  # rerun so the component receives Chitti's reply and speaks it

if st.session_state.notice:
    st.warning(st.session_state.notice)

# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

if not st.session_state.turns:
    st.info(
        "Try saying: \"Good morning Chitti\" - \"How are you?\" - \"Translate good night to Telugu\" - "
        "\"Talk to me in Hindi\" - \"What's the weather tomorrow?\" - "
        "\"Me go to market yesterday\" (Chitti fixes it) - \"Start over\""
    )

for turn in st.session_state.turns[-16:]:
    if turn["role"] == "user":
        with st.chat_message("user"):
            st.write(turn["text"])
            if turn.get("fix"):
                st.caption(f"✏️ Better: {turn['fix']}")
    else:
        with st.chat_message("assistant", avatar="🤖"):
            st.write(turn["text"])
