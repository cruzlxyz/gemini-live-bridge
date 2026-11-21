/**
 * Livetalk — composer mini-bar + backend-proxy voice session
 * Save at: ~/.hermes/desktop-plugins/livetalk/plugin.js
 *
 * The renderer NEVER talks to Google. Mic is captured here, PCM chunks are
 * POSTed to the local backend, which owns the upstream Livetalk WebSocket
 * (ephemeral token, server-side key). Playback: poll the backend for new
 * model-audio chunks and play them via WebAudio.
 */

import { atom, Button, COMPOSER_AREAS, host, Popover, PopoverContent, PopoverTrigger, Tip, useValue } from '@hermes/plugin-sdk'
import { useState, useEffect, useRef } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'livetalk'

// Mirror of the backend's VOICE_LIVE_TURN_NOTE — prepended to every delegated
// prompt so the agent answers in spoken style (GPT-Live uses the same seam,
// surface 'voice-live'). The tail is chosen by the "Spoken answers" setting.
const VOICE_NOTE_HEAD = (
  '[Note: this message is a delegation from a live spoken conversation. The text is a voice ' +
  'transcript (it may contain mis-hearings, hesitations and later corrections; use the latest ' +
  'intent). Your reply will be spoken aloud by a voice model that paraphrases it: answer in plain ' +
  'conversational sentences, no markdown, no lists, no code blocks, no URLs read out character ' +
  'by character. Do the work with your tools as usual. Do not claim an action succeeded before ' +
  'it actually did. CONTEXT FIRST: this session keeps its history — if the request is a follow-up ' +
  'about something already answered here, or the voice context below already contains the fact, ' +
  'answer from that context instead of searching or re-running tools. Search only for what is ' +
  'genuinely missing or must be fresh.]'
)
const VOICE_NOTE_TAIL = {
  brief: ' Keep it short: two to four sentences with the key facts unless the user asked for detail — the voice model reads your reply aloud.',
  full: ' Speak the full answer: keep every key fact, number and conclusion (the voice model is told to deliver all of it), just written as spoken-style prose.',
}
const voiceNote = mode => VOICE_NOTE_HEAD + (VOICE_NOTE_TAIL[mode] || VOICE_NOTE_TAIL.full)

// Honest model labels: the extended-thinking variants never fire tool calls on
// the constrained endpoint (tested live — thinking consumes the turn), so they
// chat beautifully but cannot delegate. Say so right in the picker.
const prettyModel = n => n.replace(/^models\//, '') + (n.includes('extended-thinking') ? ' — no delegation' : '')

const CSS = `
.gemini-live-bar{display:flex;align-items:center;gap:2px;height:100%;color:var(--ui-text-tertiary)}
.gemini-live-panel{width:320px;max-width:calc(100vw - 24px);height:auto;overflow:visible}
.gemini-live-error{font-size:11px;line-height:15px;color:var(--ui-text-secondary);padding:6px 4px;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;max-height:76px;overflow-y:auto}
.gemini-live-transport{display:flex;align-items:center;gap:4px;padding:2px 2px 0}
.gemini-live-transport-space{flex:1}
.gemini-live-usage{font-size:10px;color:var(--ui-text-quaternary,var(--muted-foreground,#999));font-variant-numeric:tabular-nums}
.gemini-live-log{max-height:102px;overflow-y:auto;font-family:var(--font-mono,monospace);font-size:10px;line-height:14px;color:var(--ui-text-quaternary);padding:4px;margin-top:6px;border-top:1px solid var(--ui-stroke-secondary);white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
.gemini-live-log>div:hover{color:var(--ui-text-secondary)}




/* ---- full-page visualizer (/livetalk — the Scheduled-jobs-style overlay) ---- */
.glb-viz-backdrop{position:fixed;inset:0;z-index:80;display:flex;align-items:center;justify-content:center;background:rgba(8,8,14,.62);backdrop-filter:blur(6px)}
.glb-viz-card{position:relative;width:min(720px,80vw);height:min(480px,76vh);border-radius:14px;border:1px solid var(--ui-stroke-secondary,rgba(255,255,255,.09));background:var(--ui-surface,#12121a);box-shadow:0 24px 70px rgba(0,0,0,.55);overflow:hidden}
.glb-viz-breathe{animation:glb-viz-breathe 4.6s ease-in-out infinite}
@keyframes glb-viz-breathe{0%,100%{transform:scale(1)}50%{transform:scale(1.025)}}
.glb-viz-eyes{position:absolute;inset:0}
.glb-viz-eye{position:absolute;top:34%;width:8%;height:13%;border-radius:50%;background:rgba(12,12,22,.85);transition:height .09s ease, top .09s ease}
.glb-viz-eye-l{left:31%}
.glb-viz-eye-r{right:31%}
.glb-blink .glb-viz-eye{height:2.5%;top:40%}
.glb-viz-mouth{position:absolute;left:50%;top:60%;translate:-50% 0;width:26%;height:9%;border-radius:2px;background:rgba(12,12,22,.75);transform-origin:center;transition:opacity .25s ease, transform .09s ease}
.glb-viz-head{position:absolute;top:0;left:0;right:0;display:flex;align-items:center;justify-content:space-between;padding:14px 18px;z-index:3}
.glb-viz-center{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:2;pointer-events:none}
.glb-viz-foot{position:absolute;left:0;right:0;bottom:0;display:flex;flex-direction:column;gap:8px;padding:14px 18px;z-index:3}
.glb-viz-legend{display:flex;gap:14px;font-size:10.5px;color:var(--ui-text-quaternary)}
.glb-viz-tip{position:absolute;display:none;max-width:260px;font-size:11px;line-height:1.4;padding:6px 9px;border-radius:8px;border:1px solid var(--ui-stroke-secondary);background:rgba(18,18,26,.95);color:var(--ui-text-secondary);z-index:4;pointer-events:none;word-break:break-word}
.glb-viz-head{display:flex;align-items:center;justify-content:space-between}
.glb-viz-title{font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px}
.glb-viz-model{font-size:10.5px;font-weight:500;color:var(--ui-text-quaternary);border:1px solid var(--ui-stroke-secondary);border-radius:999px;padding:2px 8px}
.glb-viz-blob-wrap{display:flex;justify-content:center;padding:10px 0 0}
.glb-viz-blob{width:160px;height:160px;box-shadow:0 0 46px rgba(124,108,255,.45)}
.glb-viz-blob[data-voice=idle]{box-shadow:none}
.glb-viz-wave{display:flex;align-items:center;justify-content:center;gap:3px;height:46px}
.glb-viz-bar{width:4px;height:30px;border-radius:2px;background:linear-gradient(180deg,#8f7bff,#4fd1c5);animation:glb-wave 520ms ease-in-out infinite alternate;transform-origin:center}
.glb-viz-backdrop[data-voice=idle] .glb-viz-bar{animation:none;transform:scaleY(.14);opacity:.5}
.glb-viz-backdrop[data-voice=speaking] .glb-viz-bar{background:linear-gradient(180deg,#7dffb2,#17a163)}
@keyframes glb-wave{from{transform:scaleY(calc(.18 + var(--lv,0)*.12))}to{transform:scaleY(calc(.3 + var(--lv,0)*.55))}}
.glb-viz-status{display:flex;align-items:center;justify-content:center;gap:14px;font-size:12px;color:var(--ui-text-secondary)}
.glb-viz-meta{color:var(--ui-text-quaternary);font-size:11px}
.glb-viz-msg{font-size:12px;color:var(--ui-text-secondary);background:rgba(143,123,255,.08);border:1px solid rgba(143,123,255,.25);border-radius:8px;padding:6px 10px}
.glb-viz-transcript{display:flex;flex-direction:column;gap:6px;max-height:220px;overflow-y:auto}
.glb-viz-line{font-size:12.5px;line-height:1.45;padding:6px 10px;border-radius:10px;width:fit-content;max-width:88%}
.glb-viz-you{align-self:flex-end;background:rgba(143,123,255,.16)}
.glb-viz-gemini{align-self:flex-start;background:rgba(255,255,255,.06)}
.glb-viz-empty{font-size:12px;color:var(--ui-text-quaternary);text-align:center;padding:14px}
.glb-viz-log{font-family:var(--font-mono,monospace);font-size:10px;line-height:14px;color:var(--ui-text-quaternary);border-top:1px solid var(--ui-stroke-secondary);padding-top:6px;white-space:pre-wrap;word-break:break-word}
.glb-viz-actions{display:flex;justify-content:center;margin-top:2px}


.glb-viz-panelhost{display:flex;flex-direction:column;min-height:100%;background:var(--ui-surface,#12121a)}
.glb-viz-card-panel{border:none;border-radius:0;box-shadow:none;background:transparent;width:100%;max-height:none;flex:1}


.glb-viz-body{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;flex:1;min-height:0}
.glb-viz-left{display:flex;flex-direction:column;align-items:center;gap:12px}
.glb-viz-right{width:100%;display:flex;flex-direction:column;gap:8px;min-width:0;justify-content:flex-end}
.glb-viz-transcript{flex:none;max-height:180px;width:min(640px,100%);margin:0 auto;justify-content:flex-end}
.glb-viz-log{max-height:84px;overflow-y:auto;width:min(640px,100%);margin:0 auto;border-top:1px solid var(--ui-stroke-secondary)}
/* smooth blob: level scales a WRAPPER with a transition (absorbs mic jumps);
   the morph itself is a gentler 5-stop curve, no rotation wobble */
.glb-viz-blob-scale{transform:scale(calc(1 + var(--lv,0)*.10));transition:transform .4s cubic-bezier(.22,.61,.36,1);will-change:transform}
@keyframes glb-blob-soft{0%,100%{border-radius:46% 54% 52% 48%/48% 46% 54% 52%}
  25%{border-radius:54% 46% 44% 56%/50% 56% 44% 50%}
  50%{border-radius:48% 52% 58% 42%/44% 48% 52% 56%}
  75%{border-radius:52% 48% 46% 54%/54% 44% 56% 46%}}
.glb-viz-blob{animation:glb-blob-soft 11s ease-in-out infinite;animation-timing-function:cubic-bezier(.45,.05,.55,.95)}
.glb-viz-blob[data-voice=speaking]{animation-duration:3.6s}
.glb-viz-blob[data-voice=thinking]{animation:glb-spin 2.4s linear infinite;border-radius:50%}
/* the wave bars ease too */
.glb-viz-bar{transition:transform .18s ease-out}


/* face-ghost at composer size: the % features scale up so the face reads */
.glb-blob-sb .glb-face-eyes{position:absolute;inset:0;display:block}
.glb-blob-sb .glb-viz-eye{width:13%;height:17%;top:32%;background:rgba(12,12,22,.9)}
.glb-blob-sb .glb-viz-eye-l{left:29%}
.glb-blob-sb .glb-viz-eye-r{right:29%}
.glb-blob-sb .glb-face-mouth{position:absolute;left:50%;top:58%;translate:-50% 0;width:30%;height:11%;border-radius:1.5px;background:rgba(12,12,22,.85);transform-origin:center;transition:opacity .25s ease, transform .09s ease}
.glb-blob-sb.glb-blink .glb-viz-eye{height:3%;top:38%}
.glb-blob-sb[data-voice=idle] .glb-face-mouth{opacity:0 !important}

/* ---- liquid blob logo (statusbar icon + panel hero) ---- */
@keyframes glb-blob{0%,100%{border-radius:42% 58% 55% 45%/45% 42% 58% 55%;transform:rotate(0deg) scale(calc(1 + var(--lv,0)*0.24))}
  33%{border-radius:58% 42% 40% 60%/52% 58% 42% 48%;transform:rotate(10deg) scale(calc(1 + var(--lv,0)*0.24))}
  66%{border-radius:50% 50% 62% 38%/40% 50% 50% 60%;transform:rotate(-8deg) scale(calc(1 + var(--lv,0)*0.24))}}
@keyframes glb-spin{to{transform:rotate(360deg)}}
.glb-blob-sb{position:relative;width:22px;height:22px;border-radius:44% 56% 52% 48%/48% 44% 56% 52%;
  background:linear-gradient(135deg,#8f7bff,#4fd1c5);animation:glb-blob 4s ease-in-out infinite;
  display:inline-block}
.glb-blob-sb[data-voice=idle]{background:rgba(127,127,127,.45);animation:none}
.glb-blob-sb[data-voice=listening]{background:linear-gradient(135deg,#8f7bff,#4fd1c5)}
.glb-blob-sb[data-voice=speaking]{background:linear-gradient(135deg,#7dffb2,#17a163);animation-duration:1.4s}
.glb-blob-sb[data-voice=thinking]{background:conic-gradient(from 0deg,#8f7bff,#4fd1c5,#8f7bff);animation:glb-spin 1.4s linear infinite;border-radius:50%}
.glb-blob-wrap{display:flex;justify-content:center;padding:8px 0 2px}
.glb-blob{position:relative;width:54px;height:54px;background:linear-gradient(135deg,#8f7bff,#4fd1c5);
  border-radius:42% 58% 55% 45%/45% 42% 58% 55%;animation:glb-blob 5s ease-in-out infinite;
  box-shadow:0 0 18px rgba(124,108,255,.35);transition:background .35s, box-shadow .35s}
.glb-blob[data-voice=idle]{background:rgba(127,127,127,.3);box-shadow:none;animation-duration:9s}
.glb-blob[data-voice=listening]{background:linear-gradient(135deg,#8f7bff,#4fd1c5)}
.glb-blob[data-voice=speaking]{background:linear-gradient(135deg,#7dffb2,#17a163);box-shadow:0 0 20px rgba(63,185,111,.45);animation-duration:1.8s}
.glb-blob[data-voice=thinking]{background:linear-gradient(135deg,#8f7bff,#4fd1c5);animation-duration:1.6s;box-shadow:0 0 22px rgba(124,108,255,.5)}

/* speaking: deeper, faster morph + echo ring ping */
.glb-blob[data-voice=speaking]{animation-duration:1.1s}
.glb-blob--talking::after{content:'';position:absolute;inset:-6px;border-radius:inherit;
  border:2px solid rgba(63,185,111,.55);animation:glb-ping 1s ease-out infinite}
@keyframes glb-ping{0%{transform:scale(.92);opacity:.9}100%{transform:scale(1.28);opacity:0}}
.glb-blob{position:relative}
/* the inline scale transform would fight the morph keyframes — put the wobble
   on the blob itself via a CSS var the JSX sets each render */

.glb-blob-btn{background:none;border:none;padding:0;cursor:pointer;display:flex;transition:transform .15s ease}
.glb-blob-btn:hover{transform:scale(1.07)}
.glb-blob-btn:active{transform:scale(.94)}

/* composer action-row fit: the blob chip matches the row's height */
.gemini-live-bar{height:auto}
.gemini-live-panel{width:320px}

/* ---- soft chat-bubble transcript (minibar popover) ----
   Smoothness contract: bubbles and word spans carry STABLE keys, so React
   keeps their DOM nodes across polls — entrance/word animations run once on
   mount and are never restarted by streaming updates. All easing is the soft
   cubic-bezier(.22,.61,.36,1); no hard blinks, no snaps. */
.glb-tx{display:flex;flex-direction:column;gap:10px;max-height:150px;overflow-y:auto;padding:6px 2px 2px;scroll-behavior:smooth;scrollbar-width:none}
.glb-tx::-webkit-scrollbar{display:none}
.glb-tx-col{display:flex;flex-direction:column;min-width:0}
.glb-tx-col.r{align-items:flex-end}
.glb-tx-who{font-size:9px;letter-spacing:.13em;text-transform:uppercase;color:var(--ui-text-quaternary,#5c5c72);margin-bottom:4px;padding:0 4px;display:flex;align-items:center}
.glb-tx-row{display:flex;gap:7px;align-items:flex-end}
.glb-tx-mini{flex:none;width:20px;height:20px;background:linear-gradient(135deg,#a5b4fc,#8f7bff);border-radius:46% 54% 52% 48%/48% 46% 54% 52%;position:relative;opacity:.9;animation:glb-tx-morph 11s ease-in-out infinite}
.glb-tx-mini::before,.glb-tx-mini::after{content:"";position:absolute;top:8px;width:2.5px;height:3.5px;border-radius:50%;background:#14141c}
.glb-tx-mini::before{left:5.5px}
.glb-tx-mini::after{right:5.5px}
@keyframes glb-tx-morph{0%,100%{border-radius:46% 54% 52% 48%/48% 46% 54% 52%}50%{border-radius:52% 48% 46% 54%/54% 44% 56% 46%}}
.glb-tx-bub{max-width:85%;padding:9px 13px;border-radius:16px;line-height:1.55;font-size:12.5px;width:fit-content;word-break:break-word;overflow-wrap:anywhere}
.glb-tx-you{background:rgba(143,123,255,.11);color:#e9e5fc;border-bottom-right-radius:7px}
.glb-tx-gem{background:rgba(255,255,255,.045);color:var(--ui-text-primary,#e8e8f0);border-bottom-left-radius:7px;transition:box-shadow 1.2s cubic-bezier(.22,.61,.36,1),background 1.2s cubic-bezier(.22,.61,.36,1)}
.glb-tx-gem.live{background:rgba(255,255,255,.055);animation:glb-tx-breathe 3.6s ease-in-out infinite}
@keyframes glb-tx-breathe{0%,100%{box-shadow:0 0 16px rgba(143,123,255,.05)}50%{box-shadow:0 0 30px rgba(143,123,255,.12)}}
.glb-tx-enter{animation:glb-tx-enter .7s cubic-bezier(.22,.61,.36,1) both}
@keyframes glb-tx-enter{from{opacity:0;transform:translateY(9px) scale(.98)}to{opacity:1;transform:none}}
.glb-tx-w{animation:glb-tx-wfade .5s cubic-bezier(.22,.61,.36,1) both}
@keyframes glb-tx-wfade{from{opacity:0}to{opacity:1}}
.glb-tx-caret{display:inline-block;width:2px;height:1em;background:#4fd1c5;vertical-align:-2px;margin-left:3px;border-radius:2px;animation:glb-tx-pulse 1.5s ease-in-out infinite}
@keyframes glb-tx-pulse{0%,100%{opacity:.85}50%{opacity:.12}}
.glb-tx-eq{display:inline-flex;gap:2px;align-items:flex-end;height:8px;margin-left:6px}
.glb-tx-eq i{width:2px;background:#7dffb2;border-radius:2px;animation:glb-tx-eq 1s ease-in-out infinite alternate;opacity:.85}
.glb-tx-eq i:nth-child(1){height:3px}
.glb-tx-eq i:nth-child(2){height:7px;animation-delay:.25s}
.glb-tx-eq i:nth-child(3){height:5px;animation-delay:.5s}
@keyframes glb-tx-eq{from{transform:scaleY(.45)}to{transform:scaleY(1)}}
`

function b64FromInt16(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength)
  let bin = ''
  const CHUNK = 0x8000
  for (let i = 0; i < bytes.length; i += CHUNK) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK))
  return btoa(bin)
}

function createLive(ctx) {
  const status = atom('idle') // idle | connecting | live | error
  const message = atom('')
  const transcript = atom([])
  const level = atom(0)
  const speaking = atom(false)   // Gemini's audio is playing right now
  const showTranscript = atom(true)  // panel prefs (Settings → Plugins writes them)
  const showLog = atom(true)
  const outLevel = atom(0)       // live playback level 0..1 (what Gemini speaks)
  const models = atom([])        // candidates from /config (short names)
  const chosen = atom('')        // model display; mirrored from the settings store
  const usage = atom('')         // session token meter, e.g. '23.4k'
  const log = atom([])
  let stream = null
  let processor = null
  let srcCtx = null
  let playCtx = null
  let stopped = true
  let pollTimer = null
  let playHead = 0 // sequential audio queue clock (AudioContext time)
  let activeSrcs = new Set() // scheduled-but-maybe-playing sources (flushable)
  let micQueue = Promise.resolve() // keeps chunk order
  let speakMode = 'full'           // spoken-answer detail, from /start (settings)
  let off = { i: 0, o: 0, a: 0, d: 0 }   // poll offsets (d = delegations)
  let lastInterrupted = 0                // last seen server barge-in count
  let bargeHits = 0                      // consecutive loud mic blocks while playing
  let outAnalyser = null                 // playback-side level (what Gemini speaks)
  let outData = null
  let outTimer = null

  // ---- Hermes delegation (the GPT-Live pattern) ---------------------------
  // The model decides to hand work over (talk toolCall). This side
  // submits the request to the dedicated 'Livetalk' session via
  // prompt.submit (surface 'voice-live'), polls the submission receipt until
  // it settles, and feeds the final answer back upstream as a toolResponse
  // so the voice speaks it.
  const delegating = atom(false)   // an talk is being answered right now
  let inFlight = null              // { id, request } being answered by the agent

  function recentContext() {
    return transcript.get().filter(r => r.role !== 'sep').slice(-6)
      .map(r => `${r.role === 'you' ? 'User' : 'Assistant'}: ${r.text}`).join('\n')
  }

  // Delegations go to a DEDICATED "Livetalk" session: it is auto-created on
  // first use, then reused, so the agent keeps one memory home for voice work
  // and delegation never depends on which chat the user has focused.
  const LIVE_TITLE = 'Livetalk'
  let voiceSessionId = null

  async function targetSession() {
    if (voiceSessionId) return voiceSessionId
    try {
      const found = await host.request('session.list', { title: LIVE_TITLE, scope: 'stored' })
      const hit = found?.sessions?.[0]?.session_id || found?.sessions?.[0]?.id
      if (hit) { voiceSessionId = hit; addLog(`reusing session "${LIVE_TITLE}" (${hit})`); return hit }
    } catch { /* fall through to create */ }
    try {
      const created = await host.request('session.create', { title: LIVE_TITLE })
      voiceSessionId = created?.session_id || created?.id || null
      if (voiceSessionId) addLog(`created session "${LIVE_TITLE}" (${voiceSessionId})`)
      return voiceSessionId
    } catch (e) {
      addLog(`session.create failed: ${e?.message || e}`)
      return null
    }
  }

  async function sendToolResponse(id, answer) {
    try { await ctx.rest('/tool_response', { method: 'POST', body: { id, answer } }) }
    catch { /* session died — the poll loop surfaces it */ }
  }

  // The prompt.submit receipt carries an admission_id; poll prompt.receipt
  // (include_result) until the turn settles — terminal means the structured
  // result with the agent's final_response is readable. No event bus needed:
  // 'assistant.completed' is an API-stream event the plugin host never emits.
  async function waitAnswer(admissionId, sessionId, timeoutMs = 300000) {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 2000))
      if (!inFlight) return null // superseded or disposed
      try {
        const r = await host.request('prompt.receipt', { session_id: sessionId, admission_id: admissionId, include_result: true })
        if (r?.status === 'terminal') {
          const reply = String(r?.result?.final_response ?? '').trim()
          addLog(`receipt terminal — ${reply.length} chars`)
          return reply
        }
        if (r?.status === 'unknown') { addLog('admission unknown — giving up'); return '' }
      } catch (e) { addLog(`receipt poll failed: ${e?.message || e}`) }
    }
    addLog('receipt poll timed out (5 min)')
    return null
  }

  async function handleDelegation(d) {
    inFlight = d
    delegating.set(true)
    transcript.set([...transcript.get().slice(-80), { role: 'you', text: `🎙️ ${d.request}` }])
    addLog(`delegating to Hermes: ${d.request.slice(0, 60)}`)
    const sessionId = await targetSession()
    if (!sessionId) {
      addLog('no Hermes session available — answering with guidance')
      await sendToolResponse(d.id, 'I could not open a Hermes chat session. Please try again in a moment.')
      inFlight = null
      delegating.set(false)
      return
    }
    try {
      const receipt = await host.request('prompt.submit', {
        session_id: sessionId,
        text: `${voiceNote(speakMode)}\n\n${d.request}`,
        submission_id: `gemini-live-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        surface: 'voice-live',
        voice_context: recentContext(),
      })
      const admissionId = receipt?.admission_id
      if (!admissionId) throw new Error(`no admission id: ${JSON.stringify(receipt ?? {}).slice(0, 80)}`)
      addLog(`submitted — receipt ${String(admissionId).slice(0, 24)}…`)
      const answer = await waitAnswer(admissionId, sessionId)
      if (!inFlight) return
      inFlight = null
      delegating.set(false)
      if (answer) {
        transcript.set([...transcript.get().slice(-80), { role: 'gemini', text: answer }])
        addLog(`hermes answered (${answer.length} chars) — speaking`)
        void sendToolResponse(d.id, answer)
      } else {
        addLog('agent returned no answer — telling the voice side')
        await sendToolResponse(d.id, 'The agent finished but returned no answer. Please ask again.')
      }
    } catch (e) {
      addLog(`prompt.submit failed: ${e?.message || e}`)
      await sendToolResponse(d.id, `Hermes could not take the request: ${e?.message || e}`)
      inFlight = null
      delegating.set(false)
    }
  }


  function dispatchDelegations(list) {
    for (const d of list || []) {
      if (inFlight || !d?.id) continue
      void handleDelegation(d)
    }
  }


  const addLog = msg => log.set([...log.get().slice(-40), { t: new Date().toLocaleTimeString(), msg }])

  function playPcm24k(b64) {
    if (!playCtx) return
    const bin = atob(b64)
    const pcm = new Int16Array(bin.length / 2)
    for (let i = 0; i < pcm.length; i++) pcm[i] = (bin.charCodeAt(2 * i) & 0xff) | ((bin.charCodeAt(2 * i + 1) & 0xff) << 8)
    const buf = playCtx.createBuffer(1, pcm.length, 24000)
    buf.getChannelData(0).set(new Float32Array(pcm.length).map((_, i) => pcm[i] / 32768))
    const src = playCtx.createBufferSource()
    src.buffer = buf
    if (outAnalyser) src.connect(outAnalyser)
    src.connect(playCtx.destination)
    // Schedule SEQUENTIALLY — chunks from /poll arrive in bursts; starting
    // them all 'now' overlaps and chops. Queue after the previous one instead.
    const now = playCtx.currentTime
    if (playHead < now + 0.02) playHead = now + 0.02 // resync after silence
    activeSrcs.add(src)
    src.onended = () => activeSrcs.delete(src)
    src.start(playHead)
    playHead += buf.duration
    speaking.set(true)
    setTimeout(() => { if (playCtx.currentTime >= playHead - 0.05) speaking.set(false) },
      Math.max(150, (playHead - playCtx.currentTime) * 1000 + 120))
  }

  // Barge-in: the server raised `interrupted` (the user talked over the
  // model). Stop every scheduled source NOW and drop the queued clock —
  // otherwise the stale answer keeps playing to its end while the model
  // already moved on.
  function flushPlayback() {
    for (const src of activeSrcs) { try { src.onended = null; src.stop() } catch {} }
    activeSrcs.clear()
    playHead = 0
    speaking.set(false)
    addLog('interrupted — playback flushed')
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  }

  function startPolling() {
    stopPolling()
    pollTimer = setInterval(async () => {
      if (stopped) return
      try {
        const p = await ctx.rest(`/poll?in_offset=${off.i}&out_offset=${off.o}&a_offset=${off.a}&d_offset=${off.d}`)
        if (p.usage && p.usage.totalTokenCount != null) {
          const t = p.usage.totalTokenCount
          usage.set(t >= 1000 ? `${(t / 1000).toFixed(1)}k` : String(t))
        }
        off.i = p.in_total ?? off.i
        off.o = p.out_total ?? off.o
        off.a = p.a_total ?? off.a
        off.d = p.d_total ?? off.d
        if (p.error) { message.set(p.error); addLog(`error: ${p.error}`) }
        // Display sync: the official settings store is the single source of
        // truth (Settings → Plugins form OR this picker can change it).
        if (p.model_setting) {
          const cur = chosen.get().replace(/^models\//, '')
          if (cur !== p.model_setting) chosen.set(p.model_setting)
        }
        if (p.show_transcript != null) showTranscript.set(!!p.show_transcript)
        if (p.show_log != null) showLog.set(!!p.show_log)
        // Barge-in check FIRST: when the server flags an interruption, every
        // audio chunk in this (and any pending) batch is dead air — flush the
        // playback queue and skip this batch's audio.
        const barged = (p.interrupted ?? lastInterrupted) > lastInterrupted
        if (barged) { lastInterrupted = p.interrupted; flushPlayback() }
        for (const t of p.input) transcript.set([...transcript.get().slice(-80), t === '—' ? { role: 'sep', text: '' } : { role: 'you', text: t }])
        for (const t of p.output) transcript.set([...transcript.get().slice(-80), { role: 'gemini', text: t }])
        if (!barged) for (const a of p.audio) playPcm24k(a)
        if (p.delegations?.length && !inFlight) dispatchDelegations(p.delegations)
        if (p.session !== 'live' && !stopped) {
          // upstream dropped — surface honestly, keep UI state
          addLog('upstream session closed')
          status.set('error')
          message.set(p.error || 'Session dropped — press start again')
          stopMic()
          stopPolling()
        }
      } catch { /* transient — next tick retries */ }
    }, 300)
  }

  function stopMic() {
    try { processor && (processor.onaudioprocess = null, processor.disconnect()) } catch {}
    try { stream && stream.getTracks().forEach(t => t.stop()) } catch {}
    try { srcCtx && srcCtx.close() } catch {}
    processor = null; stream = null; srcCtx = null
    level.set(0)
  }

  function stop() {
    stopped = true
    stopMic()
    stopPolling()
    flushPlayback()
    speaking.set(false)
    if (outTimer) { clearInterval(outTimer); outTimer = null }
    outLevel.set(0)
    void ctx.rest('/close', { method: 'POST' }).catch(() => {})
    status.set('idle')
  }

  async function start() {
    if (status.get() === 'live' || status.get() === 'connecting') { stop(); return }
    status.set('connecting')
    message.set('')
    stopped = false
    playHead = 0
    usage.set('')
    transcript.set([])
    off = { i: 0, o: 0, a: 0, d: 0 }
    try {
      playCtx = new AudioContext({ sampleRate: 24000 })
      outAnalyser = playCtx.createAnalyser()
      outAnalyser.fftSize = 256
      outData = new Uint8Array(outAnalyser.fftSize)
      outTimer = setInterval(() => {   // RMS of what Gemini is playing
        if (!outAnalyser) return
        outAnalyser.getByteTimeDomainData(outData)
        let sum = 0
        for (let i = 0; i < outData.length; i++) { const v = (outData[i] - 128) / 128; sum += v * v }
        outLevel.set(Math.min(1, Math.sqrt(sum / outData.length) * 3))
      }, 100)
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      srcCtx = new AudioContext({ sampleRate: 16000 })
      const src = srcCtx.createMediaStreamSource(stream)
      processor = srcCtx.createScriptProcessor(4096, 1, 1)
      processor.onaudioprocess = event => {
        if (stopped) return
        const pcm = new Int16Array(event.inputBuffer.length)
        const input = event.inputBuffer.getChannelData(0)
        let peak = 0
        for (let i = 0; i < input.length; i++) {
          const s = Math.max(-1, Math.min(1, input[i]))
          pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff
          peak = Math.max(peak, Math.abs(s))
        }
        level.set(Math.round(peak * 100))
        // Client-side barge-in: the server only raises `interrupted` while it
        // is still GENERATING, but generation outruns playback — long answers
        // are fully queued locally seconds in, so the frame never comes. While
        // audio is playing, a sustained real-voice peak (echo cancellation
        // removes the model's own speaker bleed) cuts the queue immediately.
        if (playCtx && playHead > playCtx.currentTime + 0.05 && peak > 0.22) {
          if (++bargeHits >= 3) { // 3 consecutive blocks ≈ 770 ms of speech
            bargeHits = 0
            flushPlayback()
            addLog('barge-in: voice over playback — queue cut')
          }
        } else bargeHits = 0
        const b64 = b64FromInt16(pcm)
        // Serialize uploads to preserve order; drop backlog if we fall behind.
        micQueue = micQueue.then(() => stopped
          ? null
          : ctx.rest('/audio', { method: 'POST', body: { data: b64 }, timeoutMs: 4000 }).catch(() => {})
        )
      }
      src.connect(processor)
      processor.connect(srcCtx.destination) // keeps the node pumping

      addLog('POST /start — dialing Gemini…')
      const body = { resume: true }   // the model comes from the plugin settings
      const r = await ctx.rest('/start', { method: 'POST', timeoutMs: 30000, body })
      if (r?.ok) speakMode = r.speak === 'brief' ? 'brief' : 'full'
      if (!r.ok) {
        stopMic()
        status.set('error')
        message.set(r.error || 'Start failed')
        addLog(`start failed: ${r.error}`)
        return
      }
      startPolling()
      addLog(`LIVE — ${r.model} (mic streaming)`)
      status.set('live')
      message.set('')
    } catch (e) {
      stopMic()
      stopPolling()
      status.set('error')
      message.set(String(e?.message || e))
      addLog(`error: ${e?.message || e}`)
    }
  }

  function setChosen(v) {
    const bare = v.replace(/^models\//, '')
    chosen.set(bare)
    // Single source of truth: write through the OFFICIAL plugin-settings writer
    // (the same store the Settings → Plugins form saves) — picker and form can
    // never disagree. The poll loop echoes the saved value back for display.
    try {
      void host.request('plugins.manage', { action: 'settings', key: 'livetalk',
        values: { live_model: bare } }).catch(() => {})
    } catch {}
  }

  async function fetchModels() {
    try {
      const cfg = await ctx.rest('/config')
      const list = (cfg.candidates || []).map(m => String(m).replace('models/', '')).filter(m => !m.includes('translate'))
      models.set(list.length ? list : [String(cfg.model || '').replace('models/', '')])
      // The settings store is the source of truth — reflect it in the picker
      // (covers form changes made while the session is idle).
      const want = String(cfg.model || '').replace('models/', '')
      if (want && chosen.get() !== want) chosen.set(want)
      else if (!chosen.get() && models.get()[0]) chosen.set(models.get()[0])
      if (cfg.show_transcript != null) showTranscript.set(!!cfg.show_transcript)
      if (cfg.show_log != null) showLog.set(!!cfg.show_log)
    } catch { /* keep previous list */ }
  }

  ctx.onDispose(() => { stop(); inFlight = null; if (outTimer) { clearInterval(outTimer); outTimer = null } })
  return { outLevel, showTranscript, showLog, status, message, transcript, level, models, chosen, usage, log, delegating, speaking, start, stop, toggle: start, fetchModels, setChosen }
}



// Soft chat-bubble transcript. Stable keys (turn index in the merged list,
// word index inside a turn) let React reuse DOM nodes across polls, so the
// entrance + word-fade animations run exactly once per mount — streaming
// updates only mutate text, never restart animations.
// Scroll is STICKY-BOTTOM: new text auto-follows only while the user is at
// the bottom; scrolling up to read pins the position — polls never yank it
// back down. Scrolling to the bottom re-arms the follow.
function TxBubbles({ tail, speaking }) {
  const ref = useRef(null)
  const pinned = useRef(true)
  const lastLen = useRef(-1)
  const len = tail.reduce((a, r) => a + r.text.length, 0)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (len < lastLen.current) pinned.current = true // transcript reset (new session)
    if (len !== lastLen.current) {
      lastLen.current = len
      if (pinned.current) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    }
  })
  const onScroll = () => {
    const el = ref.current
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 28
  }
  return jsx('div', { className: 'glb-tx', ref, onScroll, role: 'status', 'aria-label': 'transcript', children:
    tail.map((row, i) => {
      const isLast = i === tail.length - 1
      const liveGem = row.role === 'gemini' && isLast && speaking
      // split AFTER each space so every chunk keeps its trailing space; the
      // in-flight last chunk updates in place (same key → no remount/flicker)
      const words = row.text.split(/(?<= )/).map((c, wi) => jsx('span', { className: 'glb-tx-w', children: c }, wi))
      if (liveGem) words.push(jsx('span', { className: 'glb-tx-caret', 'aria-hidden': true }, 'caret'))
      const bub = jsx('div', { className: 'glb-tx-bub ' + (row.role === 'you' ? 'glb-tx-you' : 'glb-tx-gem' + (liveGem ? ' live' : '')), children: words })
      if (row.role === 'you') {
        return jsx('div', { className: 'glb-tx-col r glb-tx-enter', children: bub }, row.k)
      }
      // no speaker text for Gemini — just the avatar; the equalizer row keeps
      // a constant height (opacity fade, no layout shift) as the speaking cue
      return jsxs('div', { className: 'glb-tx-row glb-tx-enter', children: [
        jsx('span', { className: 'glb-tx-mini', 'aria-hidden': true }),
        jsxs('div', { className: 'glb-tx-col', children: [
          jsx('div', { className: 'glb-tx-who', children:
            jsxs('span', { className: 'glb-tx-eq', 'aria-hidden': true, style: { opacity: liveGem ? 1 : 0, transition: 'opacity .8s cubic-bezier(.22,.61,.36,1)' }, children: [jsx('i', {}, 0), jsx('i', {}, 1), jsx('i', {}, 2)] })
          }),
          bub,
        ] }),
      ] }, row.k)
    })
  })
}


function Minibar({ live }) {
  const outLv = useValue(live.outLevel)
  const [blink, setBlink] = useState(false)
  useEffect(() => {
    let alive = true, tid
    function schedule() {
      tid = setTimeout(() => {
        if (!alive) return
        setBlink(true)
        setTimeout(() => { if (!alive) return; setBlink(false); schedule() }, 140)
      }, 1400 + Math.random() * 3000)
    }
    schedule()
    return () => { alive = false; clearTimeout(tid) }
  }, [])
  const status = useValue(live.status)
  const message = useValue(live.message)
  const transcript = useValue(live.transcript)
  const level = useValue(live.level)
  const logs = useValue(live.log)
  const models = useValue(live.models)
  const chosen = useValue(live.chosen)
  const usage = useValue(live.usage)
  const delegating = useValue(live.delegating)
  const speaking = useValue(live.speaking)
  const showTranscript = useValue(live.showTranscript)
  const showLog = useValue(live.showLog)
  const active = status === 'live' || status === 'connecting'
  // The blob's mood: purple morph = listening, green = speaking, spin = thinking
  const voiceState = delegating ? 'thinking' : speaking ? 'speaking' : active ? 'listening' : 'idle'
  // Merge streaming fragments into flowing sentences per speaker: consecutive
  // same-role fragments concatenate; '—' (turnComplete) starts a new line.
  const merged = []
  for (const row of transcript) {
    if (row.role === 'sep') { merged.push({ role: 'sep', text: '' }); continue }
    const last = merged[merged.length - 1]
    if (last && last.role === row.role) last.text += row.text
    else merged.push({ role: row.role, text: row.text })
  }
  // keep the absolute merged index as a stable React key — tail position
  // shifts as turns arrive, but the key must not (or bubbles would remount)
  const tail = merged.map((r, k) => ({ ...r, k })).filter(r => r.role !== 'sep').slice(-2)
  return jsxs('div', { className: 'gemini-live-bar', 'data-gemini-live-status': status, children: [
    jsx(Popover, {
      onOpenChange: open => { if (open) void live.fetchModels(); if (!open && status === 'idle') live.stop() },
      children: [
        jsx(PopoverTrigger, { asChild: true, children:
          jsx(Button, { variant: 'ghost', size: 'micro', 'aria-label': 'Livetalk panel', children:
            jsxs('span', { className: 'glb-blob-sb' + (blink ? ' glb-blink' : ''), 'data-voice': voiceState, 'aria-hidden': true, children: [
              jsxs('span', { className: 'glb-viz-eyes glb-face-eyes', children: [
                jsx('span', { className: 'glb-viz-eye glb-viz-eye-l' }),
                jsx('span', { className: 'glb-viz-eye glb-viz-eye-r' }),
              ] }),
              jsx('span', { className: 'glb-viz-mouth glb-face-mouth', style: {
                transform: `scaleY(${0.3 + Math.min(1, level / 100) * 1.4})`,
                opacity: speaking ? 0.9 : 0,
              } }),
            ] })
          })
        }),
        jsx(PopoverContent, { side: 'top', align: 'end', className: 'gemini-live-panel', 'aria-label': 'Livetalk', children:
          jsxs('div', { children: [
            jsx('div', { className: 'glb-blob-wrap', children:
              jsx(Tip, { label: active ? 'Stop session' : 'Start session', children:
                jsx('button', { type: 'button', className: 'glb-blob-btn', onClick: live.toggle,
                  'aria-label': active ? 'Stop Livetalk session' : 'Start Livetalk session', children:
                  jsxs('div', { className: 'glb-blob' + (speaking ? ' glb-blob--talking' : '') + (blink ? ' glb-blink' : ''), 'data-voice': voiceState,
                    style: { '--lv': (Math.max(level, outLv * 100) / 100).toFixed(3) },
                    children: [
                      jsxs('span', { className: 'glb-viz-eyes glb-face-eyes', children: [
                        jsx('span', { className: 'glb-viz-eye glb-viz-eye-l' }),
                        jsx('span', { className: 'glb-viz-eye glb-viz-eye-r' }),
                      ] }),
                      jsx('span', { className: 'glb-viz-mouth glb-face-mouth', style: {
                        transform: `scaleY(${0.25 + Math.min(1, Math.max(level, outLv * 100) / 100) * 1.5})`,
                        opacity: speaking ? 0.9 : 0,
                      } }),
                    ],
                    'aria-hidden': true })
                })
              })
            }),
            jsxs('div', { className: 'gemini-live-transport', children: [
              jsx('span', { className: 'gemini-live-usage', title: 'Session tokens (free-tier meter)', children: usage ? `${usage} tok` : '' }),
              jsx('span', { className: 'gemini-live-transport-space' })
            ] }),
            (message || (showTranscript && tail.length > 0)) && (message
              ? jsx('div', { className: 'gemini-live-error', role: 'status', children: message })
              : jsx(TxBubbles, { tail, speaking })),
            showLog && jsx('div', { className: 'gemini-live-log', 'aria-label': 'diagnostics', children:
              logs.map((row, i) => jsxs('div', { children: [row.t, '  ', row.msg] }, i))
            })
          ] })
        })
      ]
    })
  ] })
}


export default {
  id: ID,
  name: 'Livetalk',
  description: 'Realtime voice chat with your Hermes agent — Gemini Live today, more providers tomorrow; full proxy in the local backend.',
  register(ctx) {
    // idempotent injection: a failed load leaks the previous <style> (its
    // onDispose never runs), so the old full-bleed rules would outlive us —
    // always kill the stale one first.
    document.getElementById('livetalk-style')?.remove()
    const style = document.createElement('style')
    style.id = 'livetalk-style'
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())
    const live = createLive(ctx)
    // Lives in the composer's action row (beside the mic) — the voice control
    // belongs with the voice input; the popover opens above the composer.
    ctx.register({ id: 'minibar', area: COMPOSER_AREAS.actions, order: 10, render: () => jsx(Minibar, { live }) })


  }
}
