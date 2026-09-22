/**
 * Gemini Live Bridge — status-bar minibar + backend-proxy voice session
 * Save at: ~/.hermes/desktop-plugins/gemini-live-bridge/plugin.js
 *
 * The renderer NEVER talks to Google. Mic is captured here, PCM chunks are
 * POSTed to the local backend, which owns the upstream Gemini Live WebSocket
 * (ephemeral token, server-side key). Playback: poll the backend for new
 * model-audio chunks and play them via WebAudio.
 */

import { atom, Button, icons, Popover, PopoverContent, PopoverTrigger, STATUSBAR_AREAS, Tip, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'gemini-live-bridge'

const CSS = `
.gemini-live-bar{display:flex;align-items:center;gap:2px;height:100%;color:var(--ui-text-tertiary)}
.gemini-live-bar .gemini-live-name{max-width:96px;overflow:hidden;text-overflow:ellipsis;text-align:left}
.gemini-live-dot{display:inline-block;width:7px;height:7px;border-radius:9999px;background:var(--ui-text-quaternary);flex-shrink:0}
.gemini-live-dot[data-active=true]{background:var(--ui-accent)}
.gemini-live-panel{width:320px;max-width:calc(100vw - 24px);height:auto;overflow:visible}
.gemini-live-status{display:flex;align-items:center;gap:6px;padding:6px 4px;font-size:11px;color:var(--ui-text-secondary)}
.gemini-live-error{font-size:11px;line-height:15px;color:var(--ui-text-secondary);padding:6px 4px;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;max-height:76px;overflow-y:auto}
.gemini-live-transport{display:flex;align-items:center;gap:4px;padding:2px 2px 0}
.gemini-live-transport-space{flex:1}
.gemini-live-flag{font-size:10px;line-height:18px;padding:0 7px;border:1px solid var(--ui-stroke-secondary,var(--border,#333));border-radius:9999px;background:transparent;color:var(--ui-text-secondary,var(--foreground,#ddd));cursor:pointer;white-space:nowrap}
.gemini-live-flag:disabled{opacity:.5;cursor:default}
.gemini-live-usage{font-size:10px;color:var(--ui-text-quaternary,var(--muted-foreground,#999));font-variant-numeric:tabular-nums}
.gemini-live-thinkrow{display:flex;flex-direction:column;align-items:flex-start;gap:3px;padding:2px 2px;margin-top:2px}
.gemini-live-thinkhead{display:flex;align-items:center;gap:6px}
.gemini-live-thinklabel{font-size:10px;color:var(--ui-text-quaternary,var(--muted-foreground,#999))}
.gemini-live-thinkbtn{width:auto;min-width:0;max-width:none;padding:1px 9px;text-align:center;flex:0 0 auto}
.gemini-live-thinklist{position:static;display:flex;flex-direction:column;gap:2px;min-width:118px;background:var(--ui-background,var(--card,#1a1a1a));border:1px solid var(--ui-stroke-secondary,var(--border,#333));border-radius:6px;padding:3px}
.gemini-live-picker{position:relative;max-width:200px}
.gemini-live-model{display:block;width:100%;text-align:right;font-size:10px;color:var(--ui-text-secondary);background:transparent;border:1px solid var(--ui-stroke-secondary);border-radius:4px;padding:1px 5px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:inherit}
.gemini-live-picker-list{position:absolute;right:0;bottom:calc(100% + 4px);z-index:50;min-width:230px;max-height:132px;overflow-y:auto;background:var(--ui-background,var(--card,#1a1a1a));border:1px solid var(--ui-stroke-secondary,var(--border,#333));border-radius:6px;box-shadow:0 8px 24px rgba(0,0,0,.35);padding:3px}
.gemini-live-picker-item{font-size:10px;line-height:22px;padding:0 8px;color:var(--ui-text-secondary,var(--foreground,#ddd));white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;border-radius:4px}
.gemini-live-picker-item:hover{background:var(--ui-hover,rgba(128,128,128,.15))}
.gemini-live-picker-item--sel{color:var(--ui-accent,var(--accent,#8ab4f8))}
.gemini-live-meter{display:flex;align-items:flex-end;gap:2px;height:16px;padding:0 4px;margin:5px 0 3px}
.gemini-live-meter>span{width:4px;background:var(--ui-accent);border-radius:1px;opacity:.8;min-height:2px}
.gemini-live-log{max-height:102px;overflow-y:auto;font-family:var(--font-mono,monospace);font-size:10px;line-height:14px;color:var(--ui-text-quaternary);padding:4px;margin-top:6px;border-top:1px solid var(--ui-stroke-secondary);white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
.gemini-live-log>div:hover{color:var(--ui-text-secondary)}
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
  const modelLabel = atom('…')
  const models = atom([])        // candidates from /config (short names)
  const chosen = atom('')        // selected model; '' migrates to first candidate
  const pickerOpen = atom(false)
  const thinkLevel = atom('low') // only relevant for extended-thinking
  const thinkOpen = atom(false)
  const usage = atom('') // session token meter, e.g. '23.4k'
  const log = atom([])
  let stream = null
  let processor = null
  let srcCtx = null
  let playCtx = null
  let stopped = true
  let pollTimer = null
  let playHead = 0 // sequential audio queue clock (AudioContext time)
  let micQueue = Promise.resolve() // keeps chunk order
  let off = { i: 0, o: 0, a: 0 }   // poll offsets

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
    src.connect(playCtx.destination)
    // Schedule SEQUENTIALLY — chunks from /poll arrive in bursts; starting
    // them all 'now' overlaps and chops. Queue after the previous one instead.
    const now = playCtx.currentTime
    if (playHead < now + 0.02) playHead = now + 0.02 // resync after silence
    src.start(playHead)
    playHead += buf.duration
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  }

  function startPolling() {
    stopPolling()
    pollTimer = setInterval(async () => {
      if (stopped) return
      try {
        const p = await ctx.rest(`/poll?in_offset=${off.i}&out_offset=${off.o}&a_offset=${off.a}`)
        if (p.usage && p.usage.totalTokenCount != null) {
          const t = p.usage.totalTokenCount
          usage.set(t >= 1000 ? `${(t / 1000).toFixed(1)}k` : String(t))
        }
        off.i = p.in_total ?? off.i
        off.o = p.out_total ?? off.o
        off.a = p.a_total ?? off.a
        if (p.error) { message.set(p.error); addLog(`error: ${p.error}`) }
        for (const t of p.input) transcript.set([...transcript.get().slice(-80), t === '—' ? { role: 'sep', text: '' } : { role: 'you', text: t }])
        for (const t of p.output) transcript.set([...transcript.get().slice(-80), { role: 'gemini', text: t }])
        for (const a of p.audio) playPcm24k(a)
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
    off = { i: 0, o: 0, a: 0 }
    try {
      playCtx = new AudioContext({ sampleRate: 24000 })
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
      const wanted = chosen.get()
      const body = wanted ? { model: wanted } : {}
      if ((wanted || '').includes('extended-thinking')) body.thinkingLevel = thinkLevel.get()
      body.resume = true                 // backend auto-resumes when a valid handle exists
      const r = await ctx.rest('/start', { method: 'POST', timeoutMs: 30000, body })
      if (!r.ok) {
        stopMic()
        status.set('error')
        message.set(r.error || 'Start failed')
        addLog(`start failed: ${r.error}`)
        return
      }
      modelLabel.set(String(r.model || '').replace('models/', ''))
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
    chosen.set(v)
    try { ctx.storage.set('local.model', v) } catch {}
  }

  try { const saved = ctx.storage.get('local.model', ''); if (saved) chosen.set(saved) } catch {}

  async function fetchModels() {
    try {
      const cfg = await ctx.rest('/config')
      const list = (cfg.candidates || []).map(m => String(m).replace('models/', '')).filter(m => !m.includes('translate'))
      models.set(list.length ? list : [String(cfg.model || '').replace('models/', '')])
      if (!chosen.get() && models.get()[0]) chosen.set(models.get()[0]) // default = 3.8-live
    } catch { /* keep previous list */ }
  }

  ctx.onDispose(stop)
  return { status, message, transcript, level, modelLabel, models, chosen, pickerOpen, thinkLevel, thinkOpen, usage, log, start, stop, toggle: start, fetchModels, setChosen }
}

function Minibar({ live }) {
  const status = useValue(live.status)
  const message = useValue(live.message)
  const transcript = useValue(live.transcript)
  const level = useValue(live.level)
  const modelLabel = useValue(live.modelLabel)
  const logs = useValue(live.log)
  const models = useValue(live.models)
  const chosen = useValue(live.chosen)
  const pickerOpen = useValue(live.pickerOpen)
  const thinkLevel = useValue(live.thinkLevel)
  const thinkOpen = useValue(live.thinkOpen)
  const usage = useValue(live.usage)
  const active = status === 'live' || status === 'connecting'
  // Merge streaming fragments into flowing sentences per speaker: consecutive
  // same-role fragments concatenate; '—' (turnComplete) starts a new line.
  const merged = []
  for (const row of transcript) {
    if (row.role === 'sep') { merged.push({ role: 'sep', text: '' }); continue }
    const last = merged[merged.length - 1]
    if (last && last.role === row.role) last.text += row.text
    else merged.push({ role: row.role, text: row.text })
  }
  const tail = merged.filter(r => r.role !== 'sep').slice(-2)
  return jsxs('div', { className: 'gemini-live-bar', 'data-gemini-live-status': status, children: [
    jsx('span', { className: 'gemini-live-dot', 'data-active': active, 'aria-hidden': true }),
    jsx(Popover, {
      onOpenChange: open => { if (open) void live.fetchModels(); if (!open && status === 'idle') live.stop() },
      children: [
        jsx(PopoverTrigger, { asChild: true, children:
          jsx(Button, { variant: 'ghost', size: 'micro', 'aria-label': 'Gemini Live panel', children:
            jsx('span', { className: 'gemini-live-name', children: 'Gemini Live' })
          })
        }),
        jsx(PopoverContent, { side: 'top', align: 'end', className: 'gemini-live-panel', 'aria-label': 'Gemini Live', children:
          jsxs('div', { children: [
            jsxs('div', { className: 'gemini-live-status', children: [
              jsx('span', { className: 'gemini-live-dot', 'data-active': active, 'aria-hidden': true }),
              jsx('span', { children: status === 'idle' ? 'Idle' :
                            status === 'live' ? 'Live — just talk' :
                            status === 'connecting' ? 'Connecting…' : 'Error' }),
              jsx('span', { className: 'gemini-live-transport-space' }),
              jsx('span', { className: 'gemini-live-usage', title: 'Session tokens (free-tier meter)', children: usage ? `${usage} tok` : '' })
            ] }),
            active && jsx('div', { className: 'gemini-live-meter', 'aria-hidden': true, children:
              Array.from({ length: 8 }, (_, i) => jsx('span', { style: { height: `${Math.max(2, Math.min(16, (level / 100) * 16 * (1 - i * 0.09)))}px` } }, i))
            }),
            jsxs('div', { className: 'gemini-live-transport', children: [
              jsx(Tip, { label: active ? 'Stop session' : 'Start session', children: jsx(Button, {
                variant: 'ghost', size: 'icon-xs', onClick: live.toggle,
                children: jsx(active ? icons.Pause : icons.Play, { size: 12 })
              }) }),
              jsx('span', { className: 'gemini-live-transport-space' }),
              jsxs('div', { className: 'gemini-live-picker', children: [
                jsx('button', {
                  type: 'button', className: 'gemini-live-model', disabled: active,
                  title: 'Pick a Live model',
                  onClick: () => live.pickerOpen.set(!pickerOpen),
                  children: status === 'live' ? modelLabel : chosen
                }),
                pickerOpen && jsx('div', { className: 'gemini-live-picker-list', role: 'listbox', children:
                  models.map(m => jsx('div', {
                    role: 'option', 'aria-selected': chosen === m,
                    className: 'gemini-live-picker-item' + (chosen === m ? ' gemini-live-picker-item--sel' : ''),
                    onClick: () => { live.setChosen(m); live.pickerOpen.set(false) },
                    children: m
                  }, m))
                })
              ] })
            ] }),
            chosen === 'gemini-3.8-live-extended-thinking' && jsxs('div', { className: 'gemini-live-thinkrow', children: [
              jsxs('div', { className: 'gemini-live-thinkhead', children: [
                jsx('span', { className: 'gemini-live-thinklabel', children: 'Thinking:' }),
                jsx('button', { type: 'button', className: 'gemini-live-model gemini-live-thinkbtn', disabled: active,
                  onClick: () => live.thinkOpen.set(!thinkOpen), children: thinkLevel })
              ] }),
              thinkOpen && jsx('div', { className: 'gemini-live-thinklist', role: 'listbox', children:
                ['low', 'high'].map(lv => jsx('div', {
                  role: 'option', 'aria-selected': thinkLevel === lv,
                  className: 'gemini-live-picker-item' + (thinkLevel === lv ? ' gemini-live-picker-item--sel' : ''),
                  onClick: () => { live.thinkLevel.set(lv); live.thinkOpen.set(false) },
                  children: lv === 'low' ? 'low — fast thinking' : 'high — deep thinking'
                }, lv))
              })
            ] }),
            jsx('div', { className: 'gemini-live-error', role: 'status', children:
              message || (tail.length ? tail.map((row, i) => jsxs('div', { children: [row.role === 'you' ? 'you: ' : row.role === 'gemini' ? 'gemini: ' : '', row.text] }, i)) : '')
            }),
            jsx('div', { className: 'gemini-live-log', 'aria-label': 'diagnostics', children:
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
  name: 'Gemini Live',
  description: 'Realtime voice via Gemini Live API — full proxy in the local backend; the renderer never talks to Google.',
  register(ctx) {
    const style = document.createElement('style')
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())
    const live = createLive(ctx)
    ctx.register({ id: 'minibar', area: STATUSBAR_AREAS.right, order: 10, render: () => jsx(Minibar, { live }) })
  }
}
