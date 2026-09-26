export type CalendarEvent = { Id?: string; Subject?: string; Start?: string; End?: string; Location?: string;
  Organizer?: string; IsAllDay?: boolean; ShowAs?: string; TimeZone?: string }
export type DateRange = { start: Date; end: Date }
export type EventSpan = { id: string; event: CalendarEvent; start: Date; end: Date }
export type CalendarDay = { key: string; day: Date; list: EventSpan[]; hours: number }

// The connector returns UTC instants, sometimes without a zone, with up to seven fractional digits.
export function utc(value?: string): Date | null {
  if (!value) return null
  let text = value.replace(/\.(\d+)(?=Z|[+-]\d\d:?\d\d|$)/i,
    (_, fraction: string) => `.${fraction.slice(0, 3).padEnd(3, '0')}`)
  if (/^\d{4}-\d\d-\d\d$/.test(text)) text += 'T00:00:00'
  if (!/(Z|[+-]\d\d:?\d\d)$/i.test(text)) text += 'Z'
  const date = new Date(text)
  return Number.isNaN(date.getTime()) ? null : date
}

export function localDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}

export function addDays(date: Date, days: number): Date {
  const next = new Date(date)
  next.setDate(next.getDate() + days)
  return next
}

export function dayKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

export function rangeFor(days: number, now = new Date()): DateRange {
  const start = localDay(now)
  return { start, end: addDays(start, days) }
}

export function eventId(event: CalendarEvent): string {
  return event.Id || JSON.stringify([event.Subject ?? '', event.Start ?? ''])
}

export function spanOf(event: CalendarEvent): EventSpan | null {
  const start = utc(event.Start)
  const end = utc(event.End)
  if (!start || (event.End && !end) || (end && end < start)) return null
  if (!event.IsAllDay) return { id: eventId(event), event, start, end: end ?? start }

  // Midnight-UTC all-day values are floating dates, not instants in the viewer's zone. The end date is exclusive.
  const floating = start.getUTCHours() === 0 && start.getUTCMinutes() === 0 &&
    start.getUTCSeconds() === 0 && start.getUTCMilliseconds() === 0
  const dateOf = (date: Date) => floating
    ? new Date(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()) : localDay(date)
  const first = dateOf(start)
  const last = end ? dateOf(end) : addDays(first, 1)
  return last > first ? { id: eventId(event), event, start: first, end: last } : null
}

export function hoursIn(span: EventSpan, range: DateRange): number {
  if (span.event.IsAllDay) return 0
  return Math.max(0, Math.min(span.end.getTime(), range.end.getTime()) -
    Math.max(span.start.getTime(), range.start.getTime())) / 3.6e6
}

export function calendarView(events: CalendarEvent[], range: DateRange, now = new Date()) {
  const distinct = new Map<string, EventSpan>()
  for (const event of events) {
    const span = spanOf(event)
    if (!span || range.end <= range.start) continue
    const point = span.start.getTime() === span.end.getTime()
    const overlaps = span.start < range.end && (point ? span.start >= range.start : span.end > range.start)
    if (overlaps && !distinct.has(span.id)) distinct.set(span.id, span)
  }
  const list = [...distinct.values()].sort((a, b) => a.start.getTime() - b.start.getTime())
  const byDay = new Map<string, CalendarDay>()
  for (const span of list) {
    const first = localDay(new Date(Math.max(span.start.getTime(), range.start.getTime())))
    const lastInstant = span.end > span.start ? span.end.getTime() - 1 : span.start.getTime()
    const last = localDay(new Date(Math.min(lastInstant, range.end.getTime() - 1)))
    for (let day = first; day <= last; day = addDays(day, 1)) {
      const key = dayKey(day)
      if (!byDay.has(key)) byDay.set(key, { key, day, list: [], hours: 0 })
      const bucket = byDay.get(key)!
      bucket.list.push(span)
      bucket.hours += hoursIn(span, {
        start: new Date(Math.max(day.getTime(), range.start.getTime())),
        end: new Date(Math.min(addDays(day, 1).getTime(), range.end.getTime())),
      })
    }
  }
  const days = [...byDay.values()].sort((a, b) => a.day.getTime() - b.day.getTime())
  const busiest = days.reduce<CalendarDay | null>((best, day) => day.hours > (best?.hours ?? 0) ? day : best, null)
  const next = list.find((span) => !span.event.IsAllDay && span.start > now) ?? null
  return { events: list, days, hours: list.reduce((sum, span) => sum + hoursIn(span, range), 0), busiest, next }
}

export const clock = (date: Date) => date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
export const dayName = (date: Date) => date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })

export function eventTime(span: EventSpan): string {
  if (span.event.IsAllDay) return 'All day'
  if (dayKey(span.start) === dayKey(span.end)) return `${clock(span.start)} – ${clock(span.end)}`
  const options: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
  if (span.start.getFullYear() !== span.end.getFullYear()) options.year = 'numeric'
  // two lines, each end on its own: the time column is narrow, and a wrap inside a date reads badly
  return `${span.start.toLocaleString(undefined, options)} –\n${span.end.toLocaleString(undefined, options)}`
}
