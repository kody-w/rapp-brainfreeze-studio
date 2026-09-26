// Connector results in the App Player, per the managed-apps plugin's shared rules ("Connector Response Handling"):
// every generated service returns { success, data, error }; lists arrive as an array or in data.value; a failure
// throws so it is seen, and an empty list is a valid, empty state. Binary content (files, photos) arrives as a
// Uint8Array, and the deployed player's content security policy allows media and images only from 'self' and data:
// URLs, so binary content is shown as data: URLs (blob: URLs are blocked).
export type Result<T> = { success: boolean; data?: T; error?: { message?: string } }

export function need<T>(result: Result<T>, what: string): T {
  if (!result.success) throw new Error(result.error?.message ?? `${what} failed`)
  return result.data as T
}

export function listOf<T>(data: unknown): T[] {
  if (Array.isArray(data)) return data as T[]
  const value = (data as { value?: unknown } | null | undefined)?.value
  return Array.isArray(value) ? (value as T[]) : []
}

export function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

// A request owns its result only until the next request starts or its component is cleaned up.
export function latestRequest() {
  let sequence = 0
  return {
    start() {
      const request = ++sequence
      return () => request === sequence
    },
    cancel() {
      sequence++
    },
  }
}

export function asBytes(data: unknown): Uint8Array {
  if (data instanceof Uint8Array) return data
  if (data instanceof ArrayBuffer) return new Uint8Array(data)
  if (typeof data === 'string') {
    if (/^https?:/i.test(data)) throw new Error('Expected content, got a URL')
    const binary = atob(data.replace(/^data:[^,]*,/, ''))
    const out = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i)
    return out
  }
  throw new Error(`Unexpected content: ${Object.prototype.toString.call(data)}`)
}

export function dataUrl(bytes: Uint8Array, type: string): string {
  let binary = ''
  const step = 0x8000   // in chunks: spreading millions of bytes into one call overflows the stack
  for (let i = 0; i < bytes.length; i += step) binary += String.fromCharCode(...bytes.subarray(i, i + step))
  return `data:${type};base64,${btoa(binary)}`
}

// A photo or other image response as a data: URL (the add-office365-users skill's toImageDataUrl): data: URLs pass
// through, and an http(s) value is refused rather than handed to <img src>, where the policy would block it.
export function imageUrl(value: unknown, type = 'image/jpeg'): string {
  if (typeof value === 'string' && /^data:image\/[a-z0-9.+-]+;base64,/i.test(value)) return value
  return dataUrl(asBytes(value), type)
}

export function formatBytes(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} KB`
  return `${n} B`
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  const first = parts[0]?.[0] ?? ''
  const last = parts.length > 1 ? parts[parts.length - 1][0] : ''
  return (first + last).toUpperCase() || '?'
}

// What the app is doing, for people (the status line) and for automation (data-state on it): loading, ready, empty
// or error. A test or an agent waits for ready or empty and treats error as a failure, without reading pixels.
export type Phase = 'loading' | 'ready' | 'empty' | 'error'
