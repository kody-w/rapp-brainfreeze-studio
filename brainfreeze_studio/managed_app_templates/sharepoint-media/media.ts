// Media files by name, for a folder that mixes them: the type each extension plays as.
export type MediaKind = 'video' | 'image' | 'audio'

export function extensionOf(name: string): string {
  const m = /\.([A-Za-z0-9]+)$/.exec(name)
  return m ? m[1].toLowerCase() : ''
}

export function typeOf(name: string, mime: Record<string, string>): string {
  return mime[extensionOf(name)] ?? ''
}

export function kindOf(name: string, mime: Record<string, string>): MediaKind | null {
  const kind = typeOf(name, mime).split('/')[0]
  return kind === 'video' || kind === 'image' || kind === 'audio' ? kind : null
}
