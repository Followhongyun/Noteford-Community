import { readElementGeometry } from './g3-corners.mjs'
import { contourPixels, surfaceSvg, nativeRequest } from './paint.mjs'

const SIDES = ['Top', 'Right', 'Bottom', 'Left']
const RADII = ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomRightRadius', 'borderBottomLeftRadius']
const cache = new Map()
const LIMIT = 96
const url = svg => `url("data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}")`

function png(data, width, height) {
  const canvas = document.createElement('canvas')
  canvas.width = width; canvas.height = height
  canvas.getContext('2d').putImageData(new ImageData(data, width, height), 0, 0)
  return canvas.toDataURL('image/png')
}

function masksFor(g, scale, widths) {
  const key = JSON.stringify(['g3-v2', g.width, g.height, g.usedRadii, scale, widths])
  if (cache.has(key)) { const entry = cache.get(key); cache.delete(key); cache.set(key, entry); return entry }
  const pixels = contourPixels(g, scale, widths)
  const entry = { outer: png(pixels.outer, pixels.width, pixels.height), strokes: pixels.strokes.map(data => data ? png(data, pixels.width, pixels.height) : null) }
  cache.set(key, entry)
  if (cache.size > LIMIT) cache.delete(cache.keys().next().value)
  return entry
}

/** App-wide enhancement of authored CSS radii. Prefer CALayer.continuous on macOS;
 * the canonical skill generator handles unsupported geometry and other platforms.
 * No React tree changes or layout wrappers.
 * Native outlines and box-shadow parameters remain native (not claimed as G3 shadows).
 * System controls, zero corners and SVG icon paths remain owned by their renderer.
 */
export function installAppleCorners(root) {
  const entries = new Map(), dirty = new Set(), candidates = new Set(), animated = new Map(), ownStyles = new WeakMap(), nativeCache = new Map()
  let raf = 0, stopped = false, scale = window.devicePixelRatio || 1
  const media = window.matchMedia('(prefers-color-scheme: dark)')
  const excluded = 'svg, svg *, canvas, option, input[type="checkbox"], input[type="radio"], input[type="range"], [data-native-corners]'

  function eligible(element) {
    return element instanceof HTMLElement && !element.matches(excluded) && !element.closest('[data-native-corners]')
  }
  function queue(element) { if (!stopped && eligible(element)) { dirty.add(element); if (!raf) raf = requestAnimationFrame(flush) } }
  function scan(element) {
    if (!(element instanceof Element)) return
    queue(element)
    for (const child of element.querySelectorAll('*')) queue(child)
  }
  function ancestorQueue(element) {
    while (element instanceof HTMLElement && root.contains(element)) { queue(element); element = element.parentElement }
  }
  function clear(element) {
    element.removeAttribute('data-apple-corners')
    element.removeAttribute('data-apple-clip')
    element.removeAttribute('data-apple-shadow')
    element.style.removeProperty('--apple-corner-paint')
    element.style.removeProperty('--apple-corner-mask')
    ownStyles.set(element, element.getAttribute('style'))
  }

  function paint(element) {
    if (!root.contains(element) && element !== root) { resize.unobserve(element); entries.delete(element); candidates.delete(element); return }
    const previous = entries.get(element)
    // Remove only our clipping override to read the original per-corner radius.
    element.removeAttribute('data-apple-clip')
    const cs = getComputedStyle(element)
    if (cs.display === 'none' || !element.getClientRects().length) { if (previous) clear(element); return }
    if (RADII.every(name => parseFloat(cs[name]) === 0)) { if (previous) clear(element); return }
    if (cs.maskImage !== 'none' || SIDES.some(side => parseFloat(cs[`border${side}Width`]) > 0 && !['none', 'solid'].includes(cs[`border${side}Style`]))) {
      throw Error('Preserve an existing mask or unsupported border style')
    }
    if (!candidates.has(element)) { candidates.add(element); resize.observe(element) }
    const g = readElementGeometry(element)
    if (g.width <= 0 || g.height <= 0) return
    const widths = SIDES.map(side => cs[`border${side}Style`] === 'solid' ? parseFloat(cs[`border${side}Width`]) || 0 : 0)
    const style = { color: cs.backgroundColor, image: cs.backgroundImage, borders: SIDES.map(side => cs[`border${side}Color`]) }
    const hasShadow = cs.boxShadow !== 'none'
    const clips = ['hidden', 'clip', 'auto', 'scroll'].includes(cs.overflowX) || ['hidden', 'clip', 'auto', 'scroll'].includes(cs.overflowY) || element.matches('img, video')
    const key = JSON.stringify([g.width, g.height, g.usedRadii, scale, widths, style])
    let masks, renderer = g.kind
    const request = window.noteford?.platform === 'darwin' && window.noteford.nativeCorners && nativeRequest(g, scale, widths, style.borders)
    if (request) {
      const nativeKey = JSON.stringify(request)
      if (!nativeCache.has(nativeKey)) {
        const entry = {pending: true, masks: null, waiting: new Set()}
        nativeCache.set(nativeKey, entry)
        // The bridge only receives numeric geometry, never DOM/content/data.
        Promise.resolve(window.noteford.nativeCorners(request)).then(result => {
          if (result?.renderer === 'macos-continuous') entry.masks = {outer: result.outer, strokes: [result.stroke, null, null, null]}
        }).catch(() => {}).finally(() => {
          entry.pending = false
          for (const node of entry.waiting) if (root.contains(node)) queue(node)
          entry.waiting.clear()
          if (nativeCache.size > LIMIT) nativeCache.delete(nativeCache.keys().next().value)
        })
      }
      const native = nativeCache.get(nativeKey)
      if (native.pending) { native.waiting.add(element); clear(element); return }
      if (native.masks) { masks = native.masks; renderer = 'macos-continuous' }
    }
    masks ||= previous?.key === key ? previous.masks : masksFor(g, scale, widths)
    if (previous?.key !== key || !element.hasAttribute('data-apple-corners')) {
      element.style.setProperty('--apple-corner-paint', url(surfaceSvg(g, masks, style)))
      element.style.setProperty('--apple-corner-mask', `url("${masks.outer}")`)
      element.dataset.appleCorners = renderer
      element.removeAttribute('data-apple-corner-fallback')
    }
    // Existing overflow still controls scrolling/descendants. Never mask open
    // popovers, message tails, native focus rings or the outer shadow.
    if (clips && !hasShadow) element.dataset.appleClip = 'true'
    if (hasShadow) element.dataset.appleShadow = 'native'
    else element.removeAttribute('data-apple-shadow')
    entries.set(element, { key, masks, ownStyle: element.getAttribute('style') })
    ownStyles.set(element, element.getAttribute('style'))
  }

  function flush() {
    raf = 0
    for (const element of animated.keys()) if (root.contains(element)) dirty.add(element); else animated.delete(element)
    const batch = [...dirty]; dirty.clear()
    for (const element of batch) {
      try { paint(element) } catch { clear(element); element.dataset.appleCornerFallback = 'native' }
    }
    if (animated.size && !raf) raf = requestAnimationFrame(flush)
  }
  const resize = new ResizeObserver(records => records.forEach(record => queue(record.target)))
  const observer = new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'childList') {
        for (const child of record.addedNodes) if (child instanceof Element) scan(child)
        for (const child of record.removedNodes) if (child instanceof Element) {
          for (const element of [...candidates]) if (child === element || child.contains(element)) { resize.unobserve(element); entries.delete(element); candidates.delete(element) }
        }
      } else if (record.attributeName !== 'style' || ownStyles.get(record.target) !== record.target.getAttribute('style')) {
        scan(record.target)
      }
    }
  })
  observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'open', 'aria-pressed', 'aria-current', 'aria-checked', 'aria-expanded', 'aria-selected', 'data-state', 'checked', 'disabled'] })
  const stateChange = event => { ancestorQueue(event.target); if (event.type === 'change') scan(event.target.parentElement) }
  const transition = event => {
    if (!/background|border|shadow/.test(event.propertyName)) return
    const properties = animated.get(event.target) || new Set()
    if (event.type === 'transitionrun') { properties.add(event.propertyName); animated.set(event.target, properties) }
    else { properties.delete(event.propertyName); if (!properties.size) animated.delete(event.target) }
    ancestorQueue(event.target)
  }
  const events = ['pointerover', 'pointerout', 'focusin', 'focusout', 'change', 'input', 'toggle']
  for (const name of events) root.addEventListener(name, stateChange, true)
  // Keep native hover animation; only the tiny SVG color wrapper changes,
  // never regenerate/encode the geometry PNG on each animation frame.
  for (const name of ['transitionrun', 'transitionend', 'transitioncancel']) root.addEventListener(name, transition, true)
  const repaint = () => { scale = window.devicePixelRatio || 1; scan(root) }
  window.addEventListener('resize', repaint)
  media.addEventListener('change', repaint)
  const theme = new MutationObserver(repaint)
  theme.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style', 'data-theme', 'data-platform'] })
  let resolution
  const resolutionChanged = () => {
    resolution?.removeEventListener('change', resolutionChanged)
    repaint()
    resolution = matchMedia(`(resolution: ${scale}dppx)`)
    resolution.addEventListener('change', resolutionChanged)
  }
  resolutionChanged()
  document.fonts?.ready.then(() => { if (!stopped) repaint() })
  scan(root)
  return () => {
    stopped = true; cancelAnimationFrame(raf); observer.disconnect(); resize.disconnect(); theme.disconnect()
    for (const name of events) root.removeEventListener(name, stateChange, true)
    for (const name of ['transitionrun', 'transitionend', 'transitioncancel']) root.removeEventListener(name, transition, true)
    window.removeEventListener('resize', repaint); media.removeEventListener('change', repaint)
    resolution?.removeEventListener('change', resolutionChanged)
    for (const element of candidates) clear(element)
    entries.clear(); candidates.clear(); dirty.clear(); animated.clear(); cache.clear(); nativeCache.clear()
  }
}
