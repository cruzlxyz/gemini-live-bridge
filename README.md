<p align="center"><img src="assets/cover.jpg" alt="Livetalk — talk with your Hermes agent" width="720"></p>

# Livetalk

<p align="center"><img src="assets/livetalk.svg" alt="The Livetalk face-ghost — a liquid blob that blinks and speaks" width="150"></p>

<p align="center"><b>Talk to it. It talks back — and when real work is needed, the Hermes agent does it.</b></p>

<p align="center"><a href="docs/demo.html">✨ See the face-ghost move (live demo)</a></p>

> **What's Hermes?** [Hermes Agent](https://github.com/NousResearch/hermes-agent) is the open-source AI agent by [Nous Research](https://nousresearch.com) that lives in your desktop app — it chats, runs tools, browses the web, remembers things, and automates your work. This plugin gives it a voice you can talk to.

A composer mini-bar for the Hermes desktop app: a full-duplex voice session in one small popover. Small talk is answered by the voice model itself; anything that needs facts, your machine, or tools is handed to a dedicated Hermes agent session, and the answer is spoken back to you.

> The renderer never talks to Google or ElevenLabs. A local Python backend owns the upstream WebSocket and your API key; the desktop UI only exchanges data with `localhost`.

---

## Two voice providers

| | **Gemini Live** (Google) | **ElevenLabs Realtime** |
|---|---|---|
| Models | `gemini-3.8-live`, `gemini-2.5-flash-native-audio-latest`, `…-preview-12-2025`, `…-preview-09-2025`, `gemini-3.1-flash-live-preview` | `eleven_realtime_v1_mini`, `eleven_realtime_v1_max` |
| Voices | 30 HD prebuilt (Puck, Aoede, Kore, Leda, …) | every voice on your account (17k+ library) |
| Languages | any documented BCP-47 pair (primary + secondary) | auto-detected, 90+ languages incl. Indonesian & Javanese |
| Transcript | streaming, word by word | streaming; expressive tags (`[calm]`, `[laughs]`) stripped so the text matches the speech |
| Key | `GOOGLE_API_KEY` | `ELEVENLABS_API_KEY` |

Pick the provider in **Settings → Plugins → livetalk**. Each provider keeps its own model/voice/language fields — switching never mixes them. Tools (`talk`, `recall`), barge-in, transcript and the token meter behave the same on both.

## What you get

- **Voice ↔ Hermes delegation** — greetings and general knowledge are answered by the voice model; anything needing live data, your machine, or tools is delegated via a `talk` call to a dedicated **"Livetalk" agent session**, and the agent's final answer is spoken aloud.
- **Session memory (`recall`)** — a second tool lets the voice model search earlier messages of the very same Hermes session, so "what did we talk about yesterday?" is answered from real history instead of a guess.
- **Warm start** — every session opens with the last five messages plus a compact topic digest (your recent requests and what came of them), so continuity survives app restarts.
- **One persistent session** — the agent session id is remembered (`ctx.storage`) and matched by title, so the conversation is never re-created behind your back.
- **Full-duplex + barge-in** — talk over the model mid-sentence. The client cuts its playback queue immediately; the server-side interruption path covers the mid-generation case.
- **Liquid blob indicator** — one animated blob instead of text: violet and swelling with your voice while listening, green while the model speaks, spinning teal while the agent works. Click it to start/stop.
- **Live transcript** — both sides rendered as flowing sentences (streaming fragments merged per speaker). Toggleable.
- **Session token meter** — real `usageMetadata` from Google, shown as `12.3k tok`, reset per session.
- **Session resumption + auto-redial** — a network drop reconnects with the resumption handle, keeping context. A manual stop never reconnects.
- **Secure by design** — API keys live in `~/.hermes/.env` and never leave the backend process. The UI only receives short-lived session tokens.

## Requirements

| | |
|---|---|
| **Hermes** | Desktop app with plugin support; **tested on v0.23.3** (`requires_hermes: >=0.22.0`). The settings page needs a build with `registerSettingsPage` (~0.23+). |
| **API key** | One of: `GOOGLE_API_KEY` ([AI Studio](https://aistudio.google.com/apikey), Live API access) or `ELEVENLABS_API_KEY` ([elevenlabs.io](https://elevenlabs.io), Realtime Conversations) — in `~/.hermes/.env` |
| **Python** | Nothing extra — the backend uses only Hermes-bundled packages (`fastapi`, `websockets`) |
| **Hardware** | Microphone + speakers/headphones (headphones strongly recommended — see Tips) |
| **OS** | Windows / macOS / Linux desktop |

## Install

```bash
hermes plugins install cruzlxyz/livetalk
```

This installs both halves (the Python dashboard backend and the desktop mini-bar), then restart the desktop app once.

**One-time setup** — put at least one key in `~/.hermes/.env`:

```
GOOGLE_API_KEY=AIza...
ELEVENLABS_API_KEY=sk_...
```

Then click the blob in the status bar and just talk.

## Usage

- **Click the blob** to start; click again to stop.
- **Just talk.** Examples of how turns route:

| You say | What happens |
|---|---|
| "What is Hermes anyway?" | The voice model answers itself — general knowledge |
| "What's the BTC price right now?" | Delegated (`talk`) — the agent searches the live web, the answer is spoken |
| "Check how many files are in my Downloads folder" | Delegated — the agent runs tools on your machine |
| "What did we talk about yesterday?" | `recall` — searches this session's earlier messages |
| "Wait, how many percent did you say?" | Answered from context — no re-search |

- **Blob colors**: violet = listening · green = speaking · spinning teal = Hermes is on it · gray = idle.

<p align="center"><img src="assets/blob.svg" width="600" alt="The blob's three states: listening (violet), speaking (green, echoing), thinking (teal, spinning ring)"></p>

- Use **headphones**: without them the microphone re-captures the model's voice, which is the #1 cause of chaotic replies in every voice app.
- Google caps Live audio sessions (~15 min); the backend auto-resumes on network drops.

## Settings (Settings → Plugins → livetalk)

One page, provider-aware — only the active provider's fields are shown.

| Setting | Scope | Values | Default |
|---|---|---|---|
| Provider | common | `gemini` / `elevenlabs` | `gemini` |
| Live model | Gemini | the five Live models above | `gemini-3.8-live` |
| Voice | Gemini | 30 HD prebuilt voices | `Puck` |
| Primary language | Gemini | any documented BCP-47 code | `id-ID` |
| Second language | Gemini | `none` or any BCP-47 code (code-switching) | `en-US` |
| ElevenLabs model | ElevenLabs | `eleven_realtime_v1_mini` / `_max` | `_mini` |
| ElevenLabs voice | ElevenLabs | every voice on your account | `brian` |
| ElevenLabs language | ElevenLabs | `auto` or a fixed language | `auto` |
| Smart transcription | common | on/off — removes fillers, fixes false starts | on |
| Spoken answers | common | `full` / `brief` (2–4 sentences) | `full` |
| Show transcript | common | show the popover conversation text | on |
| Show diagnostic log | common | show the timestamped event log | on |

## What this plugin touches

- **Dashboard API** (`dashboard/plugin_api.py`): FastAPI routes under `/api/plugins/livetalk/*` — session proxy for both providers, delegation + recall queues, settings read/write.
- **Desktop surface** (`desktop/plugin.js`): one composer blob + popover (blob control, token counter, transcript, log), mic capture, playback queue, delegation worker.
- **One agent session**: creates and reuses a Hermes session titled "Livetalk" for all delegations. No agent tools, no hooks, no middleware, no core overrides.

### API routes

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/config` | `{ model, candidates[], show_transcript, show_log }` — model catalog + toggles |
| `GET` | `/settings` | Settings page data: `{ values, fields }` for the active provider |
| `POST` | `/settings` | Persist settings (per-provider keys kept apart) |
| `POST` | `/start` | Dial the active provider → live session. Returns `{ ok, model, provider, resumed, speak }` |
| `POST` | `/audio` | Forward one mic chunk upstream (`{ data: <base64 PCM16 mono 16kHz> }`) |
| `GET` | `/poll` | Incremental state: transcriptions, audio, delegations, recalls, usage, barge-in counter — all offset-based |
| `POST` | `/tool_response` | Answer a queued `talk` delegation or a `recall` lookup |
| `POST` | `/close` | Tear down the session (blocks auto-redial) |
| `GET` | `/status` | Liveness probe |

## How it works

Three flows, one router — the voice model decides per turn:

1. **Small talk / general knowledge** → answered directly over the audio channel.
2. **Real work** → the model emits `talk(request)` → the renderer submits it to the dedicated agent session (`prompt.submit`, surface `voice-live`, with recent spoken context) → polls the submission receipt until it settles → the agent's `final_response` is fed back as the tool response → spoken aloud.
3. **Session memory** → the model emits `recall(query)` → the renderer searches this session's history → the matching messages are returned as the tool response → answered from them.

Full details and diagrams: [docs/WORKFLOW.md](docs/WORKFLOW.md).

```
┌─ Hermes desktop (Electron renderer) ────────────┐
│  composer blob + popover (plugin.js)            │
│  mic capture (WebAudio PCM16/16k)               │
│  playback + output level analyser (24k)         │
│  delegation queue + recall lookup               │
└──────────────┬──────────────────────────────────┘
               │ ctx.rest() → localhost only
┌──────────────▼──────────────────────────────────┐
│  Backend (plugin_api.py, FastAPI router)        │
│  • owns the upstream WebSocket (either provider)│
│  • Gemini: mints 1-use ephemeral tokens         │
│  • ElevenLabs: mints a realtime client session  │
│  • queues talk + recall calls                   │
│  • reads/writes the plugin settings page        │
└───────┬────────────────────────┬────────────────┘
        │ wss://…                │ prompt.submit / prompt.receipt
        ▼                        ▼
  Gemini Live / ElevenLabs   Hermes agent session
  Realtime (v1alpha)         "Livetalk" (full toolset)
```

## Compatibility notes

- **Settings page** needs a Hermes build with plugin settings pages (~0.23+). Older builds: everything works, the page just does not appear.
- **`gemini-3.8-live-extended-thinking` is deliberately not offered** — verified live, that model never emits tool calls, so it cannot delegate or recall. The picker only lists models that can do real work.
- Transcription language hints follow the [Live API transcription docs](https://aistudio.google.com/docs/live-api/live-transcribe) (`languageCodes`, SMART mode, custom vocabulary).
- The backend reads the plugin settings store through two Hermes-internal helpers (`_plugin_settings_entry`, `save_plugin_setting`) because no public seam exists yet; every call is wrapped, so a future rename degrades to defaults instead of breaking a session.

## Privacy

Transcripts and audio exist only in memory while a session runs; the plugin writes no transcripts to disk. Your API keys never leave the backend process. On the provider side, the key owner's data-use policy applies.

## License

MIT — see [LICENSE](LICENSE).
