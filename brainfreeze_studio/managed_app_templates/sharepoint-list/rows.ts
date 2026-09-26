export type Row = Record<string, unknown>

// Automatic discovery hides SharePoint's system columns and connector bookkeeping; explicit columns need not.
const SYSTEM = new Set(['ID', 'Id', 'ItemInternalId', 'Author', 'Editor', 'Attachments', 'GUID', 'ContentType',
  'ComplianceAssetId', 'FileSystemObjectType', 'ServerRedirectedEmbedUri', 'ServerRedirectedEmbedUrl', 'Modified',
  'Created'])
const hidden = (key: string) => SYSTEM.has(key) || /^[{@]|#|^OData_/.test(key)
const rawColumns = (rows: Row[]) => [...new Set(rows.flatMap((row) => Object.keys(row)))]

export function discoveredColumns(rows: Row[]): string[] {
  const keys = rawColumns(rows).filter((key) => !hidden(key))
  return keys.includes('Title') ? ['Title', ...keys.filter((key) => key !== 'Title')] : keys
}

export function columnsOf(rows: Row[], requested: string[] = []): string[] {
  if (requested.length) {
    const keys = new Set(rawColumns(rows))
    return [...new Set(requested)].filter((key) => keys.has(key))
  }
  return discoveredColumns(rows).slice(0, 7)
}

export function detailColumns(rows: Row[], requested: string[] = []): string[] {
  return [...new Set([...columnsOf(rows, requested), ...discoveredColumns(rows), 'Modified', 'Created'])]
}

// Midnight UTC represents a date-only column, so it must not become the preceding date west of Greenwich.
export function when(value: string): string {
  const date = new Date(value.replace(/\.(\d{3})\d+(?=Z|[+-]\d\d:?\d\d|$)/i, '.$1'))
  if (Number.isNaN(date.getTime())) return value
  return /T00:00:00(\.0+)?Z$/.test(value)
    ? date.toLocaleDateString(undefined, { timeZone: 'UTC' }) : date.toLocaleString()
}

// Choice, lookup and person columns are objects; multi-value columns are arrays of the same shapes.
export function shown(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(value) ? when(value) : value
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'number') return String(value)
  if (Array.isArray(value)) return value.map(shown).filter(Boolean).join(', ')
  if (typeof value === 'object') {
    const object = value as Row
    for (const key of ['Value', 'DisplayName', 'Title', 'Name', 'Email']) {
      if (typeof object[key] === 'string' || typeof object[key] === 'number') return String(object[key])
      if (typeof object[key] === 'boolean') return shown(object[key])
    }
  }
  return ''
}

export const label = (key: string) => key.replace(/_x0020_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2')

export function matchingRows(rows: Row[], term: string, requested: string[] = []): Row[] {
  const query = term.trim().toLowerCase()
  const keys = [...new Set([...discoveredColumns(rows), ...columnsOf(rows, requested)])]
  return query ? rows.filter((row) => keys.some((key) => shown(row[key]).toLowerCase().includes(query))) : rows
}

export function groupsOf(rows: Row[], column: string): Map<string, number> {
  const groups = new Map<string, number>()
  if (column) for (const row of rows) {
    const group = shown(row[column]) || '(blank)'
    groups.set(group, (groups.get(group) ?? 0) + 1)
  }
  return groups
}
