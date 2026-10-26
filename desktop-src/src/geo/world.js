// Country lookup over the baked raster: index <-> ISO alpha-2, names, label points, and
// lat/lon -> country. Decoding is lazy so importing this module costs nothing until a globe mounts.
// Two resolutions are baked in (4096x2048 and 8192x4096); the user can switch at runtime.
import {
  COUNTRY_ROWS,
  LAND_UNASSIGNED,
  RASTER_4096x2048_RLE,
  RASTER_4096x2048_W,
  RASTER_4096x2048_H,
  RASTER_8192x4096_RLE,
  RASTER_8192x4096_W,
  RASTER_8192x4096_H
} from './world.generated.js'

export { LAND_UNASSIGNED }

export const COUNTRIES = COUNTRY_ROWS.map(([code, name, lat, lon, area], i) => ({
  index: i + 1,
  code,
  name,
  lat,
  lon,
  area
}))

const byCode = new Map(COUNTRIES.map(c => [c.code, c]))

let displayNames = null
try {
  displayNames = new Intl.DisplayNames(['en'], { type: 'region', fallback: 'none' })
} catch {
  displayNames = null
}

/** Country record for an alpha-2 code, or undefined (codes outside Natural Earth, e.g. tiny states). */
export function countryByCode(code) {
  return code ? byCode.get(code) : undefined
}

export function countryByIndex(index) {
  return index > 0 && index <= COUNTRIES.length ? COUNTRIES[index - 1] : undefined
}

/** English short name for an alpha-2 code, falling back to Natural Earth's name, then the code. */
export function countryName(code) {
  if (!code) return 'Unknown'
  let name = null
  try {
    name = displayNames?.of(code) ?? null
  } catch {
    name = null
  }
  return name || byCode.get(code)?.name || code
}

const RASTERS = {
  '4096x2048': { W: RASTER_4096x2048_W, H: RASTER_4096x2048_H, RLE: RASTER_4096x2048_RLE },
  '8192x4096': { W: RASTER_8192x4096_W, H: RASTER_8192x4096_H, RLE: RASTER_8192x4096_RLE }
}

/** Active resolution, e.g. '4096x2048' or '8192x4096'. */
let active = '4096x2048'

/** The active resolution string. */
export function resolution() {
  return active
}

/** Switch the active raster resolution. Clears the cached decode. */
export function setResolution(size) {
  if (!(size in RASTERS)) throw new Error(`unknown resolution: ${size}`)
  active = size
  raster = null
  width = null
  height = null
}

let raster = null
let width = null
let height = null

/** The decoded index raster for the active resolution (row 0 = 90°N, column 0 = 180°W). */
export function countryRaster() {
  if (raster) return raster
  const { W, H, RLE } = RASTERS[active]
  width = W
  height = H
  const bytes = decodeBase64(RLE)
  const out = new Uint8Array(W * H)
  let o = 0
  let i = 0
  while (i < bytes.length && o < out.length) {
    const value = bytes[i++]
    let run = 0
    let shift = 0
    let b
    do {
      b = bytes[i++]
      run |= (b & 0x7f) << shift
      shift += 7
    } while (b & 0x80)
    out.fill(value, o, o + run)
    o += run
  }
  raster = out
  return raster
}

/** Raster width of the active resolution. */
export function rasterWidth() {
  if (width === null) countryRaster()
  return width
}

/** Raster height of the active resolution. */
export function rasterHeight() {
  if (height === null) countryRaster()
  return height
}

function decodeBase64(text) {
  if (typeof atob === 'function') {
    const binary = atob(text)
    const out = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i)
    return out
  }
  return new Uint8Array(Buffer.from(text, 'base64'))
}

/** Raster index under a lat/lon (0 = ocean, LAND_UNASSIGNED = land with no country). */
export function indexAt(lat, lon) {
  const r = countryRaster()
  const w = rasterWidth()
  const h = rasterHeight()
  const x = Math.min(w - 1, Math.max(0, Math.floor(((lon + 180) / 360) * w)))
  const y = Math.min(h - 1, Math.max(0, Math.floor(((90 - lat) / 180) * h)))
  return r[y * w + x]
}

export function countryAt(lat, lon) {
  return countryByIndex(indexAt(lat, lon))
}
