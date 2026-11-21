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
import json
import os
import time
import urllib.request
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
    return {
        "model": model if isinstance(model, str) and model else defaults["model"],
        "primary_language": primary if primary in _LANG_CODES else defaults["primary_language"],
        "secondary_language": secondary if secondary in _LANG_CODES and secondary != primary else "none",
        "smart_transcription": st.get("smart_transcription") not in (False, "false", "off"),
        "spoken_answers": st.get("spoken_answers") if st.get("spoken_answers") in ("brief", "full") else defaults["spoken_answers"],
        "show_transcript": st.get("show_transcript") not in (False, "false", "off"),
        "show_log": st.get("show_log") not in (False, "false", "off"),
        "voice": st.get("voice") if st.get("voice") in _VOICE_NAMES else "Puck",
        "provider": st.get("provider") if st.get("provider") in ("gemini",) else "gemini",
    }


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


def _tool_decl(model: str) -> dict:
    """The talk declaration, shaped per model.

    Extended-thinking models REQUIRE behavior NON_BLOCKING (docs: live-api/thinking —
    synchronous blocking tools return an error there, and the async toolCall arrives only
    after the filler's turnComplete while interactionStatus stays IN_PROGRESS). Blocking
    is the default and proven on every other live model."""
    if "extended-thinking" in model:
        return {**TALK_TOOL, "behavior": "NON_BLOCKING"}
    return TALK_TOOL

# Per-turn note prepended to the delegated prompt (GPT-Live pattern): the text
# is a voice transcript and the agent's reply will be SPOKEN by the voice model.
VOICE_LIVE_TURN_NOTE = (
    "[Note: this message is a delegation from a live spoken conversation. The text is a voice "
    "transcript (it may contain mis-hearings, hesitations and later corrections; use the latest "
    "intent). Your reply will be spoken aloud by a voice model that paraphrases it: answer in plain "
    "conversational sentences, keep it short (a few sentences unless the user asked for detail), no "
    "markdown, no lists, no code blocks, no URLs read out character by character. Do the work with "
    "your tools as usual; only the final facts need to be spoken. Do not claim an action succeeded "
    "before it actually did.]"
)


def _load_resume() -> dict:
    try:
        with open(RESUME_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _save_resume(handle: str, model: str) -> None:
    try:
        with open(RESUME_FILE, "w", encoding="utf-8") as fh:
            json.dump({"handle": handle, "model": model, "t": time.time()}, fh)
    except Exception:  # noqa: BLE001
        pass
LIST_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
AUTH_TOKENS_URL = "https://generativelanguage.googleapis.com/v1beta/auth_tokens"
WS_HOST = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained"

_state: dict = {
    "session": None,     # 'live' | None
    "ws": None,          # upstream websocket
    "task": None,        # reader task
    "model": "",
    "input": [],         # input transcription pieces
    "output": [],        # output transcription pieces
    "audio": [],         # model audio chunks (b64 PCM16/24k)
    "error": "",
    "interaction": "",   # extended-thinking: IN_PROGRESS while reasoning in background
    "usage": None,       # latest usageMetadata from the server (token meter)
    "resumed": False,    # current session was resumed from a stored handle
    "manual": False,     # user called /close (blocks auto-redial)
    "redials": 0,        # auto-reconnect attempts for the current session
    "delegations": [],   # pending talk toolCalls from the model: [{id, request}]
    "tool_in_flight": None,  # {id, request} the renderer is currently answering
}


_ENV_KEYS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")


def _env_files() -> list[str]:
    """``.env`` files to consult, in order: ``$HERMES_HOME``, this plugin's own Hermes
    home (``<home>/plugins/<name>/dashboard/`` → ``<home>``), then ``~/.hermes``."""
    home = os.path.normpath(os.path.join(_DIR, os.pardir, os.pardir, os.pardir))
    roots = (os.environ.get("HERMES_HOME"), home, os.path.expanduser("~/.hermes"))
    return list(dict.fromkeys(os.path.join(r, ".env") for r in roots if r))


def _read_dotenv(path: str) -> dict[str, str]:
    """Minimal ``.env`` reader: one ``KEY=VALUE`` per line, ``#`` comments, optional
    ``export`` prefix and quotes. Stdlib only, same tolerance as python-dotenv."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                values[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _api_key() -> str:
    """The Google API key: the process environment first, then Hermes' ``.env`` files.

    The environment alone is not enough. Hermes loads only the *launch* home's
    ``.env`` into ``os.environ``; a named profile's ``.env``
    (``~/.hermes/profiles/<name>/.env``) deliberately stays out of the process
    environment — and that is exactly where Hermes' Providers page writes the key.
    Falling back to the files keeps the plugin working under a profile without
    asking anyone to move or duplicate their key.
    """
    for name in _ENV_KEYS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    for path in _env_files():
        env = _read_dotenv(path)
        for name in _ENV_KEYS:
            value = (env.get(name) or "").strip()
            if value and not value.startswith("op://"):
                return value
    return ""


def _mint_token() -> str:
    now = datetime.now(timezone.utc)
    body = json.dumps({
        "uses": 1,
        "expireTime": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newSessionExpireTime": (now + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }).encode()
    req = urllib.request.Request(AUTH_TOKENS_URL, data=body, headers={"x-goog-api-key": _api_key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())["name"]


def _candidates() -> list[str]:
    key = _api_key()
    if not key:
        return [DEFAULT_MODEL]
    try:
        req = urllib.request.Request(f"{LIST_MODELS_URL}?key={key}&pageSize=200", headers={"User-Agent": "hermes-livetalk"})
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
        # Fixed catalog order: 3.8-live -> extended-thinking -> native-audio
        # (latest, then newest preview first) -> the rest.
        def rank(n: str) -> tuple:
            if n == "models/gemini-3.8-live":
                return (0, 0)
            if "extended-thinking" in n:
                return (1, 0)
            if "native-audio-latest" in n:
                return (2, 0)
            if "native-audio-preview" in n:
                # newest first: parse the largest month digits (09-2025 < 12-2025)
                digits = [int(x) for x in n.split("preview-")[-1].replace("-2025", "").split("-") if x.isdigit()]
                return (3, -max(digits) if digits else 0)
            return (4, 0)

        names.sort(key=lambda n: (rank(n), n))
        return names
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
                    # The user started talking over the model (server-side VAD
                    # barge-in). Bump a monotonic counter so the renderer can
                    # drop its pending playback queue and stop mid-sentence.
                    _state["interrupted"] += 1
                if sc.get("turnComplete"):
                    _state["input"].append("—")
            tc = msg.get("toolCall")
            if tc:
                # The model delegated to talk — queue each function call
                # for the renderer, which submits it to the Hermes agent and
                # answers via /tool_response.
                for fc in tc.get("functionCalls", []):
                    if fc.get("name", "") == "talk":
                        args = fc.get("args") or {}
                        _state["delegations"].append({
                            "id": fc.get("id", ""),
                            "request": str(args.get("request", ""))[:2000],
                        })
            if msg.get("error"):
                _state["error"] = str(msg["error"].get("message") or msg["error"])[:200]
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — any upstream drop surfaces in /poll
        _state["error"] = f"upstream closed: {exc}"[:200]
    finally:
        _state["session"] = None
        # Auto-redial: unexpected drop (not user /close) with a valid handle ->
        # reconnect in the background so the conversation keeps its context.
        if not _state["manual"] and _state["redials"] < 2 and _state.get("handle"):
            _state["redials"] += 1
            asyncio.get_event_loop().create_task(_auto_resume())


async def _auto_resume() -> None:
    """Reconnect after an unexpected drop, reusing the stored handle."""
    await asyncio.sleep(1.5)
    if _state["session"] == "live" or _state["manual"]:
        return
    await start({"resume": True, "_auto": True})


@router.get("/config")
async def config() -> dict:
    h = _load_resume()
    s = _load_settings()
    return {"model": s["model"], "candidates": _candidates(), "resumable": bool(h.get("handle")),
            "show_transcript": s["show_transcript"], "show_log": s["show_log"]}


@router.post("/start")
async def start(body: dict | None = None) -> dict:
    if not _api_key():
        return {"ok": False, "error": "GOOGLE_API_KEY not set (looked in the environment and in: "
                                     + ", ".join(_env_files()) + ")"}
    await _cleanup()
    _state.update({"session": None, "ws": None, "model": "", "input": [], "output": [], "audio": [], "error": "", "usage": None, "interaction": "", "delegations": [], "tool_in_flight": None, "interrupted": 0})
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
            if "extended-thinking" in model:
                # Extended-thinking models REFUSE to connect without a thinking
                # level, so it is hardcoded (the level picker was removed — it
                # had no user-visible effect and no other model accepts it).
                gen_cfg["thinkingConfig"] = {"thinkingLevel": "low"}
            # Session resumption: first attempt may reuse the stored handle; any
            # retry falls back to a fresh session (expired handles are rejected).
            setup: dict = {
                "model": model,
                "generationConfig": gen_cfg,
                "inputAudioTranscription": _transcription_config(),
                "outputAudioTranscription": {},
                "sessionResumption": {},
                "systemInstruction": {"parts": [{"text": LIVE_PERSONA + _speak_directive()}]},
                "tools": [{"functionDeclarations": [_tool_decl(model)]}],
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
        await ws.send(json.dumps({"realtimeInput": {"audio": {"data": data, "mimeType": "audio/pcm;rate=16000"}}}))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"send failed: {exc}"[:160]
        _state["session"] = None
        return {"ok": False, "error": _state["error"]}


@router.get("/poll")
async def poll(in_offset: int = 0, out_offset: int = 0, a_offset: int = 0, d_offset: int = 0) -> dict:
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
        "tool_in_flight": _state["tool_in_flight"],
        "interrupted": _state["interrupted"],
        "model_setting": _load_settings()["model"].replace("models/", ""),
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
        response: dict = {"output": answer or "The agent returned no answer."}
        if "extended-thinking" in _state["model"]:
            # NON_BLOCKING contract (docs: live-api/thinking): scheduling INTERRUPT
            # tells the thinking model to deliver the result at once instead of
            # waiting for an idle moment in the conversation.
            response["scheduling"] = "INTERRUPT"
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
