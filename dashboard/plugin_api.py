"""Gemini Live Bridge — Python backend: FULL PROXY architecture.

The renderer never talks to Google. This module owns the upstream Live
WebSocket (verified working from Python repeatedly) and exposes a simple
local REST surface:

    GET  /config         -> { model, candidates[] }        (no key material)
    POST /start          -> opens upstream session; { ok, model }
    POST /audio {data}   -> forwards one PCM16/16k chunk upstream
    GET  /poll           -> { session, model, input[], output[], audio[], error }
                            lists are INCREMENTAL: pass ?in=&out=&a= offsets
    POST /close          -> closes the session

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

DEFAULT_MODEL = "models/gemini-2.5-flash-native-audio-latest"
_DIR = os.path.dirname(__file__)
RESUME_FILE = os.path.join(_DIR, ".resume.json")   # last resumable session handle


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
}


def _api_key() -> str:
    return os.environ.get("GOOGLE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")


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
        req = urllib.request.Request(f"{LIST_MODELS_URL}?key={key}&pageSize=200", headers={"User-Agent": "hermes-gemini-live-bridge"})
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
                if sc.get("turnComplete"):
                    _state["input"].append("—")
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
    await start({"resume": True, "model": _state.get("model", ""), "memory": False, "_auto": True})


@router.get("/config")
async def config() -> dict:
    h = _load_resume()
    return {"model": _candidates()[0], "candidates": _candidates(),
            "resumable": bool(h.get("handle"))}


@router.post("/start")
async def start(body: dict | None = None) -> dict:
    if not _api_key():
        return {"ok": False, "error": "GOOGLE_API_KEY not set in ~/.hermes/.env"}
    await _cleanup()
    _state.update({"session": None, "ws": None, "model": "", "input": [], "output": [], "audio": [], "error": "", "usage": None, "interaction": ""})
    import websockets

    # Optional explicit model from the UI picker — tried FIRST, then the rest.
    candidates = _candidates()
    wanted = (body or {}).get("model")
    think_level = (body or {}).get("thinkingLevel")
    if think_level not in ("low", "high"):
        think_level = "low"
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
            gen_cfg = {"responseModalities": ["AUDIO"]}
            if "extended-thinking" in model:
                # Required for *-extended-thinking models; string levels accepted.
                gen_cfg["thinkingConfig"] = {"thinkingLevel": think_level}
            # Session resumption: first attempt may reuse the stored handle; any
            # retry falls back to a fresh session (expired handles are rejected).
            setup: dict = {
                "model": model,
                "generationConfig": gen_cfg,
                "inputAudioTranscription": {},
                "outputAudioTranscription": {},
                "sessionResumption": {},
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
            return {"ok": True, "model": model, "resumed": _state["resumed"]}
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
async def poll(in_offset: int = 0, out_offset: int = 0, a_offset: int = 0) -> dict:
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
    }


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
