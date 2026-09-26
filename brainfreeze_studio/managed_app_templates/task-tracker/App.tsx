import { useEffect, useRef, useState, type FormEvent } from 'react'
import { TaskService } from './bound'
import { CONFIG } from './config'
import { latestRequest, listOf, message, need, type Phase } from './connector'
import './App.css'

// A Dataverse table through its generated service (the add-dataverse skill): list the active rows, add one, mark it
// done, delete it. Column names come from config.ts; the record types come from the generated service itself, so a
// table that doesn't match the spec fails the type check or the first call instead of writing the wrong columns.
type Task = { id: string; name: string; done: boolean; due: string | null; notes: string }
type Raw = Record<string, unknown>
type NewRecord = Parameters<typeof TaskService.CreateRecord>[0]
type Change = Parameters<typeof TaskService.UpdateRecord>[1]
type Show = 'open' | 'done' | 'all'

const F = CONFIG.fields

function task(r: Raw): Task {
  return { id: String(r[F.id] ?? ''), name: String(r[F.name] ?? ''), done: r[F.done] === true,
    due: typeof r[F.due] === 'string' ? (r[F.due] as string) : null, notes: String(r[F.notes] ?? '') }
}

async function load(): Promise<Task[]> {
  const got = await TaskService.ListRecords({ $select: [F.id, F.name, F.done, F.due, F.notes].join(','),
    $filter: 'statecode eq 0', $orderby: 'createdon desc' })
  return listOf<Raw>(need(got, 'Reading tasks')).map(task).filter((t) => t.id)
}

async function add(name: string, due: string, notes: string): Promise<void> {
  const record: Raw = { [F.name]: name, [F.done]: false, statecode: 0 }
  if (due) record[F.due] = new Date(`${due}T17:00:00`).toISOString()   // due at the end of that working day
  if (notes) record[F.notes] = notes
  need(await TaskService.CreateRecord(record as unknown as NewRecord), `Adding ${name}`)
}

async function setDone(t: Task, done: boolean): Promise<void> {
  need(await TaskService.UpdateRecord(t.id, { [F.done]: done } as unknown as Change), `Updating ${t.name}`)
}

async function remove(t: Task): Promise<void> {
  need(await TaskService.DeleteRecord(t.id), `Deleting ${t.name}`)
}

const late = (t: Task) => !t.done && t.due !== null && new Date(t.due).getTime() < Date.now()
const when = (iso: string) => new Date(iso).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [show, setShow] = useState<Show>('open')
  const [name, setName] = useState('')
  const [due, setDue] = useState('')
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(true)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [status, setStatus] = useState('Reading tasks…')
  const [phase, setPhase] = useState<Phase>('loading')
  const requests = useRef(latestRequest())
  const writing = useRef(false)

  const settle = (list: Task[], note: string) => {
    setPhase(list.length ? 'ready' : 'empty')
    setStatus(note)
  }

  async function refresh(current: () => boolean) {
    const list = await load()
    if (!current()) return
    setTasks(list)
    settle(list, `${list.filter((t) => !t.done).length} open · ${list.filter((t) => t.done).length} done`)
  }

  useEffect(() => {
    const current = requests.current.start()
    refresh(current)
      .catch((e: unknown) => {
        if (!current()) return
        setPhase('error')
        setStatus(message(e))
      })
      .finally(() => { if (current()) setBusy(false) })
    return () => { requests.current.cancel() }
  }, [])

  // every write is followed by a fresh read, so the list shows what Dataverse holds, not what the app hoped it wrote
  async function run(what: string, write: () => Promise<void>, afterWrite?: () => void) {
    if (busy || writing.current) return
    writing.current = true
    const current = requests.current.start()
    setBusy(true)
    setPhase('loading')
    setStatus(what)
    try {
      await write()
      if (!current()) return
      afterWrite?.()
      await refresh(current)
    } catch (e) {
      if (!current()) return
      setPhase('error')
      setStatus(message(e))
    } finally {
      writing.current = false
      if (current()) {
        setBusy(false)
        setConfirming(null)
      }
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    const title = name.trim()
    if (!title) return
    void run(`Adding ${title}…`, () => add(title, due, notes.trim()), () => {
      setName('')
      setDue('')
      setNotes('')
    })
  }

  const toggle = (t: Task) => void run(`${t.done ? 'Reopening' : 'Completing'} ${t.name}…`, () => setDone(t, !t.done))

  const drop = (t: Task) => {
    if (busy || writing.current) return
    if (confirming !== t.id) {
      setConfirming(t.id)
      return
    }
    void run(`Deleting ${t.name}…`, () => remove(t))
  }

  const visible = tasks.filter((t) => show === 'all' || (show === 'done' ? t.done : !t.done))
  const counts = { open: tasks.filter((t) => !t.done).length, done: tasks.filter((t) => t.done).length, all: tasks.length }

  return (
    <main className="app">
      <div className="bar">
        <div>
          <h1>{CONFIG.title}</h1>
          <p className="where">Dataverse · {CONFIG.table}</p>
        </div>
      </div>
      <form className="panel add" onSubmit={submit}>
        <div className="field">
          <label htmlFor="new-title">Task</label>
          <input id="new-title" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} required disabled={busy}
                 placeholder="What needs doing?" data-testid="new-title" />
        </div>
        <div className="field">
          <label htmlFor="new-due">Due</label>
          <input id="new-due" type="date" value={due} onChange={(e) => setDue(e.target.value)} disabled={busy}
                 data-testid="new-due" />
        </div>
        <button className="btn primary" type="submit" disabled={busy || !name.trim()} data-testid="add">Add task</button>
        <div className="field notes">
          <label htmlFor="new-notes">Notes</label>
          <textarea id="new-notes" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000}
                    disabled={busy} data-testid="new-notes" />
        </div>
      </form>
      <div className="tabs" role="group" aria-label="Show">
        {(['open', 'done', 'all'] as const).map((s) => (
          <button key={s} type="button" aria-pressed={show === s} onClick={() => setShow(s)} data-testid={`show-${s}`}>
            {s === 'open' ? 'Open' : s === 'done' ? 'Done' : 'All'} ({counts[s]})
          </button>
        ))}
      </div>
      <p className="status" data-testid="status" data-state={phase} role="status">{status}</p>
      {phase !== 'loading' && visible.length === 0 && phase !== 'error' && (
        <p className="empty">{show === 'done' ? 'Nothing finished yet.' : show === 'open' ? 'All clear.' : 'No tasks yet.'}</p>
      )}
      <ul className="tasks" data-testid="tasks" data-count={visible.length}>
        {visible.map((t) => (
          <li key={t.id} className="task" data-testid="task" data-id={t.id} data-title={t.name} data-done={t.done}>
            <input type="checkbox" checked={t.done} disabled={busy} onChange={() => toggle(t)}
                   aria-label={`${t.done ? 'Reopen' : 'Complete'} ${t.name}`} data-testid="toggle" />
            <div>
              <b>{t.name}</b>
              {(t.due || t.notes) && (
                <span>
                  {t.due && <em className={late(t) ? 'late' : undefined}>{late(t) ? 'Overdue · ' : 'Due '}{when(t.due)}</em>}
                  {t.due && t.notes && ' · '}
                  {t.notes}
                </span>
              )}
            </div>
            <button type="button" className="btn small danger" disabled={busy} onClick={() => drop(t)} data-testid="delete">
              {confirming === t.id ? 'Confirm delete' : 'Delete'}
            </button>
          </li>
        ))}
      </ul>
    </main>
  )
}
