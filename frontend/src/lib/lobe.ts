/**
 * Lobe icon loader with dual-channel CDN fallback.
 *
 * Order: configured local base -> npmmirror (fast in CN) -> jsDelivr -> unpkg.
 * Responses are cached in memory and in localStorage so repeat renders never
 * hit the network, and the GUI degrades to a text glyph if every channel fails.
 */

const VERSION = '1.102.0'
const STORAGE_PREFIX = 'onion.icon.'

const CHANNELS = [
  `https://registry.npmmirror.com/@lobehub/icons-static-svg/${VERSION}/files/icons`,
  `https://cdn.jsdelivr.net/npm/@lobehub/icons-static-svg@${VERSION}/icons`,
  `https://unpkg.com/@lobehub/icons-static-svg@${VERSION}/icons`,
]

const memory = new Map<string, string>()
const pending = new Map<string, Promise<string | null>>()
let activeChannel: number | null = null

const ALLOWED = /^(deepseek|openai|anthropic|claude|moonshot|zhipu|qwen|siliconcloud|openrouter|gemini|ollama|grok|mistral|meta|huggingface)$/i

function readCache(name: string): string | null {
  if (memory.has(name)) return memory.get(name)!
  try {
    const stored = localStorage.getItem(STORAGE_PREFIX + name)
    if (stored) {
      memory.set(name, stored)
      return stored
    }
  } catch {
    /* storage unavailable */
  }
  return null
}

function writeCache(name: string, svg: string) {
  memory.set(name, svg)
  try {
    localStorage.setItem(STORAGE_PREFIX + name, svg)
  } catch {
    /* quota or disabled */
  }
}

async function fetchChannel(index: number, name: string): Promise<string | null> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 4500)
  try {
    const response = await fetch(`${CHANNELS[index]}/${name}.svg`, {
      signal: controller.signal,
      cache: 'force-cache',
    })
    if (!response.ok) return null
    const text = await response.text()
    if (!text.includes('<svg')) return null
    return text
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

/** Fetch a mono Lobe icon by name; resolves to raw SVG markup or null. */
export function loadLobeIcon(name: string): Promise<string | null> {
  const key = name.toLowerCase()
  if (!ALLOWED.test(key)) return Promise.resolve(null)
  const cached = readCache(key)
  if (cached) return Promise.resolve(cached)
  if (pending.has(key)) return pending.get(key)!

  const promise = (async () => {
    const order =
      activeChannel === null
        ? [0, 1, 2]
        : [activeChannel, ...CHANNELS.map((_, i) => i).filter((i) => i !== activeChannel)]
    for (const index of order) {
      const svg = await fetchChannel(index, key)
      if (svg) {
        activeChannel = index
        writeCache(key, svg)
        return svg
      }
    }
    return null
  })()

  pending.set(key, promise)
  promise.finally(() => pending.delete(key))
  return promise
}