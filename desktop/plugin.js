/**
 * Ariadne for Hermes Desktop.
 *
 * A page (/ariadne) to browse recorded sessions, replay them offline and watch the
 * replay land call by call, fork a session from any turn, and read the hook-contract
 * report. Plus a REC chip in the status bar, a mark on recorded sessions, palette
 * commands and the Ariadne theme.
 *
 * Plain ESM, loaded uncompiled: only @hermes/plugin-sdk, react and react/jsx-runtime.
 */

import {
  Badge, Button, cn, DecodeText, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle, EmptyState, haptic, host, Input, PALETTE_AREA, queryClient, relativeTime, ROUTES_AREA, SegmentedControl,
  SESSION_ROW_AREAS, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, Switch, THEMES_AREA, Tip, useQuery, useQueryClient,
  useTheme
} from '@hermes/plugin-sdk'
import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'

const ID = 'ariadne'
const ROUTE = '/ariadne'
const h = React.createElement

let api = null // ctx.rest, set in register()

const rest = (path, opts) => (api ? api(path, opts) : Promise.reject(new Error('Ariadne is not loaded')))

// --------------------------------------------------------------------------- live store

function createStore(initial) {
  let state = initial
  const subs = new Set()
  return {
    get: () => state,
    set(next) {
      state = typeof next === 'function' ? next(state) : next
      subs.forEach(fn => fn())
    },
    subscribe(fn) {
      subs.add(fn)
      return () => subs.delete(fn)
    }
  }
}

// job: { id, kind, cassette, state, run, result, error }, events: report lines in arrival order
const live = createStore({ job: null, events: [] })
const useLive = () => useSyncExternalStore(live.subscribe, live.get)

// --------------------------------------------------------------------------- styles

const CSS = `
.ar-root{position:relative;height:100%;overflow:auto;color:var(--ui-text-primary);isolation:isolate}
.ar-root *{box-sizing:border-box}
.ar-field{position:absolute;inset:0;width:100%;height:100%;z-index:-1;pointer-events:none;opacity:.9}
.ar-wrap{max-width:1180px;margin:0 auto;padding:40px 36px 80px}
.ar-display{font-family:'Syne',var(--font-sans,inherit);letter-spacing:-.04em;font-weight:700}
.ar-mono{font-family:'JetBrains Mono',var(--font-mono,ui-monospace),monospace}
.ar-eyebrow{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--ui-text-tertiary)}
.ar-hero{display:grid;grid-template-columns:auto 1fr auto;gap:32px;align-items:center;margin-bottom:36px}
.ar-title{font-size:clamp(44px,7vw,92px);line-height:.9;margin:6px 0 10px;display:flex}
.ar-title span{display:inline-block;opacity:0;transform:translateY(.45em) rotate(6deg);animation:ar-rise .9s cubic-bezier(.2,.8,.2,1) forwards}
.ar-sub{color:var(--ui-text-secondary);font-size:15px;max-width:520px;line-height:1.5}
.ar-sub b{color:var(--ui-accent);font-weight:600}
.ar-rec{display:flex;flex-direction:column;align-items:flex-end;gap:10px}
.ar-rec-row{display:flex;align-items:center;gap:10px;padding:10px 14px;border:1px solid var(--ui-stroke-secondary);border-radius:999px;background:color-mix(in srgb,var(--ui-bg-elevated,transparent) 70%,transparent);backdrop-filter:blur(8px)}
.ar-dot{width:9px;height:9px;border-radius:50%;background:var(--ui-text-quaternary)}
.ar-dot.on{background:var(--ui-red,#f43);box-shadow:0 0 0 0 color-mix(in srgb,var(--ui-red,#f43) 60%,transparent);animation:ar-pulse 1.6s infinite}
.ar-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--ui-stroke-tertiary);border:1px solid var(--ui-stroke-tertiary);border-radius:16px;overflow:hidden;margin-bottom:28px}
.ar-stat{padding:20px 22px;background:var(--ui-bg-editor,var(--background));position:relative;overflow:hidden}
.ar-stat::after{content:'';position:absolute;left:0;bottom:0;height:2px;width:100%;background:var(--ui-accent);transform:scaleX(0);transform-origin:left;animation:ar-bar 1.4s .3s cubic-bezier(.2,.8,.2,1) forwards}
.ar-stat .n{font-size:40px;line-height:1}
.ar-stat .l{margin-top:8px}
.ar-tabs{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;gap:12px}
.ar-grid{display:grid;grid-template-columns:minmax(260px,360px) 1fr;gap:18px;align-items:start}
.ar-list{display:flex;flex-direction:column;gap:8px}
.ar-card{text-align:left;width:100%;padding:14px 16px;border-radius:14px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-bg-card,transparent);cursor:pointer;transition:border-color .25s,transform .25s,background .25s;opacity:0;animation:ar-in .6s cubic-bezier(.2,.8,.2,1) forwards;position:relative;overflow:hidden;color:inherit}
.ar-card:hover{border-color:var(--ui-stroke-primary);transform:translateY(-1px)}
.ar-card.sel{border-color:var(--ui-accent);background:color-mix(in srgb,var(--ui-accent) 7%,transparent)}
.ar-card.sel::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ui-accent)}
.ar-card .t{font-size:13px;line-height:1.4;color:var(--ui-text-primary);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ar-card .m{display:flex;gap:10px;margin-top:8px;font-size:11px;color:var(--ui-text-tertiary)}
.ar-panel{border:1px solid var(--ui-stroke-tertiary);border-radius:18px;padding:22px;background:var(--ui-bg-card,transparent);min-height:420px;position:relative;overflow:hidden}
.ar-panel-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px}
.ar-h2{font-size:26px;line-height:1.05;margin:4px 0 6px}
.ar-actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.ar-progress{height:3px;border-radius:3px;background:var(--ui-stroke-tertiary);overflow:hidden;margin:4px 0 18px}
.ar-progress i{display:block;height:100%;background:linear-gradient(90deg,transparent,var(--ui-accent));transition:width .4s cubic-bezier(.2,.8,.2,1)}
.ar-progress.run i{background:linear-gradient(90deg,var(--ui-accent),color-mix(in srgb,var(--ui-accent) 40%,white),var(--ui-accent));background-size:200% 100%;animation:ar-sheen 1.2s linear infinite}
.ar-thread{position:relative;padding-left:34px}
.ar-thread svg.line{position:absolute;left:10px;top:6px;width:4px;height:calc(100% - 12px);overflow:visible}
.ar-thread svg.line path{stroke:var(--ui-stroke-secondary);stroke-width:2;fill:none}
.ar-thread svg.line path.lit{stroke:var(--ui-accent);stroke-dasharray:1;stroke-dashoffset:1;transition:stroke-dashoffset .6s cubic-bezier(.2,.8,.2,1)}
.ar-node{position:relative;padding:7px 0 7px;opacity:0;animation:ar-in .5s cubic-bezier(.2,.8,.2,1) forwards}
.ar-node .k{position:absolute;left:-30px;top:10px;width:14px;height:14px;border-radius:50%;border:2px solid var(--ui-stroke-primary);background:var(--ui-bg-editor,var(--background));transition:all .35s}
.ar-node.turn .k{border-radius:4px;border-color:var(--ui-text-secondary)}
.ar-node.match .k{border-color:var(--ui-accent);background:var(--ui-accent);box-shadow:0 0 0 5px color-mix(in srgb,var(--ui-accent) 18%,transparent)}
.ar-node.drift .k{border-color:var(--ui-red,#f43);background:var(--ui-bg-editor,var(--background));border-width:3px;box-shadow:0 0 0 6px color-mix(in srgb,var(--ui-red,#f43) 20%,transparent);animation:ar-shake .4s}
.ar-node.now .k{animation:ar-pulse 1s infinite}
.ar-node .row{display:flex;gap:10px;align-items:baseline;font-size:13px}
.ar-node .kind{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ui-text-tertiary);width:58px;flex:none}
.ar-node .txt{color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.ar-node.turn .txt{color:var(--ui-text-primary);white-space:normal}
.ar-node .diff{margin:8px 0 4px;padding:10px 12px;border-radius:10px;border:1px solid color-mix(in srgb,var(--ui-red,#f43) 35%,transparent);background:color-mix(in srgb,var(--ui-red,#f43) 6%,transparent);font-size:11.5px;line-height:1.55}
.ar-diff-line{display:grid;grid-template-columns:auto 1fr;gap:4px 10px}
.ar-del{color:var(--ui-diff-remove-foreground,var(--ui-red))}
.ar-add{color:var(--ui-diff-add-foreground,var(--ui-green))}
.ar-textdiff{grid-column:1/-1;margin-top:6px;padding-top:6px;border-top:1px dashed var(--ui-stroke-tertiary);white-space:pre-wrap;word-break:break-word}
.ar-hunk{color:var(--ui-text-quaternary)}
.ar-ctx{color:var(--ui-text-tertiary)}
.ar-verdict{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;border:1px solid currentColor}
.ar-verdict.identical{color:var(--ui-accent)}
.ar-verdict.drift,.ar-verdict.leak,.ar-verdict.error,.ar-verdict.failed{color:var(--ui-red,#f43)}
.ar-verdict.running{color:var(--ui-text-secondary)}
.ar-stamp{position:absolute;right:24px;top:72px;font-size:64px;line-height:1;opacity:0;transform:scale(1.6) rotate(-8deg);animation:ar-stamp .7s cubic-bezier(.2,1.4,.4,1) forwards;pointer-events:none}
.ar-stamp.identical{color:color-mix(in srgb,var(--ui-accent) 85%,transparent)}
.ar-stamp.drift{color:color-mix(in srgb,var(--ui-red,#f43) 85%,transparent)}
.ar-table{width:100%;border-collapse:collapse;font-size:13px}
.ar-table td,.ar-table th{text-align:left;padding:11px 10px;border-bottom:1px solid var(--ui-stroke-tertiary)}
.ar-table th{font-weight:500;color:var(--ui-text-tertiary);font-size:11px;letter-spacing:.14em;text-transform:uppercase}
.ar-table tr.click{cursor:pointer}
.ar-table tr.click:hover td{background:var(--ui-row-hover-background)}
.ar-chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 16px}
.ar-chip{font-size:11px;padding:3px 9px;border-radius:999px;border:1px solid var(--ui-stroke-secondary);color:var(--ui-text-secondary)}
.ar-chip b{color:var(--ui-text-primary);font-weight:600}
.ar-promise{display:grid;grid-template-columns:28px 1fr auto;gap:12px;padding:14px 0;border-bottom:1px solid var(--ui-stroke-tertiary);align-items:start;opacity:0;animation:ar-in .5s forwards}
.ar-promise .ok{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:12px;border:1px solid var(--ui-accent);color:var(--ui-accent)}
.ar-promise .bad{border-color:var(--ui-red,#f43);color:var(--ui-red,#f43)}
.ar-note{font-size:12px;color:var(--ui-text-tertiary);line-height:1.5}
.ar-chipbar{display:inline-flex;align-items:center;gap:6px;height:100%;padding:0 8px;font-size:11px;color:var(--ui-text-tertiary);background:none;border:0;cursor:pointer}
.ar-chipbar:hover{color:var(--ui-text-primary);background:var(--chrome-action-hover)}
.ar-chipbar .ar-dot{width:7px;height:7px}
.ar-sessmark{display:inline-block;width:10px;height:10px;opacity:.85}
@keyframes ar-rise{to{opacity:1;transform:none}}
@keyframes ar-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes ar-bar{to{transform:scaleX(1)}}
@keyframes ar-pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,currentColor 0%,transparent)}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
.ar-dot.on{color:var(--ui-red,#f43)}
@keyframes ar-sheen{to{background-position:-200% 0}}
@keyframes ar-shake{0%,100%{transform:none}25%{transform:translateX(-3px)}75%{transform:translateX(3px)}}
@keyframes ar-stamp{to{opacity:.16;transform:scale(1) rotate(-8deg)}}
@keyframes ar-spin{to{transform:rotate(360deg)}}
@keyframes ar-draw{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}
.ar-mark .r1{transform-origin:48px 48px;animation:ar-spin 48s linear infinite}
.ar-mark .r2{transform-origin:48px 48px;animation:ar-spin 32s linear infinite reverse}
.ar-mark .r3{transform-origin:48px 48px;animation:ar-spin 20s linear infinite}
.ar-mark .th{stroke-dasharray:1;stroke-dashoffset:1;animation:ar-draw 2.4s .2s cubic-bezier(.65,0,.35,1) forwards}
.ar-mark .knot{opacity:0;animation:ar-in .5s 2.4s forwards}
@media (max-width:860px){.ar-grid{grid-template-columns:1fr}.ar-hero{grid-template-columns:1fr}.ar-rec{align-items:flex-start}.ar-stats{grid-template-columns:repeat(2,1fr)}}
@media (prefers-reduced-motion:reduce){.ar-root *{animation-duration:.01ms!important;animation-iteration-count:1!important;transition:none!important}}
`

function useStyles() {
  useEffect(() => {
    if (document.getElementById('ariadne-style')) {
      return
    }
    const el = document.createElement('style')
    el.id = 'ariadne-style'
    el.textContent = CSS
    document.head.appendChild(el)
  }, [])
}

// --------------------------------------------------------------------------- mark

function arc(r, a0, a1) {
  const pt = a => {
    const t = ((a - 90) * Math.PI) / 180
    return [48 + r * Math.cos(t), 48 + r * Math.sin(t)]
  }
  const [x0, y0] = pt(a0)
  const [x1, y1] = pt(a1)
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${(a1 - a0) % 360 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`
}

const THREAD = 'M 48 94 L 48 80.5 A 32.5 32.5 0 0 1 48 15.5 L 48 28.5 A 19.5 19.5 0 0 1 48 67.5 L 48 50'
const RINGS = [
  [39, 200, 520],
  [26, 22, 338],
  [13, 200, 520]
]

function Mark({ size = 96, motion = false, wall = 'var(--ui-text-primary)' }) {
  const sw = size < 40 ? 6.5 : 4.4
  return h(
    'svg',
    { className: cn('ar-mark', !motion && 'still'), height: size, viewBox: '0 0 96 96', width: size, 'aria-hidden': true },
    RINGS.slice(0, size < 40 ? 2 : 3).map(([r, a0, a1], i) =>
      h('path', {
        className: motion ? `r${i + 1}` : undefined,
        d: arc(r, a0, a1),
        fill: 'none',
        key: r,
        opacity: 0.92,
        stroke: wall,
        strokeLinecap: 'round',
        strokeWidth: sw
      })
    ),
    h('path', {
      className: motion ? 'th' : undefined,
      d: THREAD,
      fill: 'none',
      pathLength: 1,
      stroke: 'var(--ui-accent)',
      strokeLinecap: 'round',
      strokeLinejoin: 'round',
      strokeWidth: sw * 1.1
    }),
    h('circle', { className: motion ? 'knot' : undefined, cx: 48, cy: 48, fill: 'var(--ui-accent)', r: sw })
  )
}

// --------------------------------------------------------------------------- thread field (canvas)

function ThreadField() {
  const ref = useRef(null)
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) {
      return
    }
    const g = canvas.getContext('2d')
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let w = 0
    let hgt = 0
    let raf = 0
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const resize = () => {
      const r = canvas.getBoundingClientRect()
      w = r.width
      hgt = r.height
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(hgt * dpr)
      g.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    const ro = new ResizeObserver(resize)
    ro.observe(canvas)
    resize()
    const style = getComputedStyle(canvas)
    const accent = style.getPropertyValue('--ui-accent').trim() || '#ff5a1f'
    const ink = style.getPropertyValue('--ui-text-quaternary').trim() || '#888'
    const lines = Array.from({ length: 9 }, (_, i) => ({
      amp: 18 + i * 7,
      freq: 0.0022 + i * 0.00031,
      phase: i * 1.7,
      speed: 0.00018 + i * 0.000035,
      y: 0.12 + i * 0.05,
      hot: i === 4
    }))
    const draw = t => {
      g.clearRect(0, 0, w, hgt)
      for (const l of lines) {
        g.beginPath()
        for (let x = -10; x <= w + 10; x += 8) {
          const y = l.y * hgt + Math.sin(x * l.freq + l.phase + t * l.speed) * l.amp + Math.sin(x * l.freq * 2.3 + t * l.speed * 1.7) * l.amp * 0.25
          x === -10 ? g.moveTo(x, y) : g.lineTo(x, y)
        }
        g.strokeStyle = l.hot ? accent : ink
        g.globalAlpha = l.hot ? 0.55 : 0.12
        g.lineWidth = l.hot ? 1.6 : 1
        g.stroke()
        if (l.hot) {
          // a spark running along the live thread
          const sx = ((t * 0.08) % (w + 200)) - 100
          const sy = l.y * hgt + Math.sin(sx * l.freq + l.phase + t * l.speed) * l.amp + Math.sin(sx * l.freq * 2.3 + t * l.speed * 1.7) * l.amp * 0.25
          const grad = g.createRadialGradient(sx, sy, 0, sx, sy, 26)
          grad.addColorStop(0, accent)
          grad.addColorStop(1, 'transparent')
          g.globalAlpha = 0.5
          g.fillStyle = grad
          g.fillRect(sx - 26, sy - 26, 52, 52)
        }
      }
      g.globalAlpha = 1
      if (!reduce) {
        raf = requestAnimationFrame(draw)
      }
    }
    raf = requestAnimationFrame(draw)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
    }
  }, [])
  return h('canvas', { className: 'ar-field', ref })
}

// --------------------------------------------------------------------------- small pieces

function CountUp({ value }) {
  const [n, setN] = useState(0)
  useEffect(() => {
    const target = Number(value) || 0
    const start = performance.now()
    let raf = 0
    const step = now => {
      const p = Math.min(1, (now - start) / 1100)
      setN(Math.round(target * (1 - Math.pow(1 - p, 4))))
      if (p < 1) {
        raf = requestAnimationFrame(step)
      }
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [value])
  return h('span', null, n)
}

function Verdict({ value }) {
  return h('span', { className: cn('ar-verdict', value) }, value === 'running' ? 'replaying' : value || '—')
}

function Title() {
  return h(
    'h1',
    { className: 'ar-title ar-display' },
    'Ariadne'.split('').map((c, i) => h('span', { key: i, style: { animationDelay: `${0.15 + i * 0.06}s` } }, c))
  )
}

const short = (s, n = 90) => (s && s.length > n ? `${s.slice(0, n - 1)}…` : s || '')
const fmtVal = v => (v === undefined ? '∅' : typeof v === 'string' ? v : JSON.stringify(v))

// --------------------------------------------------------------------------- data

const KEY = ['plugin:ariadne']
const useStatus = () => useQuery({ queryKey: [...KEY, 'status'], queryFn: () => rest('/status'), refetchInterval: 10000 })
const useCassettes = () => useQuery({ queryKey: [...KEY, 'cassettes'], queryFn: () => rest('/cassettes'), refetchInterval: 15000 })

async function setRecording(on, qc) {
  await rest('/record', { body: { on }, method: 'POST' })
  haptic('tap')
  host.notify({ kind: 'info', message: on ? 'Ariadne is recording new sessions' : 'Ariadne recording is off' })
  qc && qc.invalidateQueries({ queryKey: KEY })
}

async function startJob(kind, body) {
  try {
    const job = await rest(`/${kind}`, { body, method: 'POST' })
    live.set({ job, events: [] })
    haptic('tap')
    return job
  } catch (err) {
    host.notifyError ? host.notifyError(String(err.message || err)) : host.notify({ kind: 'error', message: String(err) })
    return null
  }
}

// --------------------------------------------------------------------------- library

function CassetteCard({ c, i, selected, onSelect }) {
  return h(
    'button',
    { className: cn('ar-card', selected && 'sel'), onClick: onSelect, style: { animationDelay: `${0.04 * i}s` }, type: 'button' },
    h('div', { className: 't' }, c.first_message || c.id),
    h(
      'div',
      { className: 'm ar-mono' },
      h('span', null, `${c.turns} turn${c.turns === 1 ? '' : 's'}`),
      h('span', null, `${c.model_calls} calls`),
      h('span', null, `${c.tools} tools`),
      h('span', { style: { marginLeft: 'auto' } }, c.created ? relativeTime(Date.parse(c.created)) : '')
    )
  )
}

function nodeLabel(e) {
  switch (e.kind) {
    case 'turn':
      return e.text
    case 'llm':
      return `model call #${e.call}${e.elapsed_ms ? ` · ${e.elapsed_ms} ms recorded` : ''}`
    case 'tool':
      return `${e.tool}  ${e.args || ''}`
    case 'aux':
      return `auxiliary · ${e.task || 'call'}`
    case 'compaction':
      return `context compressed${e.reason ? ` · ${e.reason}` : ''}`
    case 'subagent':
      return `subagent ${e.child || ''}`
    default:
      return e.kind
  }
}

function Diff({ diffs }) {
  return h(
    'div',
    { className: 'diff ar-mono' },
    (diffs || []).slice(0, 6).map((d, i) =>
      h(
        'div',
        { className: 'ar-diff-line', key: i },
        h('span', { style: { color: 'var(--ui-text-tertiary)' } }, d.path),
        h('span', null, d.change),
        d.recorded !== undefined && h('span', { className: 'ar-del' }, '−'),
        d.recorded !== undefined && h('span', { className: 'ar-del' }, short(fmtVal(d.recorded), 160)),
        d.live !== undefined && h('span', { className: 'ar-add' }, '+'),
        d.live !== undefined && h('span', { className: 'ar-add' }, short(fmtVal(d.live), 160)),
        d.text_diff && h(TextDiff, { lines: d.text_diff })
      )
    )
  )
}

function TextDiff({ lines }) {
  const shown = lines.filter(l => !l.startsWith('---') && !l.startsWith('+++')).slice(0, 14)
  return h(
    'div',
    { className: 'ar-textdiff' },
    shown.map((l, i) =>
      h('div', { className: l.startsWith('+') ? 'ar-add' : l.startsWith('-') ? 'ar-del' : l.startsWith('@@') ? 'ar-hunk' : 'ar-ctx', key: i }, short(l, 220) || ' ')
    )
  )
}

function Thread({ timeline, results, running }) {
  const done = timeline.filter(e => results[e.seq]).length
  const lastSeq = Object.keys(results).map(Number).sort((a, b) => b - a)[0]
  const frac = timeline.length ? done / timeline.filter(e => e.kind === 'llm' || e.kind === 'tool').length : 0
  return h(
    'div',
    { className: 'ar-thread' },
    h(
      'svg',
      { className: 'line', preserveAspectRatio: 'none', viewBox: '0 0 4 100' },
      h('path', { d: 'M2 0 L2 100' }),
      h('path', { className: 'lit', d: 'M2 0 L2 100', pathLength: 1, style: { strokeDashoffset: 1 - Math.min(1, frac) } })
    ),
    timeline
      .filter(e => e.kind !== 'aux' || timeline.length < 40)
      .map((e, i) => {
        const r = results[e.seq]
        return h(
          'div',
          {
            className: cn('ar-node', e.kind, r && (r.drift ? 'drift' : 'match'), running && e.seq === lastSeq && 'now'),
            key: e.seq,
            style: { animationDelay: `${Math.min(i, 30) * 0.025}s` }
          },
          h('span', { className: 'k' }),
          h('div', { className: 'row' }, h('span', { className: 'kind ar-mono' }, e.kind === 'llm' ? 'model' : e.kind), h('span', { className: 'txt' }, short(nodeLabel(e), 160))),
          r && r.drift && r.diffs && r.diffs.length > 0 && h(Diff, { diffs: r.diffs })
        )
      })
  )
}

function resultsFrom(events) {
  const out = {}
  for (const ev of events) {
    if (ev.seq == null) {
      continue
    }
    if (ev.type === 'llm_match' || ev.type === 'tool_match') {
      out[ev.seq] = { drift: false }
    } else if (ev.type === 'llm_drift' || ev.type === 'tool_drift') {
      out[ev.seq] = { diffs: ev.diffs, drift: true }
    }
  }
  return out
}

function ForkDialog({ open, onOpenChange, cassette, turns }) {
  const [turn, setTurn] = useState('')
  const [prompt, setPrompt] = useState('')
  return h(
    Dialog,
    { onOpenChange, open },
    h(
      DialogContent,
      null,
      h(DialogHeader, null, h(DialogTitle, null, 'Fork this session'), h(DialogDescription, null, 'Everything before the fork is replayed offline from the recording. From the fork on, Hermes runs live with your model, and tools that change things are refused unless you allow them.')),
      h(
        'div',
        { style: { display: 'grid', gap: 10 } },
        h('label', { className: 'ar-note' }, `Fork at turn (1–${turns || 1})`),
        h(Input, { onChange: e => setTurn(e.target.value), placeholder: String(turns || 1), value: turn }),
        h('label', { className: 'ar-note' }, 'New message for that turn (optional)'),
        h(Input, { onChange: e => setPrompt(e.target.value), placeholder: 'Try the same task, but …', value: prompt })
      ),
      h(
        DialogFooter,
        null,
        h(Button, { onClick: () => onOpenChange(false), variant: 'ghost' }, 'Cancel'),
        h(
          Button,
          {
            onClick: async () => {
              const job = await startJob('fork', { at_turn: Number(turn || turns || 1), cassette, prompt: prompt || null })
              job && onOpenChange(false)
            }
          },
          'Start fork'
        )
      )
    )
  )
}

function Detail({ id }) {
  const q = useQuery({ enabled: !!id, queryKey: [...KEY, 'cassette', id], queryFn: () => rest(`/cassettes/${encodeURIComponent(id)}`) })
  const { job, events } = useLive()
  const [forkOpen, setForkOpen] = useState(false)
  const mine = job && job.cassette === id
  const results = useMemo(() => (mine ? resultsFrom(events) : {}), [mine, events])
  if (!id) {
    return h('div', { className: 'ar-panel' }, h(EmptyState, { description: 'Pick a recording on the left.', title: 'No session selected' }))
  }
  if (!q.data) {
    return h('div', { className: 'ar-panel' }, h(DecodeText, { loop: true, text: 'unwinding the thread…' }))
  }
  const { summary, timeline } = q.data
  const expected = summary.model_calls + summary.tools
  const served = Object.keys(results).length
  const running = mine && job.state === 'running'
  const verdict = mine ? (running ? 'running' : job.state === 'failed' ? 'failed' : job.result && job.result.verdict) : null
  return h(
    'div',
    { className: 'ar-panel', key: id },
    verdict && !running && h('div', { className: cn('ar-stamp ar-display', verdict) }, verdict === 'identical' ? 'IDENTICAL' : 'DRIFT'),
    h(
      'div',
      { className: 'ar-panel-head' },
      h(
        'div',
        { style: { minWidth: 0 } },
        h('div', { className: 'ar-eyebrow ar-mono' }, `${summary.platform || 'session'} · ${summary.model || 'model'} · ${summary.id}`),
        h('div', { className: 'ar-h2 ar-display' }, short(summary.first_message || summary.id, 80)),
        h('div', { className: 'ar-chips' }, ['turns', 'model_calls', 'tools', 'subagents', 'compactions'].filter(k => summary[k]).map(k => h('span', { className: 'ar-chip', key: k }, h('b', null, summary[k]), ` ${k.replace('_', ' ')}`)))
      ),
      h(
        'div',
        { className: 'ar-actions' },
        verdict && h(Verdict, { value: verdict }),
        h(Button, { disabled: !!(job && job.state === 'running'), onClick: () => startJob('play', { cassette: id }) }, running ? 'Replaying…' : 'Replay offline'),
        h(Button, { disabled: !!(job && job.state === 'running'), onClick: () => setForkOpen(true), variant: 'outline' }, 'Fork…')
      )
    ),
    mine && h('div', { className: cn('ar-progress', running && 'run') }, h('i', { style: { width: `${expected ? Math.min(100, (served / expected) * 100) : running ? 15 : 100}%` } })),
    mine && job.error && h('div', { className: 'ar-note', style: { color: 'var(--ui-red)' } }, job.error),
    h(Thread, { results, running, timeline }),
    h(ForkDialog, { cassette: id, onOpenChange: setForkOpen, open: forkOpen, turns: summary.turns })
  )
}

function Library() {
  const q = useCassettes()
  const list = (q.data && q.data.cassettes) || []
  const [sel, setSel] = useState(null)
  const current = sel || (list[0] && list[0].id)
  if (q.isSuccess && !list.length) {
    return h(EmptyState, {
      description: 'Turn recording on, then chat with Hermes as usual. Each session is saved here, ready to replay without a model.',
      title: 'No recordings yet'
    })
  }
  return h(
    'div',
    { className: 'ar-grid' },
    h('div', { className: 'ar-list' }, list.map((c, i) => h(CassetteCard, { c, i, key: c.id, onSelect: () => setSel(c.id), selected: c.id === current }))),
    h(Detail, { id: current })
  )
}

// --------------------------------------------------------------------------- runs

function RunDetail({ id, onBack }) {
  const q = useQuery({ queryKey: [...KEY, 'run', id], queryFn: () => rest(`/runs/${encodeURIComponent(id)}`) })
  if (!q.data) {
    return h(DecodeText, { loop: true, text: 'reading the report…' })
  }
  const { summary, events } = q.data
  const counts = (summary && summary.counts) || {}
  const notable = events.filter(e => /drift|leak|violation|missing|off_script|extra|environment|refused|frozen/.test(e.type))
  return h(
    'div',
    { className: 'ar-panel' },
    h(
      'div',
      { className: 'ar-panel-head' },
      h('div', null, h('div', { className: 'ar-eyebrow ar-mono' }, q.data.kind), h('div', { className: 'ar-h2 ar-display' }, id)),
      h('div', { className: 'ar-actions' }, summary && summary.verdict && h(Verdict, { value: summary.verdict }), h(Button, { onClick: onBack, variant: 'ghost' }, 'Back'))
    ),
    h('div', { className: 'ar-chips' }, Object.entries(counts).map(([k, v]) => h('span', { className: 'ar-chip', key: k }, h('b', null, v), ` ${k.replace(/_/g, ' ')}`))),
    notable.length === 0
      ? h('div', { className: 'ar-note' }, 'Nothing to report: every model request and tool call matched the recording.')
      : notable.map((e, i) =>
          h(
            'div',
            { className: cn('ar-node', /drift|leak|violation|missing/.test(e.type) ? 'drift' : 'match'), key: i, style: { animationDelay: `${Math.min(i, 20) * 0.03}s`, paddingLeft: 30 } },
            h('span', { className: 'k', style: { left: 0 } }),
            h('div', { className: 'row' }, h('span', { className: 'kind ar-mono', style: { width: 'auto' } }, e.type.replace(/_/g, ' ')), h('span', { className: 'txt' }, e.promise || e.note || (e.sections && e.sections.join(', ')) || e.tool_name || '')),
            e.diffs && e.diffs.length > 0 && h(Diff, { diffs: e.diffs }),
            e.lines && h(Diff, { diffs: [{ change: 'environment', live: e.lines.live, path: 'system prompt', recorded: e.lines.recorded }] })
          )
        )
  )
}

function Runs() {
  const q = useQuery({ queryKey: [...KEY, 'runs'], queryFn: () => rest('/runs'), refetchInterval: 15000 })
  const [sel, setSel] = useState(null)
  if (sel) {
    return h(RunDetail, { id: sel, onBack: () => setSel(null) })
  }
  const runs = (q.data && q.data.runs) || []
  if (q.isSuccess && !runs.length) {
    return h(EmptyState, { description: 'Replay a recording from the Library and the report lands here.', title: 'No replays yet' })
  }
  return h(
    'div',
    { className: 'ar-panel', style: { minHeight: 0 } },
    h(
      'table',
      { className: 'ar-table' },
      h('thead', null, h('tr', null, ['Run', 'Kind', 'Verdict', 'Turns'].map(t => h('th', { key: t }, t)))),
      h(
        'tbody',
        null,
        runs.map(r =>
          h(
            'tr',
            { className: 'click', key: r.id, onClick: () => setSel(r.id) },
            h('td', { className: 'ar-mono' }, short(r.id, 48)),
            h('td', null, r.kind === 'run' ? 'replay' : r.kind),
            h('td', null, h(Verdict, { value: r.verdict || (r.prefix_identical === false ? 'drift' : r.kind === 'fork' ? 'forked' : '') })),
            h('td', { className: 'ar-mono' }, r.turns_total != null ? `${r.turns_sent}/${r.turns_total}` : '')
          )
        )
      )
    )
  )
}

// --------------------------------------------------------------------------- contracts

function Contracts() {
  const q = useQuery({ queryKey: [...KEY, 'contracts'], queryFn: () => rest('/contracts'), refetchInterval: 20000 })
  if (!q.data) {
    return h(DecodeText, { loop: true, text: 'checking promises…' })
  }
  const counts = {}
  for (const v of q.data.violations) {
    counts[v.rule] = (counts[v.rule] || 0) + 1
  }
  return h(
    'div',
    { className: 'ar-panel', style: { minHeight: 0 } },
    h('div', { className: 'ar-eyebrow ar-mono' }, 'Hook contracts'),
    h('div', { className: 'ar-h2 ar-display', style: { marginBottom: 6 } }, 'What Hermes promises its plugins'),
    h('div', { className: 'ar-note', style: { marginBottom: 10 } }, 'Each rule quotes Hermes’s own hook docs. Ariadne checks them on every recorded turn and every replay. A broken promise is a bug you can file.'),
    Object.entries(q.data.promises).map(([rule, text], i) =>
      h(
        'div',
        { className: 'ar-promise', key: rule, style: { animationDelay: `${i * 0.04}s` } },
        h('span', { className: cn('ok', counts[rule] && 'bad') }, counts[rule] ? '!' : '✓'),
        h('div', null, h('div', { className: 'ar-mono', style: { fontSize: 12, marginBottom: 3 } }, rule), h('div', { className: 'ar-note' }, text)),
        counts[rule] ? h(Badge, { variant: 'destructive' }, `${counts[rule]} broken`) : h('span', { className: 'ar-note' }, 'kept')
      )
    )
  )
}

// --------------------------------------------------------------------------- page

function ThemeButton() {
  const { availableThemes, setTheme, themeName } = useTheme()
  const has = (availableThemes || []).some(t => t.name === 'ariadne')
  if (!has || themeName === 'ariadne') {
    return null
  }
  return h(Button, { onClick: () => setTheme('ariadne'), size: 'sm', variant: 'ghost' }, 'Use the Ariadne theme')
}

function Page() {
  useStyles()
  const qc = useQueryClient()
  const status = useStatus()
  const s = status.data || {}
  const [tab, setTab] = useState('library')
  const ran = useQuery({ queryKey: [...KEY, 'runs'], queryFn: () => rest('/runs') })
  const runs = (ran.data && ran.data.runs) || []
  const identical = runs.filter(r => r.verdict === 'identical').length
  return h(
    'div',
    { className: 'ar-root' },
    h(ThreadField),
    h(
      'div',
      { className: 'ar-wrap' },
      h(
        'div',
        { className: 'ar-hero' },
        h(Mark, { motion: true, size: 132 }),
        h(
          'div',
          null,
          h('div', { className: 'ar-eyebrow ar-mono' }, 'Session recorder · offline replay · bisect · fork'),
          h(Title),
          h('div', { className: 'ar-sub' }, 'Every Hermes session is a thread. Record it once, then ', h('b', null, 'replay it with no model, no network and no tokens'), ' to see exactly what changed.')
        ),
        h(
          'div',
          { className: 'ar-rec' },
          h(
            'div',
            { className: 'ar-rec-row' },
            h('span', { className: cn('ar-dot', s.recording && 'on') }),
            h('span', { className: 'ar-mono', style: { fontSize: 12 } }, s.recording ? 'RECORDING' : 'NOT RECORDING'),
            h(Switch, { checked: !!s.recording, onCheckedChange: v => setRecording(v, qc) })
          ),
          h(ThemeButton)
        )
      ),
      h(
        'div',
        { className: 'ar-stats' },
        [
          ['Recordings', s.cassettes],
          ['Replays', runs.length],
          ['Identical', identical],
          ['Broken promises', s.contract_violations]
        ].map(([label, value]) => h('div', { className: 'ar-stat', key: label }, h('div', { className: 'n ar-display' }, h(CountUp, { value: value || 0 })), h('div', { className: 'l ar-eyebrow ar-mono' }, label)))
      ),
      h(
        'div',
        { className: 'ar-tabs' },
        h(SegmentedControl, {
          onChange: setTab,
          options: [
            { id: 'library', label: 'Library' },
            { id: 'runs', label: 'Replays' },
            { id: 'contracts', label: 'Contracts' }
          ],
          sizing: 'content',
          value: tab
        }),
        h('span', { className: 'ar-note ar-mono' }, s.version ? `ariadne ${s.version}` : '')
      ),
      tab === 'library' ? h(Library) : tab === 'runs' ? h(Runs) : h(Contracts)
    )
  )
}

// --------------------------------------------------------------------------- chrome

function RecChip() {
  useStyles()
  const qc = useQueryClient()
  const s = useStatus().data || {}
  const { job } = useLive()
  const busy = job && job.state === 'running'
  return h(
    Tip,
    { label: s.recording ? 'Ariadne is recording. Click to stop.' : 'Ariadne: click to record new sessions' },
    h(
      'button',
      { className: 'ar-chipbar', onClick: () => setRecording(!s.recording, qc), type: 'button' },
      h('span', { className: cn('ar-dot', s.recording && 'on') }),
      busy ? 'replaying' : s.recording ? 'REC' : 'ariadne'
    )
  )
}

function SessionMark({ sessionId }) {
  const q = useQuery({ queryKey: [...KEY, 'sessions'], queryFn: () => rest('/sessions'), refetchInterval: 30000, staleTime: 15000 })
  const cid = q.data && q.data.sessions && q.data.sessions[sessionId]
  if (!cid) {
    return null
  }
  return h(Tip, { label: 'Recorded by Ariadne' }, h('span', { className: 'ar-sessmark' }, h(Mark, { size: 10, wall: 'var(--ui-text-tertiary)' })))
}

// --------------------------------------------------------------------------- theme

const ARIADNE_THEME = {
  name: 'ariadne',
  label: 'Ariadne',
  description: 'Jet black, soft white, flame orange',
  colors: {
    background: '#F2EFE9',
    foreground: '#0A0A0B',
    card: '#FBF9F5',
    cardForeground: '#0A0A0B',
    muted: '#E7E2D9',
    mutedForeground: '#6B655D',
    popover: '#FBF9F5',
    popoverForeground: '#0A0A0B',
    primary: '#E8490F',
    primaryForeground: '#FBF9F5',
    secondary: '#E7E2D9',
    secondaryForeground: '#0A0A0B',
    accent: '#F6DCCD',
    accentForeground: '#7A2306',
    border: '#DDD6CB',
    input: '#DDD6CB',
    ring: '#E8490F',
    midground: '#E8490F',
    destructive: '#C62828',
    destructiveForeground: '#FBF9F5',
    sidebarBackground: '#ECE8E0',
    sidebarBorder: '#DDD6CB',
    userBubble: '#F6E6DC',
    userBubbleBorder: '#EBC7B3'
  },
  darkColors: {
    background: '#0A0A0B',
    foreground: '#F2EFE9',
    card: '#111113',
    cardForeground: '#F2EFE9',
    muted: '#18181B',
    mutedForeground: '#8A8580',
    popover: '#121214',
    popoverForeground: '#F2EFE9',
    primary: '#FF5A1F',
    primaryForeground: '#0A0A0B',
    secondary: '#1A1A1D',
    secondaryForeground: '#F2EFE9',
    accent: '#22180F',
    accentForeground: '#FFB38A',
    border: '#24201D',
    input: '#24201D',
    ring: '#FF5A1F',
    midground: '#FF5A1F',
    destructive: '#FF4D4D',
    destructiveForeground: '#0A0A0B',
    sidebarBackground: '#070708',
    sidebarBorder: '#1A1715',
    userBubble: '#1A1411',
    userBubbleBorder: '#3A2418'
  },
  typography: {
    fontSans: "'Inter', ui-sans-serif, system-ui, sans-serif",
    fontMono: "'JetBrains Mono', ui-monospace, monospace",
    fontUrl: 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&family=Syne:wght@600;700;800&display=swap'
  }
}

// --------------------------------------------------------------------------- register

export default {
  id: ID,
  name: 'Ariadne',
  register(ctx) {
    api = ctx.rest
    const refresh = () => queryClient.invalidateQueries({ queryKey: KEY })

    ctx.onEvent('plugin.ariadne.run.started', ({ payload }) => live.set(s => ({ ...s, job: { ...(s.job || {}), ...payload } })))
    ctx.onEvent('plugin.ariadne.run.event', ({ payload }) =>
      live.set(s => (s.job && payload.job === s.job.id ? { ...s, events: [...s.events, payload.event] } : s))
    )
    ctx.onEvent('plugin.ariadne.run.finished', ({ payload }) => {
      live.set(s => ({ ...s, job: payload }))
      refresh()
      const v = payload.result && (payload.result.verdict || (payload.result.prefix_identical ? 'identical' : 'drift'))
      host.notify({ kind: v === 'identical' ? 'success' : 'info', message: payload.error ? `Ariadne ${payload.kind} failed` : `Ariadne ${payload.kind}: ${v}` })
      ctx.os.notify({ title: `Ariadne ${payload.kind} finished`, body: payload.error || `Verdict: ${v}`, activate: ROUTE })
    })
    ctx.onEvent('plugin.ariadne.record.changed', refresh)

    // Socket-less fallback: poll the running job.
    ctx.setInterval(async () => {
      const { job } = live.get()
      if (!job || job.state !== 'running') {
        return
      }
      try {
        const now = await rest(`/jobs/${job.id}`)
        if (now.run && now.state === 'running') {
          const run = await rest(`/runs/${encodeURIComponent(now.run)}`)
          live.set(s => (s.job && s.job.id === now.id ? { job: { ...s.job, ...now }, events: run.events } : s))
        } else if (now.state !== 'running') {
          live.set(s => ({ ...s, job: now }))
          refresh()
        }
      } catch (_) {
        /* the gateway may be restarting; the next tick retries */
      }
    }, 2500)

    ctx.onDispose(() => document.getElementById('ariadne-style')?.remove())

    ctx.registerMany([
      { area: ROUTES_AREA, data: { path: ROUTE }, id: 'page', render: () => h(Page) },
      { area: SIDEBAR_NAV_AREA, data: { codicon: 'debug-alt', label: 'Ariadne', path: ROUTE }, id: 'nav' },
      { area: STATUSBAR_AREAS.right, id: 'rec', order: 125, render: () => h(RecChip) },
      { area: SESSION_ROW_AREAS.trailing, data: { render: ({ sessionId }) => h(SessionMark, { sessionId }) }, id: 'session-mark' },
      { area: THEMES_AREA, data: ARIADNE_THEME, id: 'theme' },
      { area: PALETTE_AREA, data: { id: 'ariadne.open', keywords: ['replay', 'record', 'cassette'], label: 'Ariadne: Open', run: () => host.navigate(ROUTE) }, id: 'open' },
      {
        area: PALETTE_AREA,
        data: {
          id: 'ariadne.record',
          keywords: ['record', 'cassette'],
          label: 'Ariadne: Toggle recording',
          run: async () => {
            const s = await rest('/status')
            await setRecording(!s.recording)
            refresh()
          }
        },
        id: 'record'
      },
      {
        area: PALETTE_AREA,
        data: {
          id: 'ariadne.replay-latest',
          keywords: ['replay', 'play', 'offline'],
          label: 'Ariadne: Replay the latest recording',
          run: async () => {
            host.navigate(ROUTE)
            await startJob('play', { cassette: 'latest' })
          }
        },
        id: 'replay-latest'
      }
    ])
  }
}
