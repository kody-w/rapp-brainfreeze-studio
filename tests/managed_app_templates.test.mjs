import test from 'node:test'
import assert from 'node:assert/strict'
import {
  addDays, calendarView, clock, dayKey, eventId, eventTime, rangeFor, spanOf, utc,
} from '../brainfreeze_studio/managed_app_templates/calendar-dashboard/calendar.ts'
import {
  columnsOf, detailColumns, discoveredColumns, groupsOf, label, matchingRows, shown,
} from '../brainfreeze_studio/managed_app_templates/sharepoint-list/rows.ts'
import {
  asBytes, dataUrl, imageUrl, latestRequest, listOf, need,
} from '../brainfreeze_studio/managed_app_templates/common/connector.ts'
import { extensionOf, kindOf, typeOf } from '../brainfreeze_studio/managed_app_templates/sharepoint-media/media.ts'

if (!process.env.TZ) process.env.TZ = 'UTC'
const zone = process.env.TZ
const local = (day, hour = 0, minute = 0) => new Date(2026, 8, day, hour, minute)
const timedEvent = (id, start, end) => ({ Id: id, Subject: id, Start: start.toISOString(), End: end.toISOString() })
const keys = (view) => view.days.map((day) => day.key)
const week = () => rangeFor(9, local(26, 12))
const timed = {
  Id: 'sync', Subject: 'Team sync', Start: '2026-09-26T22:00:00.0000000+00:00',
  End: '2026-09-26T22:30:00.0000000+00:00', IsAllDay: false, TimeZone: 'Eastern Standard Time',
}
const overnight = {
  Id: 'overnight', Subject: 'Overnight support', Start: '2026-09-28T02:00:00.0000000+00:00',
  End: '2026-09-28T06:00:00.0000000+00:00',
}
const allDay = {
  Id: 'workshop', Subject: 'Workshop', Start: '2026-09-29T00:00:00.0000000+00:00',
  End: '2026-10-02T00:00:00.0000000+00:00', IsAllDay: true, TimeZone: 'UTC',
}

test('calendar parses UTC instants, missing zones and numeric offsets', () => {
  assert.equal(utc(timed.Start).toISOString(), '2026-09-26T22:00:00.000Z')
  assert.equal(utc('2026-09-26T22:00:00.1234567').toISOString(), '2026-09-26T22:00:00.123Z')
  assert.equal(utc('2026-09-26T18:00:00.1-04:00').toISOString(), '2026-09-26T22:00:00.100Z')
  assert.equal(utc('2026-09-27T07:00:00.12+0900').toISOString(), '2026-09-26T22:00:00.120Z')
  assert.equal(utc('2026-09-26').toISOString(), '2026-09-26T00:00:00.000Z')
  assert.equal(utc(), null)
  assert.equal(utc('not a date'), null)
})

test('calendar normalizes fractions before calling a non-V8 Date parser', () => {
  const NativeDate = globalThis.Date
  try {
    globalThis.Date = class extends NativeDate {
      constructor(...args) {
        if (typeof args[0] === 'string') assert.doesNotMatch(args[0], /\.\d{4,}/)
        super(...args)
      }
    }
    assert.equal(utc(timed.Start).toISOString(), '2026-09-26T22:00:00.000Z')
    assert.equal(utc('2026-09-26T22:00:00.1234567').toISOString(), '2026-09-26T22:00:00.123Z')
  } finally {
    globalThis.Date = NativeDate
  }
})

test('calendar range is local midnight through the exclusive Nth day', () => {
  const now = local(26, 17, 42)
  const before = now.getTime()
  const range = rangeFor(7, now)
  assert.equal(range.start.getTime(), local(26).getTime())
  assert.equal(range.end.getTime(), local(33).getTime())
  assert.equal(dayKey(range.start), '2026-09-26')
  assert.equal(dayKey(range.end), '2026-10-03')
  assert.equal(now.getTime(), before)
  assert.equal(addDays(range.start, 1).getTime(), local(27).getTime())
  assert.equal(range.start.getTime(), local(26).getTime())
})

test('ground-truth timed event uses the viewer day, not the connector TimeZone label', () => {
  const expected = zone === 'Asia/Tokyo' ? '2026-09-27' : '2026-09-26'
  const view = calendarView([timed], week(), local(26))
  assert.deepEqual(keys(view), [expected])
  assert.equal(view.events.length, 1)
  assert.equal(view.days[0].hours, 0.5)
  assert.equal(view.hours, 0.5)
  assert.equal(view.next.id, timed.Id)
})

test('ground-truth overnight event expands onto every covered local day', () => {
  const view = calendarView([overnight], week(), local(26))
  assert.deepEqual(keys(view), zone === 'America/New_York' ? ['2026-09-27', '2026-09-28'] : ['2026-09-28'])
  assert.deepEqual(view.days.map((day) => day.hours), zone === 'America/New_York' ? [2, 2] : [4])
  assert.equal(view.events.length, 1)
  assert.equal(view.hours, 4)
  assert.ok(view.days.every((day) => day.list[0].id === 'overnight'))
})

test('ground-truth all-day event floats across zones and excludes its end date', () => {
  const view = calendarView([allDay], week(), local(26))
  assert.deepEqual(keys(view), ['2026-09-29', '2026-09-30', '2026-10-01'])
  assert.equal(view.events.length, 1)
  assert.equal(view.hours, 0)
  assert.equal(view.busiest, null)
  assert.equal(view.next, null)
  assert.ok(view.days.every((day) => day.hours === 0 && day.list[0].event.IsAllDay))
  assert.equal(eventTime(view.events[0]), 'All day')
})

test('all-day non-midnight UTC starts fall back to the local date of the instant', () => {
  const event = {
    ...allDay, Start: '2026-09-29T02:00:00.0000000+00:00', End: '2026-10-01T02:00:00.0000000+00:00',
  }
  assert.deepEqual(keys(calendarView([event], week())), zone === 'America/New_York'
    ? ['2026-09-28', '2026-09-29'] : ['2026-09-29', '2026-09-30'])
})

test('continuing and trailing events are clipped without creating out-of-range sections', () => {
  const range = rangeFor(2, local(28, 12))
  const events = [
    timedEvent('continuing', local(27, 22), local(28, 2)),
    timedEvent('trailing', local(29, 23), local(30, 2)),
    { ...allDay, Start: '2026-09-27T00:00:00Z' },
  ]
  const view = calendarView(events, range, local(28))
  assert.deepEqual(keys(view), ['2026-09-28', '2026-09-29'])
  assert.equal(view.events.length, 3)
  assert.deepEqual(view.days.map((day) => day.hours), [2, 1])
  assert.equal(view.hours, 3)
  assert.equal(view.busiest.key, '2026-09-28')
  assert.equal(view.next.id, 'trailing')
})

test('events outside the range or merely touching its exclusive boundaries are excluded', () => {
  const range = rangeFor(2, local(28))
  const events = [
    timedEvent('past', local(26, 10), local(26, 11)),
    timedEvent('ends-at-start', local(27, 23), local(28)),
    timedEvent('starts-at-end', local(30), local(30, 1)),
    { ...allDay, Id: 'all-day-past', Start: '2026-09-26T00:00:00Z', End: '2026-09-28T00:00:00Z' },
    { ...allDay, Id: 'all-day-future', Start: '2026-09-30T00:00:00Z' },
  ]
  const view = calendarView(events, range)
  assert.equal(view.events.length, 0)
  assert.deepEqual(view.days, [])
  assert.equal(view.hours, 0)
})

test('a timed event ending at midnight does not occupy the next day', () => {
  const event = timedEvent('midnight', local(27, 22), local(28))
  const view = calendarView([event], week())
  assert.deepEqual(keys(view), ['2026-09-27'])
  assert.equal(view.hours, 2)
})

test('meeting hours for an event spanning the range count only the selected days', () => {
  const view = calendarView([
    timedEvent('long', local(27, 12), local(31, 12)), allDay,
  ], rangeFor(2, local(28)))
  assert.equal(view.hours, 48)
  assert.deepEqual(view.days.map((day) => day.hours), [24, 24])
  assert.equal(view.days.reduce((sum, day) => sum + day.hours, 0), view.hours)
})

test('event counts and hours deduplicate by Id, otherwise by subject and start', () => {
  const noId = { ...overnight, Id: undefined }
  const events = [timed, { ...timed, Subject: 'Renamed' }, { ...timed, Id: 'another-id' },
    noId, { ...noId }, { ...noId, Subject: 'Another subject' }]
  const snapshot = JSON.stringify(events)
  const view = calendarView(events, week())
  assert.equal(view.events.length, 4)
  assert.equal(view.hours, 9)
  assert.equal(new Set(view.events.map((span) => span.id)).size, 4)
  assert.equal(eventId(noId), eventId({ ...noId }))
  assert.notEqual(eventId(noId), eventId({ ...noId, Start: timed.Start }))
  assert.equal(JSON.stringify(events), snapshot)
})

test('busiest day uses the timed hours on each day, not full durations on the start day', () => {
  const view = calendarView([
    timedEvent('short', local(27, 12), local(27, 15)),
    timedEvent('overnight', local(27, 23), local(28, 5)),
    allDay,
  ], week())
  assert.equal(view.busiest.key, '2026-09-28')
  assert.equal(view.busiest.hours, 5)
  assert.equal(view.days.find((day) => day.key === '2026-09-27').hours, 4)
})

test('next is the earliest timed start strictly after now within the range', () => {
  const view = calendarView([
    timedEvent('later', local(27, 16), local(27, 17)),
    timedEvent('ongoing', local(27, 11), local(27, 13)),
    timedEvent('now', local(27, 12), local(27, 13)),
    timedEvent('soon', local(27, 14), local(27, 15)),
    { ...allDay, Start: '2026-09-27T00:00:00Z' },
  ], week(), local(27, 12))
  assert.equal(view.next.id, 'soon')
  assert.equal(calendarView([allDay], week(), local(27)).next, null)
})

test('single-day labels stay compact and multi-day labels retain their full dated span', () => {
  const single = spanOf(timedEvent('single', local(27, 10), local(27, 11)))
  assert.equal(eventTime(single), `${clock(local(27, 10))} – ${clock(local(27, 11))}`)
  const multi = timedEvent('multi', local(27, 22), local(28, 2))
  const view = calendarView([multi], rangeFor(1, local(28)))
  const options = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
  const expected = `${local(27, 22).toLocaleString(undefined, options)} –\n${local(28, 2).toLocaleString(undefined, options)}`
  assert.equal(eventTime(view.days[0].list[0]), expected)
})

test('local calendar day stepping respects both daylight-saving transitions', () => {
  for (const [month, date, newYorkHours] of [[2, 8, 23], [10, 1, 25]]) {
    const range = rangeFor(1, new Date(2026, month, date, 12))
    assert.equal(range.start.getHours(), 0)
    assert.equal(range.end.getHours(), 0)
    assert.equal(range.end.getDate(), date + 1)
    const expected = zone === 'America/New_York' ? newYorkHours : 24
    assert.equal((range.end - range.start) / 3.6e6, expected)
    const view = calendarView([timedEvent('dst', range.start, range.end)], range)
    assert.equal(view.days.length, 1)
    assert.equal(view.hours, expected)
    assert.equal(view.days[0].hours, expected)
  }
  const start = new Date(2026, 2, 7)
  const end = new Date(2026, 2, 10)
  assert.deepEqual(keys(calendarView([timedEvent('dst-span', start, end)], { start, end })),
    ['2026-03-07', '2026-03-08', '2026-03-09'])
})

test('missing and invalid dates do not fabricate durations or day sections', () => {
  const range = rangeFor(1, local(28))
  const view = calendarView([
    { Id: 'invalid', Start: 'invalid', End: 'invalid' },
    { Id: 'missing-start' },
    { Id: 'invalid-end', Start: local(28, 12).toISOString(), End: 'invalid' },
    timedEvent('backwards', local(28, 13), local(28, 12)),
    { Id: 'point', Start: local(28, 12).toISOString() },
  ], range)
  assert.deepEqual(view.events.map((span) => span.id), ['point'])
  assert.equal(view.hours, 0)
  assert.equal(view.busiest, null)
  assert.deepEqual(keys(view), ['2026-09-28'])
  assert.deepEqual(calendarView([timed], { start: local(30), end: local(28) }).events, [])
})

test('explicit system columns use raw keys from any row and keep the requested order', () => {
  const rows = Object.freeze([Object.freeze({ Title: 'One', ID: 1 }),
    Object.freeze({ Title: 'Two', Created: '2026-09-29T00:00:00Z', Modified: null })])
  assert.deepEqual(columnsOf(rows, ['ID', 'Created', 'Modified']), ['ID', 'Created', 'Modified'])
  assert.deepEqual(columnsOf(rows, ['Modified', 'ID', 'Missing', 'Created', 'ID']), ['Modified', 'ID', 'Created'])
  assert.deepEqual(columnsOf(rows, ['Missing']), [])
  assert.deepEqual(columnsOf([], ['ID']), [])
})

test('automatic columns hide system keys, put Title first and stop at seven', () => {
  const rows = [{ ID: 1, Category: 'A', Created: '', Modified: '', Author: {}, Editor: {}, Attachments: false,
    GUID: '', ItemInternalId: '', '{Identifier}': '', '@odata.etag': '', 'Author#Claims': '', OData__ColorTag: '',
    Title: 'One', Alpha: 1, Beta: 2, Gamma: 3, Delta: 4, Epsilon: 5, Zeta: 6 }, { Later: 7 }]
  assert.deepEqual(columnsOf(rows), ['Title', 'Category', 'Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon'])
  assert.deepEqual(discoveredColumns(rows), ['Title', 'Category', 'Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta', 'Later'])
  assert.deepEqual(columnsOf([{ Category: 'A' }, { Title: 'Later row' }]), ['Title', 'Category'])
  assert.deepEqual(columnsOf([{ ID: 1, Created: '' }]), [])
  assert.deepEqual(columnsOf([]), [])
})

test('explicit columns are not subject to the automatic seven-column limit', () => {
  const row = { Title: 'One', A: 1, B: 2, C: 3, D: 4, E: 5, F: 6, G: 7, H: 8 }
  const requested = Object.keys(row).reverse()
  assert.deepEqual(columnsOf([row], requested), requested)
})

test('row display handles choice, lookup, person, arrays and booleans', () => {
  for (const [value, expected] of [
    [{ Value: 'Choice' }, 'Choice'], [{ Id: 3, Value: 'Lookup' }, 'Lookup'],
    [{ DisplayName: 'Alex Example', Email: 'alex@contoso.example' }, 'Alex Example'],
    [{ Title: 'Title' }, 'Title'], [{ Name: 'Name' }, 'Name'], [{ Email: 'a@contoso.example' }, 'a@contoso.example'],
    [{ Value: 0 }, '0'], [{ Value: false }, 'No'], [true, 'Yes'], [false, 'No'], [0, '0'],
    [[{ Value: 'Red' }, null, { Value: 'Blue' }, ['Nested', false], 0], 'Red, Blue, Nested, No, 0'],
    [null, ''], [undefined, ''], [{ Id: 3 }, ''], [[], ''],
  ]) assert.equal(shown(value), expected)
})

test('date-only values keep their UTC date while other ISO datetimes keep local time', () => {
  const date = new Date('2026-09-29T00:00:00Z')
  const expected = date.toLocaleDateString(undefined, { timeZone: 'UTC' })
  for (const value of ['2026-09-29T00:00:00Z', '2026-09-29T00:00:00.000Z', '2026-09-29T00:00:00.0000000Z']) {
    assert.equal(shown(value), expected)
  }
  for (const value of ['2026-09-29T12:30:00Z', '2026-09-29T00:00:00.001Z', '2026-09-29T00:00:00+00:00']) {
    assert.equal(shown(value), new Date(value).toLocaleString())
  }
  assert.equal(shown('2026-09-29T12:30:00.1234567Z'), new Date('2026-09-29T12:30:00.123Z').toLocaleString())
  assert.equal(shown('2026-09-29'), '2026-09-29')
  assert.equal(shown('2026-99-99T12:30:00Z'), '2026-99-99T12:30:00Z')
})

test('row search includes explicitly shown system columns and undisplayed custom columns', () => {
  const rows = [{ ID: 12345, Title: 'One', Category: { Value: 'Red' }, A: '', B: '', C: '', D: '', E: '', Later: 'Needle' },
    { ID: 67890, Title: 'Two', Category: { Value: 'Blue' } }]
  assert.deepEqual(matchingRows(rows, '12345'), [])
  assert.deepEqual(matchingRows(rows, '12345', ['ID']), [rows[0]])
  assert.deepEqual(matchingRows(rows, ' needle '), [rows[0]])
  assert.deepEqual(matchingRows(rows, 'BLUE'), [rows[1]])
  assert.deepEqual(matchingRows(rows, 'missing'), [])
  assert.equal(matchingRows(rows, '  '), rows)
})

test('detail columns keep explicit fields without duplicate Created or Modified entries', () => {
  assert.deepEqual(detailColumns([{ Title: 'One', ID: 1, Modified: '', Created: '' }], ['ID', 'Created', 'Modified']),
    ['ID', 'Created', 'Modified', 'Title'])
  assert.equal(label('Due_x0020_Date'), 'Due Date')
  assert.equal(label('ProjectName'), 'Project Name')
})

test('row grouping uses display values and handles blanks and no grouping', () => {
  const rows = [{ Category: { Value: 'Red' } }, { Category: { Value: 'Red' } }, { Category: null }, {}]
  assert.deepEqual([...groupsOf(rows, 'Category')], [['Red', 2], ['(blank)', 2]])
  assert.deepEqual([...groupsOf(rows, '')], [])
})

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function reader(requests, state) {
  return async (pending) => {
    const current = requests.start()
    state.phase = 'loading'
    try {
      const value = await pending.promise
      if (!current()) return
      state.value = value
      state.phase = 'ready'
    } catch (error) {
      if (!current()) return
      state.error = error.message
      state.phase = 'error'
    }
  }
}

test('latest request success wins over selections and the initial automatic open', async () => {
  const state = {}
  const read = reader(latestRequest(), state)
  const auto = deferred(), alpha = deferred(), beta = deferred()
  const a = read(auto), b = read(alpha), c = read(beta)
  beta.resolve('Beta')
  await c
  alpha.resolve('Alpha')
  await b
  auto.resolve('Initial')
  await a
  assert.deepEqual(state, { phase: 'ready', value: 'Beta' })
})

test('superseded request failures cannot replace the latest success', async () => {
  const state = {}
  const read = reader(latestRequest(), state)
  const alpha = deferred(), beta = deferred()
  const a = read(alpha), b = read(beta)
  beta.resolve('Beta')
  await b
  alpha.reject(new Error('Old error'))
  await a
  assert.deepEqual(state, { phase: 'ready', value: 'Beta' })
})

test('superseded success cannot replace the latest failure', async () => {
  const state = {}
  const read = reader(latestRequest(), state)
  const alpha = deferred(), beta = deferred()
  const a = read(alpha), b = read(beta)
  beta.reject(new Error('Current error'))
  await b
  alpha.resolve('Alpha')
  await a
  assert.deepEqual(state, { phase: 'error', error: 'Current error' })
})

for (const outcome of ['resolve', 'reject']) {
  test(`superseded ${outcome} leaves the current request loading`, async () => {
    const state = {}
    const read = reader(latestRequest(), state)
    const alpha = deferred(), beta = deferred()
    const a = read(alpha), b = read(beta)
    alpha[outcome](outcome === 'resolve' ? 'Alpha' : new Error('Old error'))
    await a
    assert.deepEqual(state, { phase: 'loading' })
    beta.resolve('Beta')
    await b
    assert.deepEqual(state, { phase: 'ready', value: 'Beta' })
  })

  test(`cancelling a request ignores its later ${outcome}`, async () => {
    const requests = latestRequest()
    const state = {}
    const pending = deferred()
    const done = reader(requests, state)(pending)
    requests.cancel()
    state.phase = 'ready'
    pending[outcome](outcome === 'resolve' ? 'Old value' : new Error('Old error'))
    await done
    assert.deepEqual(state, { phase: 'ready' })
    const current = requests.start()
    assert.equal(current(), true)
  })
}

test('request sequences are independent and cancelled tokens never become current again', () => {
  const first = latestRequest(), second = latestRequest()
  const old = first.start(), independent = second.start()
  first.cancel()
  const current = first.start()
  assert.equal(old(), false)
  assert.equal(current(), true)
  assert.equal(independent(), true)
  first.start()
  assert.equal(current(), false)
})

test('connector empty results and failures preserve their meaning', () => {
  assert.deepEqual(listOf({ value: [] }), [])
  assert.deepEqual(listOf({ value: [1] }), [1])
  assert.deepEqual(listOf([2]), [2])
  assert.deepEqual(listOf(null), [])
  assert.equal(need({ success: true, data: 0 }, 'Reading'), 0)
  assert.throws(() => need({ success: false, error: { message: 'Denied' } }, 'Reading'), /Denied/)
})

test('binary content uses data URLs without a network or object URL fallback', () => {
  const bytes = new Uint8Array(0x10002).map((_, i) => i % 256)
  const url = dataUrl(bytes, 'image/png')
  assert.match(url, /^data:image\/png;base64,/)
  assert.deepEqual(asBytes(url), bytes)
  assert.equal(imageUrl(url), url)
  assert.throws(() => imageUrl('https://contoso.example/photo.png'), /Expected content/)
})

test('media classification distinguishes supported images, audio and video', () => {
  const mime = { mp4: 'video/mp4', mp3: 'audio/mpeg', png: 'image/png' }
  assert.equal(extensionOf('Team.VIDEO.MP4'), 'mp4')
  assert.equal(typeOf('Film.mp4', mime), 'video/mp4')
  assert.equal(kindOf('Film.mp4', mime), 'video')
  assert.equal(kindOf('Sound.MP3', mime), 'audio')
  assert.equal(kindOf('Poster.png', mime), 'image')
  assert.equal(kindOf('Notes.pdf', mime), null)
  assert.equal(extensionOf('README'), '')
})
