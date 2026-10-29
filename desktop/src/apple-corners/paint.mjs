import { makeField } from './g3-corners.mjs'

const clamp = n => Math.max(0, Math.min(1, n))

// CALayer's off-screen renderer supports one radius and a uniform stroke.
// renderInContext ignores maskedCorners on the tested macOS SDK; never round
// authored sharp corners. Those and elliptical/asymmetric shapes use G3.
export function nativeRequest(g, scale, widths, colors) {
  const positive = g.usedRadii.filter(([x, y]) => x > 0 && y > 0)
  const radius = positive[0]?.[0]
  if (positive.length !== 4 || radius === undefined || positive.some(([x, y]) => x !== radius || y !== radius)
    || widths.some(value => value !== widths[0])
    || (widths[0] > 0 && colors.some(value => value !== colors[0]))) return null
  return {width: g.width, height: g.height, radius, scale, stroke: widths[0],
    corners: 15}
}

// Sample the canonical degree-7/8 contour, including its border, at device scale.
// No cubic/path approximation; straight interiors avoid expensive curve evaluation.
export function contourPixels(g, scale, widths = [0, 0, 0, 0]) {
  const width = Math.ceil(g.width * scale), height = Math.ceil(g.height * scale)
  if (width * height > 16_000_000) throw new Error('Corner surface too large')
  const outer = new Uint8ClampedArray(width * height * 4)
  new Uint32Array(outer.buffer).fill(0xffffffff)
  const strokes = widths.map(value => value > 0 ? new Uint8ClampedArray(outer.length) : null)
  const field = makeField(g)
  const [tl, tr, br, bl] = g.cornerExtents
  const bandX = Math.max(tl[0], tr[0], br[0], bl[0], ...widths) + 2 / scale
  const bandY = Math.max(tl[1], tr[1], br[1], bl[1], ...widths) + 2 / scale
  for (let y = 0; y < height; y++) {
    const cy = (y + .5) / scale
    for (let x = 0; x < width; x++) {
      const cx = (x + .5) / scale, right = g.width - cx, bottom = g.height - cy
      if (g.kind === 'corners' && cy > bandY && bottom > bandY && cx > bandX && right > bandX) {
        x = Math.max(x, Math.floor((g.width - bandX) * scale) - 1)
        continue
      }
      const nearCorner = (cx < tl[0] && cy < tl[1]) || (right < tr[0] && cy < tr[1]) || (right < br[0] && bottom < br[1]) || (cx < bl[0] && bottom < bl[1])
      const straight = Math.min(cy, right, bottom, cx)
      const d = g.kind === 'corners' && !nearCorner ? straight : field(cx, cy)
      const alpha = clamp(.5 + scale * d), i = (y * width + x) * 4
      outer[i] = outer[i + 1] = outer[i + 2] = 255
      outer[i + 3] = Math.round(255 * alpha)
      // CSS unequal side widths/colors meet at a corner's diagonal.
      const side = straight === cy ? 0 : straight === right ? 1 : straight === bottom ? 2 : 3
      if (strokes[side]) {
        const data = strokes[side]
        data[i] = data[i + 1] = data[i + 2] = 255
        data[i + 3] = Math.round(255 * Math.max(0, alpha - clamp(.5 + scale * (d - widths[side]))))
      }
    }
  }
  return { width, height, outer, strokes }
}

export function splitLayers(value) {
  const result = []; let depth = 0, start = 0
  for (let i = 0; i < value.length; i++) {
    if (value[i] === '(') depth++
    if (value[i] === ')') depth--
    if (value[i] === ',' && depth === 0) { result.push(value.slice(start, i).trim()); start = i + 1 }
  }
  result.push(value.slice(start).trim())
  return result
}

const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;')

function gradient(value, width, height, id) {
  if (!value.startsWith('linear-gradient(')) throw new Error('Unsupported background image')
  const stops = splitLayers(value.slice(16, -1))
  let degrees = 180
  if (/^-?[\d.]+deg$/.test(stops[0])) degrees = parseFloat(stops.shift())
  const angle = degrees * Math.PI / 180, dx = Math.sin(angle), dy = -Math.cos(angle)
  const length = Math.abs(width * dx) + Math.abs(height * dy)
  const colors = stops.map((stop, index) => {
    const match = stop.match(/^(.*?)(?:\s+([\d.]+)(%|px))?$/)
    const offset = match[2] ? Number(match[2]) / (match[3] === '%' ? 100 : length) : index / (stops.length - 1)
    return `<stop offset="${offset}" stop-color="${escape(match[1])}"/>`
  }).join('')
  return `<linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="${width / 2 - dx * length / 2}" y1="${height / 2 - dy * length / 2}" x2="${width / 2 + dx * length / 2}" y2="${height / 2 + dy * length / 2}">${colors}</linearGradient>`
}

// SVG only composites the canonical raster masks; it does not replace G3 geometry.
export function surfaceSvg(g, masks, style) {
  const rect = fill => `<rect width="${g.width}" height="${g.height}" fill="${escape(fill)}"/>`
  const mask = (id, url) => `<mask id="${id}" maskUnits="userSpaceOnUse" x="0" y="0" width="${g.width}" height="${g.height}" style="mask-type:alpha"><image href="${url}" width="${g.width}" height="${g.height}" preserveAspectRatio="none"/></mask>`
  const layers = style.image === 'none' ? [] : splitLayers(style.image)
  const definitions = layers.map((value, i) => gradient(value, g.width, g.height, 'bg' + i))
  const backgrounds = [rect(style.color), ...layers.map((_, i) => rect(`url(#bg${i})`)).reverse()]
  const strokes = masks.strokes.map((url, i) => url ? `<g mask="url(#edge${i})">${rect(style.borders[i])}</g>` : '').join('')
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${g.width}" height="${g.height}" viewBox="0 0 ${g.width} ${g.height}"><defs>${mask('shape', masks.outer)}${masks.strokes.map((url, i) => url ? mask('edge' + i, url) : '').join('')}${definitions.join('')}</defs><g mask="url(#shape)">${backgrounds.join('')}</g>${strokes}</svg>`
}
