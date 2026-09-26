import { useEffect, useState } from 'react'
import { OutlookService } from './bound'
import { CONFIG } from './config'
import { listOf, message, need, type Phase } from './connector'
import { calendarView, clock, dayName, eventTime, rangeFor, type CalendarEvent, type DateRange } from './calendar'
import './App.css'

// The Office 365 Outlook connector the way the add-office365 skill uses it: discover the calendar with
// CalendarGetTables (never assume its id), then read a date range with GetEventsCalendarViewV2.
type Calendar = { Name?: string; DisplayName?: string }

async function findCalendar(): Promise<{ id: string; name: string }> {
  const calendars = listOf<Calendar>(need(await OutlookService.CalendarGetTables(), 'Listing your calendars'))
  if (calendars.length === 0) throw new Error('No calendars in this Office 365 connection')
  const named = (n: string) => calendars.find((c) => (c.DisplayName ?? '').toLowerCase() === n.toLowerCase())
  const pick = named(CONFIG.calendar) ?? named('calendar') ?? calendars[0]
  if (!pick.Name) throw new Error('The calendar list did not include calendar ids')
  return { id: pick.Name, name: pick.DisplayName || pick.Name }
}

async function eventsIn(calendarId: string, range: DateRange): Promise<CalendarEvent[]> {
  const got = await OutlookService.GetEventsCalendarViewV2(calendarId, range.start.toISOString(), range.end.toISOString(),
    undefined, undefined, 500, 0)
  return listOf<CalendarEvent>(need(got, 'Reading your calendar'))
}

export default function App() {
  const ranges = [...new Set([1, 7, CONFIG.days])].sort((a, b) => a - b)
  const [days, setDays] = useState<number>(CONFIG.days)
  const [range, setRange] = useState(() => rangeFor(CONFIG.days))
  const [calendar, setCalendar] = useState<{ id: string; name: string } | null>(null)
  const [events, setEvents] = useState<CalendarEvent[]>([])
  const [status, setStatus] = useState('Finding your calendar…')
  const [phase, setPhase] = useState<Phase>('loading')

  useEffect(() => {
    let current = true
    const selectedRange = rangeFor(days)
    setRange(selectedRange)
    setEvents([])
    setPhase('loading')
    setStatus(calendar ? `Reading ${calendar.name}…` : 'Finding your calendar…')
    ;(async () => {
      const cal = calendar ?? (await findCalendar())
      if (!current) return
      if (!calendar) setCalendar(cal)
      setStatus(`Reading ${cal.name}…`)
      const found = await eventsIn(cal.id, selectedRange)
      if (!current) return
      const count = calendarView(found, selectedRange).events.length
      setEvents(found)
      setPhase(count ? 'ready' : 'empty')
      setStatus(count ? `${count} event${count === 1 ? '' : 's'} in ${cal.name}`
        : `Nothing in ${cal.name} for the ${days === 1 ? 'day' : `next ${days} days`}`)
    })().catch((e: unknown) => {
      if (!current) return
      setPhase('error')
      setStatus(message(e))
    })
    return () => { current = false }
    // the calendar is found once; changing the range re-reads events only
  }, [days])

  const view = calendarView(events, range)
  const { busiest, next } = view

  return (
    <main className="app">
      <div className="bar">
        <div>
          <h1>{CONFIG.title}</h1>
          <p className="where">Office 365 Outlook · {calendar?.name ?? 'your calendar'}</p>
        </div>
      </div>
      <div className="tabs" role="group" aria-label="Range">
        {ranges.map((n) => (
          <button key={n} type="button" aria-pressed={days === n} data-testid={`range-${n}`} onClick={() => setDays(n)}>
            {n === 1 ? 'Today' : `Next ${n} days`}
          </button>
        ))}
      </div>
      <p className="status" data-testid="status" data-state={phase} role="status">{status}</p>
      {phase !== 'error' && (
        <div className="stats">
          <div className="stat"><b data-testid="event-count">{view.events.length}</b><span>events</span></div>
          <div className="stat"><b data-testid="meeting-hours">{view.hours.toFixed(1)}</b><span>hours in meetings</span></div>
          <div className="stat"><b data-testid="busiest-day" data-day={busiest?.key ?? ''}>
            {busiest ? busiest.day.toLocaleDateString(undefined, { weekday: 'short' }) : '–'}</b>
            <span>busiest day</span></div>
          <div className="stat"><b data-testid="next-event" data-event-id={next?.id ?? ''}>{next ? clock(next.start) : '–'}</b>
            <span>{next ? `next: ${next.event.Subject || '(no subject)'}` : 'nothing else coming up'}</span></div>
        </div>
      )}
      {phase === 'empty' && <p className="empty">A clear calendar.</p>}
      <div data-testid="events" data-count={view.events.length}>
        {view.days.map(({ day, key, list }) => (
          <section className="day" key={key} data-testid="day" data-day={key}>
            <h3>{dayName(day)}</h3>
            {list.map((span) => (
              <article className={span.event.IsAllDay ? 'event all-day' : 'event'} key={span.id}
                       data-testid="event" data-event-id={span.id} data-day={key}>
                <div className="time" style={{ whiteSpace: 'pre-line' }}>{eventTime(span)}</div>
                <div>
                  <b>{span.event.Subject || '(no subject)'}</b>
                  {(span.event.Location || span.event.Organizer) && (
                    <span>{[span.event.Location, span.event.Organizer && `organized by ${span.event.Organizer}`]
                      .filter(Boolean).join(' · ')}</span>
                  )}
                </div>
              </article>
            ))}
          </section>
        ))}
      </div>
    </main>
  )
}
