"""Livetalk — Python backend: FULL PROXY architecture.

The renderer never talks to Google. This module owns the upstream Live
WebSocket (verified working from Python repeatedly) and exposes a simple
local REST surface:

    GET  /config          -> { model, candidates[], prefs }      (no key material)
    POST /start           -> opens the upstream session; { ok, model, resumed, speak }
    POST /audio {data}    -> forwards one PCM16/16k chunk upstream
    GET  /poll            -> { session, input[], output[], audio[], delegations[], ... }
                             all lists are INCREMENTAL (?in_offset=&out_offset=&a_offset=&d_offset=)
    POST /tool_response   -> answers a queued talk delegation upstream
    POST /close           -> closes the session
    GET  /status          -> liveness probe

Auth: an ephemeral 1-use token is minted per dial attempt; the API key never
leaves this process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from array import array
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

router = APIRouter()

DEFAULT_MODEL = "models/gemini-3.8-live"
_DIR = os.path.dirname(__file__)
RESUME_FILE = os.path.join(_DIR, ".resume.json")   # last resumable session handle

# Transcription language hints (AudioTranscriptionConfig.languageCodes — BCP-47).
# Hints reduce wrong-language detection on short prompts; an empty list means
# pure auto-detect. The choice lists live in plugin.yaml's config_schema —
# the app renders that form natively under Settings → Plugins.
_LANG_CODES = frozenset([
    "af-ZA", "am-ET", "ar-EG", "hy-AM", "as-IN", "az-AZ", "be-BY", "bn-BD", "bn-IN",
    "bs-BA", "bg-BG", "rup-BG", "my-MM", "yue-Hant-HK", "ca-ES", "ceb", "km-KH",
    "hr-HR", "cs-CZ", "da-DK", "nl-NL", "en-GB", "en-IN", "en-US", "et-EE", "fa-IR",
    "fil-PH", "fi-FI", "fr-FR", "gl-ES", "ka-GE", "de-DE", "el-GR", "gu-IN", "ha-NG",
    "he-IL", "hi-IN", "hu-HU", "is-IS", "id-ID", "it-IT", "ja-JP", "jv-ID", "kea-CV",
    "kn-IN", "kk-KZ", "ko-KR", "ky-KG", "lv-LV", "ln-CD", "lt-LT", "mk-MK", "ms-MY",
    "ml-IN", "mt-MT", "cmn-Hans-CN", "mr-IN", "mn-MN", "ne-NP", "nb-NO", "or-IN",
    "pl-PL", "pt-BR", "pt-PT", "pa-IN", "pa-Guru-IN", "ro-RO", "ru-RU", "sr-RS",
    "sd-Arab-IN", "sk-SK", "sl-SI", "es-419", "es-US", "sw-KE", "sv-SE", "tg-TJ",
    "te-IN", "th-TH", "tr-TR", "uk-UA", "uz-UZ", "vi-VN",
])
_TRANS_VOCAB = ["Hermes", "Gemini", "Kanban", "CoinGecko"]
_VOICE_NAMES = frozenset([
    "Puck", "Aoede", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Zephyr", "Autonoe",
    "Umbriel", "Erinome", "Laomedeia", "Schedar", "Achird", "Sadachbia", "Enceladus",
    "Algieba", "Algenib", "Achernar", "Gacrux", "Zubenelgenubi", "Sadaltager",
    "Callirrhoe", "Iapetus", "Despina", "Rasalgethi", "Alnilam", "Pulcherrima",
    "Vindemiatrix", "Sulafat",
])
_PLUGIN_NAME = "livetalk"


LIST_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
AUTH_TOKENS_URL = "https://generativelanguage.googleapis.com/v1beta/auth_tokens"
WS_HOST = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained"

_state: dict = {
    "session": None,        # 'live' | None
    "ws": None,             # upstream websocket
    "task": None,           # reader task
    "model": "",
    "provider": "",         # 'gemini' | 'elevenlabs'
    "input": [],            # input transcription pieces
    "output": [],           # output transcription pieces
    "audio": [],            # model audio chunks (b64 PCM16/24k)
    "error": "",
    "interaction": "",
    "usage": None,
    "resumed": False,
    "manual": False,        # user called /close (blocks auto-redial)
    "redials": 0,
    "delegations": [],      # pending talk toolCalls: [{id, request}]
    "recalls": [],          # pending recall toolCalls: [{id, query}]
    "tool_in_flight": None,
    "interrupted": 0,
    "handle": "",
    "responding": False,    # ElevenLabs: a response is streaming
    "keepalive": "",
}


def _api_key() -> str:
    return _env_value("GOOGLE_API_KEY")


def _mint_token() -> str:
    """Short-lived auth token for the Gemini Live websocket."""
    now = datetime.now(timezone.utc)
    body = json.dumps({
        "uses": 1,
        "expireTime": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newSessionExpireTime": (now + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }).encode()
    req = urllib.request.Request(AUTH_TOKENS_URL, data=body,
                                headers={"x-goog-api-key": _api_key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())["name"]


def _load_resume() -> dict:
    """Stored resumable handle ({handle, model, t}) or {} when absent."""
    try:
        with open(RESUME_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_resume(handle: str, model: str) -> None:
    try:
        with open(RESUME_FILE, "w", encoding="utf-8") as fh:
            json.dump({"handle": handle, "model": model, "t": time.time()}, fh)
    except Exception:  # noqa: BLE001
        pass


def _candidates() -> list[str]:
    key = _api_key()
    if not key:
        return [DEFAULT_MODEL]
    try:
        req = urllib.request.Request(f"{LIST_MODELS_URL}?key={key}&pageSize=200",
                                     headers={"User-Agent": "hermes-livetalk"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        names = [
            m["name"] for m in data.get("models", [])
            if ("live" in m.get("name", "").lower() or "native-audio" in m.get("name", "").lower())
            and "bidiGenerateContent" in (m.get("supportedGenerationMethods") or [])
            and "transcribe" not in m.get("name", "").lower()
            and "translate" not in m.get("name", "").lower()
        ]
        if not names:
            return [DEFAULT_MODEL]

        # Fixed catalog order: 3.8-live -> native-audio (latest, then newest
        # preview first) -> the rest.
        def rank(n: str) -> tuple:
            if n == "models/gemini-3.8-live":
                return (0, 0)
            if "native-audio-latest" in n:
                return (1, 0)
            if "native-audio-preview" in n:
                digits = [int(x) for x in n.split("preview-")[-1].replace("-2025", "").split("-") if x.isdigit()]
                return (2, -max(digits) if digits else 0)
            return (3, 0)

        return sorted(names, key=lambda n: (rank(n), n))
    except Exception:  # noqa: BLE001
        return [DEFAULT_MODEL]


async def _reader(ws) -> None:
    """Pump upstream frames into the state lists until closed."""
    try:
        while True:
            frame = await ws.recv()
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8", "replace")
            msg = json.loads(frame)
            if msg.get("interactionStatus"):
                _state["interaction"] = msg["interactionStatus"]
            sru = msg.get("sessionResumptionUpdate")
            if sru and sru.get("newHandle") and sru.get("resumable"):
                _state["handle"] = sru["newHandle"]
                _save_resume(sru["newHandle"], _state.get("model", ""))
            if msg.get("usageMetadata"):
                _state["usage"] = msg["usageMetadata"]
            sc = msg.get("serverContent")
            if sc:
                t = (sc.get("inputTranscription") or {}).get("text")
                if t:
                    _state["input"].append(t)
                t = (sc.get("outputTranscription") or {}).get("text")
                if t:
                    _state["output"].append(t)
                for part in (sc.get("modelTurn") or {}).get("parts", []):
                    b64 = (part.get("inlineData") or {}).get("data")
                    if b64:
                        _state["audio"].append(b64)
                if sc.get("interrupted"):
                    # server-side VAD barge-in: bump a monotonic counter so the
                    # renderer drops its pending playback queue mid-sentence
                    _state["interrupted"] += 1
                if sc.get("turnComplete"):
                    _state["input"].append("—")
            tc = msg.get("toolCall")
            if tc:
                # Queue each call for the renderer: talk -> Hermes agent,
                # recall -> session history lookup (answered via /tool_response).
                for fc in tc.get("functionCalls", []):
                    args = fc.get("args") or {}
                    if fc.get("name", "") == "recall":
                        _state["recalls"].append({
                            "id": fc.get("id", ""),
                            "query": str(args.get("query", ""))[:200],
                        })
                    else:
                        _state["delegations"].append({
                            "id": fc.get("id", ""),
                            "request": str(args.get("request", ""))[:2000],
                        })
            tcc = msg.get("toolCallCancellation")
            if tcc and tcc.get("ids"):
                ids = set(tcc["ids"])
                _state["delegations"] = [d for d in _state["delegations"] if d.get("id") not in ids]
                _state["recalls"] = [r for r in _state["recalls"] if r.get("id") not in ids]
            if msg.get("error"):
                _state["error"] = str(msg["error"].get("message") or msg["error"])[:200]
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — any upstream drop surfaces in /poll
        _state["error"] = f"upstream closed: {exc}"[:200]
    finally:
        _state["session"] = None
        # Auto-redial: unexpected drop (not a user /close) with a valid handle
        # -> reconnect in the background so the conversation keeps its context.
        if not _state["manual"] and _state["redials"] < 2 and _state.get("handle"):
            _state["redials"] += 1
            asyncio.get_event_loop().create_task(start({"_auto": True, "resume": True}))


def _load_settings() -> dict:
    """Read the plugin's settings via Hermes' official plugin-settings mechanism
    (config_schema in plugin.yaml — the Settings → Plugins form writes through
    plugins.manage). Falls back to defaults on any failure — settings must never
    break the session."""
    defaults = {"model": "gemini-3.8-live", "primary_language": "id-ID",
                "secondary_language": "en-US", "smart_transcription": True,
                "spoken_answers": "full", "show_transcript": True, "show_log": True,
                "voice": "Puck", "provider": "gemini"}
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins import _plugin_settings_entry
        entry = _plugin_settings_entry(load_config_readonly() or {}, _PLUGIN_NAME) or {}
        st = entry.get("settings") or {}
    except Exception:  # noqa: BLE001
        st = {}
    primary = st.get("primary_language")
    if primary is None: primary = defaults["primary_language"]      # absent → schema default
    secondary = st.get("secondary_language")
    if secondary is None: secondary = defaults["secondary_language"]  # absent → schema default
    model = st.get("live_model")
    allowed = _SETTINGS_SPEC["live_model"]["choices"]
    if not (isinstance(model, str) and model in allowed):
        model = defaults["model"]
    return {
        "model": model if isinstance(model, str) and model else defaults["model"],
        "primary_language": primary if primary in _LANG_CODES else defaults["primary_language"],
        "secondary_language": secondary if secondary in _LANG_CODES and secondary != primary else "none",
        "smart_transcription": st.get("smart_transcription") not in (False, "false", "off"),
        "spoken_answers": st.get("spoken_answers") if st.get("spoken_answers") in ("brief", "full") else defaults["spoken_answers"],
        "show_transcript": st.get("show_transcript") not in (False, "false", "off"),
        "show_log": st.get("show_log") not in (False, "false", "off"),
        "voice": st.get("voice") if st.get("voice") in _VOICE_NAMES else "Puck",
        "provider": st.get("provider") if st.get("provider") in ("gemini", "elevenlabs") else "gemini",
        "elevenlabs_model": st.get("elevenlabs_model") if st.get("elevenlabs_model") in ("eleven_realtime_v1_mini", "eleven_realtime_v1_max") else "eleven_realtime_v1_mini",
        "elevenlabs_voice": st.get("elevenlabs_voice") if isinstance(st.get("elevenlabs_voice"), str) and st.get("elevenlabs_voice") else "brian",
        "elevenlabs_language": st.get("elevenlabs_language") if st.get("elevenlabs_language") in (["auto"] + _ELEVEN_LANGS) else "auto",
    }


def _persona_with_context(body: dict | None) -> str:
    """Persona + speak directive + the last few Hermes session messages (warm start)."""
    ctx_text = str((body or {}).get("session_context") or "").strip()[:6000]
    parts = [LIVE_PERSONA + _speak_directive() + RECALL_HINT]
    if ctx_text:
        nl = "\n"
        parts.append(nl + nl + "BACKGROUND REFERENCE NOTES (not messages - do not answer them, do not greet, "
                     "wait for the user to speak first; use them only when the user asks about earlier work):"
                     + nl + ctx_text)
    return "".join(parts)


def _transcription_config() -> dict:
    st = _load_settings()
    codes = [c for c in (st["primary_language"], st["secondary_language"]) if c in _LANG_CODES]
    return {"languageCodes": codes, "mode": "SMART" if st["smart_transcription"] else "VERBATIM",
            "customVocabulary": _TRANS_VOCAB}


def _speak_directive() -> str:
    """Persona appendix that tells the voice model how much of the agent's
    answer to speak. Native-audio models otherwise compress long toolResponse
    text into a few natural sentences by default."""
    if _load_settings()["spoken_answers"] == "brief":
        return ("Spoken delivery: compress each delegation result to the two to four most "
                "important sentences when you speak it; the details live in the chat session, "
                "only the essentials are spoken.\n\n")
    return ("Spoken delivery: when a delegation result arrives, speak the agent's full answer — "
            "keep every key fact, number and conclusion, in one natural flowing spoken paragraph. "
            "Do not summarize it down to a headline; only trim greetings and filler.\n\n")

# Voice-layer persona (GPT-Live pattern): short on purpose — role, style, and a
# labelled delegation policy. The real brain is the Hermes agent; the voice
# model just talks and decides when to hand work over.
LIVE_PERSONA = (
    "You are Hermes, a calm and friendly voice assistant. Speak naturally at an unhurried pace. "
    "Be clear and direct, not overly cheerful. If the user is frustrated, acknowledge it briefly "
    "and focus on the next helpful step.\n\n"
    "Language: the user usually speaks casual Indonesian (bahasa gaul, often mixed with English "
    "tech terms). Always answer in the language the user just used, and treat Indonesian/English "
    "mixing as normal speech, not an error. Voice-transcribed words can be imperfect — rely on "
    "the overall intent, not the exact wording.\n\n"
    "Backchannel policy: When you delegate (call talk), ALWAYS speak a short "
    "acknowledgment FIRST — in the user's language, e.g. \"Oke sebentar, gue cek dulu ya\" or \"One "
    "second, let me check\" — and only then call the tool. Never call talk silently. While "
    "waiting for the result you may fill the silence with a brief natural remark, then deliver "
    "the answer when it arrives. Acknowledge naturally without competing with the main response.\n\n"
    "Interruption policy: Stop speaking when the user interrupts. Listen to what they say.\n\n"
    "Delegation policy:\n"
    "Backend tools:\n"
    "- Hermes agent: a full AI agent with tools — it can run commands, read and edit files, "
    "browse the web, search, remember things across sessions, schedule tasks, and reason "
    "carefully about anything. It is the one who actually does work and knows facts.\n\n"
    "Delegate to the backend when:\n"
    "- The user asks a question that needs LIVE data, their machine, or real work.\n"
    "- The user asks you to do, check, find, make, fix, run or remember anything.\n"
    "- A correction changes work already requested.\n\n"
    "Do NOT delegate to the backend when:\n"
    "- The user greets you, vents, or just wants to chat — answer warmly yourself.\n"
    "- The question is general knowledge you already know (explain a concept, tell a joke).\n"
    "- The user asks you to repeat or rephrase a result already given.\n"
    "- You need a brief clarification to understand the request.\n\n"
    "Routing examples:\n"
    "- 'berapa harga BTC sekarang?' → delegate (live market data).\n"
    "- 'Bitcoin itu apa sih?' → answer yourself (general knowledge).\n"
    "- 'cek isi folder Downloads aku' → delegate (needs the machine).\n"
    "- 'gw capek banget hari ini bang' → chat, answer yourself.\n"
    "- 'ingetin aku besok jam 8' → delegate (needs memory/scheduling).\n"
    "- 'eh tadi katanya berapa persen?' → repeat the number yourself, do not delegate again.\n\n"
    "CONTEXT FIRST: this conversation is your memory. If the answer is already in it — a number "
    "you just said, a result you just delivered, a fact the user confirmed — SPEAK IT, never "
    "delegate it again. Delegate only when the answer is genuinely missing or needs to be fresh.\n\n"
    "Delegate before giving an answer that depends on backend work. Do not guess the result "
    "while waiting; say briefly that you are checking, then wait for the result."
)

RECALL_HINT = (
    " You also have a recall tool. Whenever the user asks about anything from earlier in this "
    "conversation (kemarin, tadi, pernah, sebelumnya), call recall with the topic keywords before "
    "answering, and never claim a topic was never discussed without calling recall first. "
    "Never speak first: wait for the user to talk."
)

RECALL_TOOL = {
    "name": "recall",
    "description": (
        "ALWAYS call this before saying you do not know or do not remember something about earlier "
        "in this conversation. It searches the Hermes session behind this voice call (the same session "
        "the talk tool uses) and returns the matching earlier messages. Pass short keywords. "
        "Read-only, fast, no agent run."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Short keywords, e.g. 'relay bounty result'."}
        },
        "required": ["query"],
    },
}

TALK_TOOL = {
    "name": "talk",
    "description": (
        "Delegate a request to the Hermes agent (a full AI agent with tools). Use for anything "
        "that needs facts, current information, careful reasoning, or real work: run commands, "
        "read or edit files, browse the web, search, remember, schedule. Pass the user's latest "
        "intent as a clear, self-contained instruction. Returns the agent's answer, which you "
        "will speak aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": "The user's latest intent as a clear, self-contained instruction for the agent.",
            },
        },
        "required": ["request"],
    },
}


@router.get("/config")
async def config() -> dict:
    h = _load_resume()
    s = _load_settings()
    return {"model": s["model"], "candidates": _candidates(), "resumable": bool(h.get("handle")),
            "show_transcript": s["show_transcript"], "show_log": s["show_log"]}


# ---- Settings page storage -------------------------------------------------
# The desktop settings page (desktop/plugin.js) reads/writes through these
# endpoints instead of a manifest config_schema form, so Settings → Plugins
# shows ONE page. Values land in the same plugins.entries.livetalk.settings
# store via Hermes' official per-key writer (cross-process lock + managed
# install / managed key checks enforced there).
# Eleven v4 language set (fetched live from GET /v1/models; the realtime models
# accept the same codes — spot-checked incl. id, jv, ms, ja, ar, hi).
_ELEVEN_LANGS = ("af,am,ar,as,ast,az,be,bg,bn,bs,ca,ceb,ckb,cs,cy,da,de,el,en,es,et,fa,ff,fi,fil,fr,ga,gl,gu,ha,he,"
                 "hi,hr,hu,hy,id,is,it,ja,jv,ka,kam,kk,kn,ko,ky,lb,lg,ln,lo,lt,lv,mi,mk,ml,mn,mr,ms,mt,my,ne,nl,no,"
                 "ny,oc,or,pa,pl,ps,pt,ro,ru,sd,sk,sl,sn,so,sr,sv,sw,ta,te,tg,th,tr,uk,ur,uz,vi,wo,yo,yue,zh,zu").split(",")
_ELEVEN_DEFAULT_VOICE = "nPczCjzI2devNBz1zQrb"   # premade "Brian — Deep, Resonant and Comforting"

_SETTINGS_SPEC = {
    "provider": {"scope": "common", "type": "enum", "choices": ["gemini", "elevenlabs"], "default": "gemini",
                 "label": "Provider", "description": "The voice provider for the live session."},
    "live_model": {"scope": "gemini", "type": "enum", "default": "gemini-3.8-live",
                   "choices": ["gemini-3.8-live", "gemini-2.5-flash-native-audio-latest", "gemini-2.5-flash-native-audio-preview-12-2025",
                               "gemini-2.5-flash-native-audio-preview-09-2025", "gemini-3.1-flash-live-preview"],
                   "label": "Live model", "description": "Default Gemini Live model."},
    "voice": {"scope": "gemini", "type": "enum", "choices": sorted(_VOICE_NAMES), "default": "Puck",
              "label": "Voice", "description": "Gemini's speaking voice (30 HD prebuilt voices)."},
    "elevenlabs_model": {"scope": "elevenlabs", "type": "enum", "choices": ["eleven_realtime_v1_mini", "eleven_realtime_v1_max"],
                         "default": "eleven_realtime_v1_mini",
                         "label": "ElevenLabs model", "description": "Mini = fastest & cheapest. Max = deeper reasoning + tool calling."},
    "elevenlabs_voice": {"scope": "elevenlabs", "type": "enum", "default": _ELEVEN_DEFAULT_VOICE,
                         "label": "ElevenLabs voice", "description": "Voice from your ElevenLabs library (fetched live from your account)."},
    "elevenlabs_language": {"scope": "elevenlabs", "type": "enum", "choices": ["auto"] + _ELEVEN_LANGS, "default": "auto",
                            "label": "ElevenLabs language", "description": "Transcription language hint. Auto-detect usually works; pick one for short or accented speech."},
    "primary_language": {"scope": "gemini", "type": "enum", "choices": sorted(_LANG_CODES), "default": "id-ID",
                         "label": "Primary language", "description": "Language you speak most — reduces wrong-language detection on short prompts."},
    "secondary_language": {"scope": "gemini", "type": "enum", "choices": ["none"] + sorted(_LANG_CODES), "default": "en-US",
                           "label": "Second language", "description": "Optional second hint — biases recognition when you code-switch mid-sentence."},
    "smart_transcription": {"scope": "gemini", "type": "bool", "default": True,
                            "label": "Smart transcription", "description": "Removes filler words, fixes false starts, and formats the transcript."},
    "spoken_answers": {"scope": "common", "type": "enum", "choices": ["full", "brief"], "default": "full",
                       "label": "Spoken answers", "description": "How much of the agent's answer gets spoken when a delegation finishes."},
    "show_transcript": {"scope": "common", "type": "bool", "default": True,
                        "label": "Show transcript", "description": "Display the conversation text inside the mini-bar popover."},
    "show_log": {"scope": "common", "type": "bool", "default": True,
                 "label": "Show diagnostic log", "description": "Display the timestamped event log inside the popover."},
}


def _stored_settings() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins import _plugin_settings_entry
        entry = _plugin_settings_entry(load_config_readonly() or {}, _PLUGIN_NAME) or {}
        st = entry.get("settings") or {}
    except Exception:  # noqa: BLE001
        st = {}
    return {k: st.get(k, spec["default"]) for k, spec in _SETTINGS_SPEC.items()}


@router.get("/settings")
async def settings_get() -> dict:
    fields = {k: {"label": s["label"], "description": s["description"],
                  "type": s["type"], "choices": s.get("choices"), "scope": s.get("scope", "common")}
              for k, s in _SETTINGS_SPEC.items()}
    voices = _eleven_voices()
    if voices:
        fields["elevenlabs_voice"]["choices"] = [v["id"] for v in voices]
        fields["elevenlabs_voice"]["options"] = [
            {"value": v["id"], "label": f'{v["name"]} ({v["category"]})' if v.get("category") else v["name"]}
            for v in voices]
    values = _stored_settings()
    values["elevenlabs_voice"] = _resolve_eleven_voice(values.get("elevenlabs_voice", ""))
    return {"values": values, "fields": fields}


@router.post("/settings")
async def settings_post(body: dict | None = None) -> dict:
    values = (body or {}).get("values") or {}
    if not isinstance(values, dict):
        return {"ok": False, "error": "values must be an object"}
    from hermes_cli.plugins_state import save_plugin_setting
    try:
        for key, value in values.items():
            spec = _SETTINGS_SPEC.get(key)
            if spec is None:
                return {"ok": False, "error": f"unknown setting {key!r}"}
            if spec["type"] == "bool":
                if not isinstance(value, bool):
                    return {"ok": False, "error": f"{key} must be a boolean"}
            elif spec["type"] == "enum" and value not in (spec.get("choices") or []):
                if key == "elevenlabs_voice":          # live account list, else any id
                    known = [v["id"] for v in _eleven_voices()]
                    if known and value not in known:
                        return {"ok": False, "error": "unknown ElevenLabs voice id"}
                else:
                    return {"ok": False, "error": f"{key} must be one of the declared choices"}
            save_plugin_setting(_PLUGIN_NAME, (key,), value)
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"save failed: {e}"}
    return {"ok": True, "values": _stored_settings()}


# ---- ElevenLabs Realtime adapter -------------------------------------------
# Verified against the live API (Nov 2026): 24 kHz PCM in/out, semantic VAD,
# tool calls surface as response.function_call_arguments.done, input transcripts
# as conversation.item.input_audio_transcription.completed. The REST surface
# above is unchanged — the renderer never learns which provider is upstream.
EL_HOST = "wss://api.elevenlabs.io/v1/convai/realtime"
# Expressive audio tags the TTS speaks but never says literally: [calm], [friendly], [laughs]
_EL_TAG = re.compile(r"\[[A-Za-z][A-Za-z ,\-]{0,24}\]")
EL_MINT = "https://api.elevenlabs.io/v1/convai/realtime/client_secrets"


def _env_value(name: str) -> str:
    """Process env first, then the Hermes .env files (launch home + ~/.hermes)."""
    v = os.environ.get(name, "")
    if v:
        return v
    homes = []
    if os.environ.get("HERMES_HOME"):
        homes.append(os.environ["HERMES_HOME"])
    homes.append(os.path.join(os.path.expanduser("~"), ".hermes"))
    for h in homes:
        try:
            with open(os.path.join(h, ".env"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(name + "="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _eleven_key() -> str:
    return _env_value("ELEVENLABS_API_KEY")



_voices_cache: dict = {"at": 0.0, "list": []}


def _eleven_voices() -> list:
    """Account voices: [{id, name, category}]. Cached 10 min; [] on failure."""
    now = time.time()
    if _voices_cache["list"] and now - _voices_cache["at"] < 600:
        return _voices_cache["list"]
    key = _eleven_key()
    if not key:
        return _voices_cache["list"]
    try:
        req = urllib.request.Request("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        out = [{"id": v.get("voice_id", ""), "name": v.get("name", ""), "category": v.get("category", "")}
               for v in data.get("voices", []) if v.get("voice_id")]
        _voices_cache.update({"at": now, "list": out})
        return out
    except Exception:  # noqa: BLE001 — a voice fetch must never block settings
        return _voices_cache["list"]


def _resolve_eleven_voice(stored: str) -> str:
    """Stored value may be a voice id, a name (legacy 'brian'), or empty."""
    voices = _eleven_voices()
    ids = {v["id"] for v in voices}
    if stored and stored in ids:
        return stored
    low = (stored or "").strip().lower()
    if low:
        for v in voices:
            if v["name"].lower().startswith(low) or low in v["name"].lower():
                return v["id"]
    for v in voices:
        if v["name"].lower().startswith("brian"):
            return v["id"]
    return _ELEVEN_DEFAULT_VOICE


def _resample_16_to_24(pcm: bytes) -> bytes:
    """Linear-interpolate 16 kHz PCM16 mono up to 24 kHz (speech-safe)."""
    n = len(pcm) // 2
    if not n:
        return b""
    src = array("h")
    src.frombytes(pcm[: n * 2])
    out_len = int(n * 1.5)
    out = array("h", bytes(2 * out_len))
    step = 2.0 / 3.0
    for i in range(out_len):
        pos = i * step
        j = int(pos)
        a = src[j] if j < n else src[n - 1]
        b = src[j + 1] if j + 1 < n else a
        out[i] = int(a + (b - a) * (pos - j))
    return out.tobytes()


def _eleven_tools() -> list:
    return [{"type": "function", "name": "talk",
            "description": "Delegate real work (web, files, tools, current data) to the Hermes agent session. "
                           "Use it for anything needing live information or actions.",
            "parameters": {"type": "object",
                           "properties": {"request": {"type": "string",
                                          "description": "The user's request, self-contained."}},
                           "required": ["request"]}},
            {"type": "function", "name": "recall",
             "description": "ALWAYS call this before saying you do not know or do not remember something "
                            "about earlier in this conversation. Searches the Hermes session behind this call. "
                            "Pass short keywords. Read-only.",
             "parameters": {"type": "object",
                            "properties": {"query": {"type": "string",
                                           "description": "Short keywords, e.g. 'relay result'."}},
                            "required": ["query"]}}]


def _eleven_mint(model: str, voice: str, language: str = "auto", context: str = "") -> dict:
    body = {"session": {
        "type": "realtime",
        "model": model,
        "instructions": LIVE_PERSONA + _speak_directive() + RECALL_HINT + (
            ("\n\n" + "BACKGROUND REFERENCE NOTES (not messages - do not answer them, do not greet, "
             "wait for the user to speak first; use them only when the user asks about earlier work):"
             + "\n" + context) if context else ""),
        "output_modalities": ["audio"],
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": 24000},
                       "turn_detection": {"type": "semantic_vad", "eagerness": "medium"},
                       **({"transcription": {"language": language}} if language and language != "auto" else {})},
            "output": {"format": {"type": "audio/pcm"}, "voice": voice},
        },
        "tools": _eleven_tools(),
        "max_duration_seconds": 3600,
    }, "expires_after": {"anchor": "created_at", "seconds": 3600}}
    req = urllib.request.Request(
        EL_MINT, data=json.dumps(body).encode(),
        headers={"xi-api-key": _eleven_key(), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


async def _eleven_reader(ws) -> None:
    """Pump ElevenLabs GA events into the same state lists the Gemini reader fills."""
    out_pend = ""   # partial transcript held back while a [tag] is incomplete
    try:
        while True:
            frame = await ws.recv()
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8", "replace")
            ev = json.loads(frame)
            t = ev.get("type", "")
            if t == "response.created":
                _state["responding"] = True
            elif t in ("response.done", "error"):
                _state["responding"] = False
            if t == "conversation.item.input_audio_transcription.completed":
                txt = (ev.get("transcript") or "").strip()
                if txt:
                    _state["input"].append(txt)
                    _state["input"].append("—")
            elif t == "response.output_audio.delta":
                d = ev.get("delta")
                if d:
                    _state["audio"].append(d)
            elif t == "response.output_audio_transcript.delta":
                # Stream the text as it is generated (the renderer merges
                # consecutive assistant fragments), stripping expressive tags
                # like [calm] that are spoken but not literally said.
                raw = ev.get("delta", "") or ""
                if raw:
                    out_pend += raw
                    hold = out_pend.rfind("[")
                    if hold != -1 and "]" not in out_pend[hold:]:
                        emit, out_pend = out_pend[:hold], out_pend[hold:]
                    else:
                        emit, out_pend = out_pend, ""
                    emit = re.sub(r"\s{2,}", " ", _EL_TAG.sub("", emit))
                    if emit:
                        _state["output"].append(emit)
            elif t == "response.output_audio_transcript.done":
                tail = re.sub(r"\s{2,}", " ", _EL_TAG.sub("", out_pend)).strip()
                out_pend = ""
                if tail:
                    _state["output"].append(tail)
                if _state["output"] and _state["output"][-1] != "—":
                    _state["output"].append("—")
            elif t == "response.function_call_arguments.done":
                raw = ev.get("arguments") or "{}"
                try:
                    args = json.loads(raw)
                except Exception:  # noqa: BLE001 — non-JSON args fall back to raw text
                    args = {"request": raw}
                call_id = ev.get("call_id", "")
                if ev.get("name", "") == "recall":
                    query = str(args.get("query", "")).strip()
                    if query:
                        _state["recalls"].append({"id": call_id, "query": query[:300]})
                else:
                    request = str(args.get("request", "")).strip()
                    if request:
                        _state["delegations"].append({"id": call_id, "request": request[:2000]})
            # No manual barge-in bump here: ElevenLabs' semantic VAD already
            # cancels its own response on real speech (interrupt_response), and
            # the renderer flushes on a genuine mic peak. Counting every
            # speech_started made speaker echo (model audio -> mic) look like
            # an interruption, which re-answered the same turn in a loop.
            elif t == "error":
                err = ev.get("error") or {}
                _state["error"] = str(err.get("message") or err)[:200]
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — any upstream drop surfaces in /poll
        name = type(exc).__name__
        if name.startswith("ConnectionClosed"):
            # clean/normal close (max duration, client secret expiry, idle) —
            # not an error the user caused; keep the message actionable.
            _state["error"] = "Session ended — press Start to talk again"
        else:
            _state["error"] = f"upstream closed: {exc}"[:200]
    finally:
        ka = _state.get("keepalive")
        if ka:
            ka.cancel()
            _state["keepalive"] = None
        _state["session"] = None


async def _start_eleven(body: dict | None = None) -> dict:
    import websockets
    key = _eleven_key()
    if not key:
        return {"ok": False, "error": "ELEVENLABS_API_KEY not set in the environment or ~/.hermes/.env"}
    s = _load_settings()
    model = s["elevenlabs_model"]
    voice = _resolve_eleven_voice(s["elevenlabs_voice"])
    language = s["elevenlabs_language"]
    ctx_text = str((body or {}).get("session_context") or "").strip()[:6000]
    await _cleanup()
    _state.update({"session": None, "ws": None, "model": "", "provider": "", "input": [], "output": [], "audio": [],
                   "error": "", "usage": None, "interaction": "", "delegations": [], "recalls": [],
                   "tool_in_flight": None, "interrupted": 0, "resumed": False, "responding": False})
    try:
        minted = await asyncio.to_thread(_eleven_mint, model, voice, language, ctx_text)
        secret = minted.get("value") or ""
        if not secret:
            return {"ok": False, "error": "mint failed: no client secret returned"}
        ws = await websockets.connect(f"{EL_HOST}?model={model}",
                                      additional_headers={"Authorization": f"Bearer {secret}"},
                                      open_timeout=15, close_timeout=5)
        try:
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                t = ev.get("type", "")
                if t in ("session.created", "session.updated"):
                    break
                if t == "error":
                    err = ev.get("error") or {}
                    await ws.close()
                    return {"ok": False, "error": str(err.get("message") or err)[:200]}
        except asyncio.TimeoutError:
            await ws.close()
            return {"ok": False, "error": "ElevenLabs session setup timeout"}
        _state["ws"] = ws
        _state["model"] = model
        _state["provider"] = "elevenlabs"
        _state["session"] = "live"
        _state["manual"] = False
        _state["task"] = asyncio.create_task(_eleven_reader(ws))

        async def keepalive():
            # ElevenLabs closes sessions that hear nothing from the client for
            # a while; an empty append is a no-op frame that keeps it open.
            try:
                while _state.get("ws") is ws and _state.get("session") == "live":
                    await asyncio.sleep(40)
                    if _state.get("ws") is not ws or _state.get("session") != "live":
                        return
                    await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": ""}))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the reader reports the real state
                return
        _state["keepalive"] = asyncio.create_task(keepalive())
        return {"ok": True, "model": model, "resumed": False,
                "speak": s["spoken_answers"], "voice": voice, "provider": "elevenlabs"}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:160]
        return {"ok": False, "error": f"mint failed: HTTP {e.code} {detail}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


@router.post("/start")
async def start(body: dict | None = None) -> dict:
    if _load_settings()["provider"] == "elevenlabs":
        return await _start_eleven(body)
    if not _api_key():
        return {"ok": False, "error": "GOOGLE_API_KEY not set in ~/.hermes/.env"}
    await _cleanup()
    _state.update({"session": None, "ws": None, "model": "", "provider": "", "input": [], "output": [], "audio": [], "error": "", "usage": None, "interaction": "", "delegations": [], "recalls": [], "tool_in_flight": None, "interrupted": 0, "responding": False})
    import websockets

    # The model comes from the plugin's settings — the Settings → Plugins form
    # and the minibar picker both write the same value (single source of truth).
    candidates = _candidates()
    wanted = _load_settings()["model"]
    want_resume = bool((body or {}).get("resume"))
    if not (body or {}).get("_auto"):
        _state["redials"] = 0          # user-initiated start resets the redial budget
    if wanted:
        wanted_full = wanted if wanted.startswith("models/") else f"models/{wanted}"
        candidates = [wanted_full] + [m for m in candidates if m != wanted_full]

    last_err = ""
    skip_handle: set[str] = set()
    for model in candidates:
        try:
            name = await asyncio.to_thread(_mint_token)
            ws = await websockets.connect(f"{WS_HOST}?access_token={name}", open_timeout=10, close_timeout=5)
            gen_cfg = {"responseModalities": ["AUDIO"],
                       "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": _load_settings()["voice"]}}}}
            # Session resumption: first attempt may reuse the stored handle; any
            # retry falls back to a fresh session (expired handles are rejected).
            setup: dict = {
                "model": model,
                "generationConfig": gen_cfg,
                "inputAudioTranscription": _transcription_config(),
                "outputAudioTranscription": {},
                "sessionResumption": {},
                "systemInstruction": {"parts": [{"text": _persona_with_context(body)}]},
                "tools": [{"functionDeclarations": [TALK_TOOL, RECALL_TOOL]}],
            }
            resumed_now = False
            if want_resume and model not in skip_handle:
                h = _load_resume()
                if h.get("handle") and h.get("model") == model:
                    setup["sessionResumption"] = {"handle": h["handle"]}
                    resumed_now = True
            await ws.send(json.dumps({"setup": setup}))
            setup_ok = False
            try:
                while True:
                    frame = await asyncio.wait_for(ws.recv(), timeout=8)
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8", "replace")
                    msg = json.loads(frame)
                    sru = msg.get("sessionResumptionUpdate")
                    if sru and sru.get("newHandle") and sru.get("resumable"):
                        _state["handle"] = sru["newHandle"]
                        _save_resume(sru["newHandle"], model)
                    if "setupComplete" in msg:
                        setup_ok = True
                        break
                    if "error" in msg:
                        last_err = str(msg["error"].get("message") or msg["error"])[:160]
                        break
            except asyncio.TimeoutError:
                last_err = "setup timeout (8s)"
            if not setup_ok:
                await ws.close()
                if resumed_now:
                    # the stored handle was rejected (expired) — retry this same
                    # model once with a brand-new session before moving on
                    skip_handle.add(model)
                    candidates.insert(candidates.index(model) + 1, model)
                continue
            _state["ws"] = ws
            _state["model"] = model
            _state["session"] = "live"
            _state["manual"] = False
            _state["resumed"] = resumed_now and setup_ok
            _state["provider"] = "gemini"
            _state["task"] = asyncio.create_task(_reader(ws))
            return {"ok": True, "model": model, "resumed": _state["resumed"], "speak": _load_settings()["spoken_answers"], "voice": _load_settings()["voice"], "provider": _load_settings()["provider"]}
        except Exception as exc:  # noqa: BLE001 — try the next candidate
            last_err = f"{type(exc).__name__}: {exc}"[:160]
    return {"ok": False, "error": f"all candidates failed. Last error: {last_err}"}


@router.post("/audio")
async def audio(body: dict) -> dict:
    ws = _state.get("ws")
    if _state["session"] != "live" or ws is None:
        return {"ok": False, "error": _state["error"] or "no session"}
    data = body.get("data", "")
    if not data:
        return {"ok": True, "skipped": True}
    try:
        if _state.get("provider") == "elevenlabs":
            # Client captures 16 kHz; ElevenLabs requires 24 kHz PCM16.
            up = _resample_16_to_24(base64.b64decode(data))
            if up:
                await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                          "audio": base64.b64encode(up).decode()}))
            return {"ok": True}
        await ws.send(json.dumps({"realtimeInput": {"audio": {"data": data, "mimeType": "audio/pcm;rate=16000"}}}))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"send failed: {exc}"[:160]
        _state["session"] = None
        return {"ok": False, "error": _state["error"]}


@router.get("/poll")
async def poll(in_offset: int = 0, out_offset: int = 0, a_offset: int = 0, d_offset: int = 0, r_offset: int = 0) -> dict:
    return {
        "session": _state["session"],
        "model": _state["model"],
        "error": _state["error"],
        "interaction": _state["interaction"],
        "usage": _state["usage"],
        "resumed": _state["resumed"],
        "input": _state["input"][in_offset:],
        "output": _state["output"][out_offset:],
        "audio": _state["audio"][a_offset:],
        "in_total": len(_state["input"]),
        "out_total": len(_state["output"]),
        "a_total": len(_state["audio"]),
        "delegations": _state["delegations"][d_offset:],
        "d_total": len(_state["delegations"]),
        "recalls": _state["recalls"][r_offset:],
        "r_total": len(_state["recalls"]),
        "tool_in_flight": _state["tool_in_flight"],
        "interrupted": _state["interrupted"],
        "model_setting": (_load_settings()["elevenlabs_model"] if _state.get("provider") == "elevenlabs" else _load_settings()["model"].replace("models/", "")),
        "show_transcript": _load_settings()["show_transcript"],
        "show_log": _load_settings()["show_log"],
    }


@router.post("/tool_response")
async def tool_response(body: dict) -> dict:
    """Renderer answers an talk delegation with the agent's reply.

    Forwards the toolResponse upstream so the voice model speaks it. The
    renderer submits the delegated request to the dedicated 'Livetalk'
    session (prompt.submit), polls the submission receipt until it settles,
    and passes the agent's final answer here."""
    ws = _state.get("ws")
    if _state["session"] != "live" or ws is None:
        return {"ok": False, "error": _state["error"] or "no session"}
    call_id = str(body.get("id", ""))
    answer = str(body.get("answer", ""))[:8000]
    if not call_id:
        return {"ok": False, "error": "missing tool call id"}
    _state["tool_in_flight"] = None
    try:
        if _state.get("provider") == "elevenlabs":
            # A response may still be streaming (fast agent answers): the
            # server drops response.create while one is active — wait it out.
            for _ in range(50):
                if not _state.get("responding"):
                    break
                await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call_id,
                "output": answer or "The agent returned no answer."}}))
            # The server applies the item asynchronously; response.create sent
            # back-to-back gets dropped (verified: no speech without the gap).
            await asyncio.sleep(0.4)
            await ws.send(json.dumps({"type": "response.create"}))
            return {"ok": True}
        response: dict = {"output": answer or "The agent returned no answer."}
        await ws.send(json.dumps({
            "toolResponse": {
                "functionResponses": [{
                    "id": call_id,
                    "response": response,
                }],
            }
        }))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"tool_response send failed: {exc}"[:160]
        _state["session"] = None
        return {"ok": False, "error": _state["error"]}


async def _cleanup() -> None:
    _state["manual"] = True   # user close -> block auto-redial
    task = _state.get("task")
    if task:
        task.cancel()
    ws = _state.get("ws")
    if ws is not None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    ka = _state.get("keepalive")
    if ka:
        ka.cancel()
    _state["keepalive"] = None
    _state["ws"] = None
    _state["task"] = None


@router.post("/close")
async def close() -> dict:
    await _cleanup()
    _state["session"] = None
    return {"ok": True}


@router.get("/status")
async def status() -> dict:
    return {"session": _state["session"] or "idle", "model": _state["model"], "api_key_configured": bool(_api_key())}
