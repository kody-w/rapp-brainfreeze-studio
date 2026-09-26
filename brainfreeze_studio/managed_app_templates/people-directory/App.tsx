import { useEffect, useRef, useState, type FormEvent } from 'react'
import { UsersService } from './bound'
import { CONFIG } from './config'
import { imageUrl, initials, latestRequest, listOf, message, need, type Phase } from './connector'
import './App.css'

// The Office 365 Users connector the way the add-office365-users skill uses it: the signed-in user's profile, their
// manager and direct reports, a directory search, and profile photos as data: URLs. Only the fields shown are read.
const FIELDS = 'id,displayName,jobTitle,department,mail,userPrincipalName,officeLocation'

type Person = { id: string; name: string; title: string; department: string; mail: string; office: string }
type Raw = Record<string, unknown>
type Status = { phase: Phase; status: string }

const text = (v: unknown) => (typeof v === 'string' ? v : '')

// MyProfile_V2, Manager_V2 and DirectReports_V2 return Graph users (camelCase); SearchUserV2 returns the connector's
// users (PascalCase). One shape for both.
function person(raw: unknown): Person | null {
  const r = (raw ?? {}) as Raw
  const upn = text(r.userPrincipalName) || text(r.UserPrincipalName)
  const id = text(r.id) || text(r.Id) || upn
  if (!id) return null
  return {
    id,
    name: text(r.displayName) || text(r.DisplayName) || upn,
    title: text(r.jobTitle) || text(r.JobTitle),
    department: text(r.department) || text(r.Department),
    mail: text(r.mail) || text(r.Mail) || upn,
    office: text(r.officeLocation) || text(r.OfficeLocation),
  }
}

const people = (data: unknown) => listOf<unknown>(data).map(person).filter((p): p is Person => p !== null)

async function myProfile(): Promise<Person> {
  const me = person(need(await UsersService.MyProfile_V2(FIELDS), 'Reading your profile'))
  if (!me) throw new Error('Your profile did not include an ID or user principal name')
  return me
}

async function managerOf(id: string): Promise<Person | null> {
  const r = await UsersService.Manager_V2(id, FIELDS)
  // no manager in the directory is an answer, not a failure (the directory answers 404)
  if (!r.success && /404|not ?found/i.test(r.error?.message ?? '')) return null
  return person(need(r, 'Reading your manager'))
}

async function reportsOf(id: string): Promise<Person[]> {
  return people(need(await UsersService.DirectReports_V2(id, FIELDS, 100), 'Reading your direct reports'))
}

async function search(term: string): Promise<Person[]> {
  return people(need(await UsersService.SearchUserV2(term, CONFIG.searchTop, true), `Searching for ${term}`))
}

async function photoOf(id: string): Promise<string | null> {
  const meta = need(await UsersService.UserPhotoMetadata(id), 'Checking the profile photo')
  if (!meta?.HasPhoto) return null
  const photo = need(await UsersService.UserPhoto_V2(id), 'Loading the profile photo')
  return photo ? imageUrl(photo as unknown, meta.ContentType || 'image/jpeg') : null
}

function Avatar({ who, photo, big }: { who: Person; photo?: string | null; big?: boolean }) {
  return (
    <span className={big ? 'avatar big' : 'avatar'} aria-hidden="true">
      {photo ? <img src={photo} alt="" /> : initials(who.name)}
    </span>
  )
}

export default function App() {
  const [me, setMe] = useState<Person | null>(null)
  const [manager, setManager] = useState<Person | null>(null)
  const [reports, setReports] = useState<Person[] | null>(null)
  const [found, setFound] = useState<Person[] | null>(null)
  const [term, setTerm] = useState('')
  const [searched, setSearched] = useState('')
  const [selected, setSelected] = useState<Person | null>(null)
  const [photos, setPhotos] = useState<Record<string, string | null>>({})
  const [photoProblem, setPhotoProblem] = useState('')
  const [directoryState, setDirectoryState] = useState<Status>({ phase: 'loading', status: 'Reading the directory…' })
  const [searchState, setSearchState] = useState<Status | null>(null)
  const searches = useRef(latestRequest())
  const { phase, status } = searchState ?? directoryState

  useEffect(() => {
    let current = true
    ;(async () => {
      const mine = await myProfile()
      if (!current) return
      setMe(mine)
      setSelected((was) => was ?? mine)
      const [boss, team] = await Promise.allSettled([managerOf(mine.id), reportsOf(mine.id)])
      if (!current) return
      if (boss.status === 'fulfilled') setManager(boss.value)
      if (team.status === 'fulfilled') setReports(team.value)
      if (boss.status === 'rejected') throw boss.reason
      if (team.status === 'rejected') throw team.reason
      setDirectoryState({ phase: 'ready',
        status: `${team.value.length} direct report${team.value.length === 1 ? '' : 's'}${
          boss.value ? ` · reports to ${boss.value.name}` : ''}` })
    })().catch((e: unknown) => {
      if (!current) return
      setDirectoryState({ phase: 'error', status: message(e) })
    })
    return () => {
      current = false
      searches.current.cancel()
    }
  }, [])

  // photos for everyone on screen, each fetched once; one that fails shows initials and is reported below the status
  const shown = [me, manager, ...(reports ?? []), ...(found ?? [])].filter((p): p is Person => p !== null)
  const missing = shown.map((p) => p.id).filter((id, i, all) => all.indexOf(id) === i && !(id in photos))
  const missingKey = missing.join('|')
  useEffect(() => {
    for (const id of missing) {
      setPhotos((was) => ({ ...was, [id]: null }))
      photoOf(id)
        .then((url) => setPhotos((was) => ({ ...was, [id]: url })))
        .catch((e: unknown) => setPhotoProblem(`A profile photo could not load: ${message(e)}`))
    }
  }, [missingKey])

  async function find(e: FormEvent) {
    e.preventDefault()
    const current = searches.current.start()
    const q = term.trim()
    setFound(null)
    setSearched(q)
    if (!q) {
      setSearchState(null)
      return
    }
    setSearchState({ phase: 'loading', status: `Searching for ${q}…` })
    try {
      const hits = await search(q)
      if (!current()) return
      setFound(hits)
      setSearchState({ phase: hits.length ? 'ready' : 'empty',
        status: `${hits.length} match${hits.length === 1 ? '' : 'es'} for ${q}` })
    } catch (err) {
      if (!current()) return
      setSearchState({ phase: 'error', status: message(err) })
    }
  }

  const tile = (p: Person) => (
    <button key={p.id} type="button" className="person" data-testid="person" aria-current={selected?.id === p.id}
            onClick={() => setSelected(p)}>
      <Avatar who={p} photo={photos[p.id]} />
      <span className="who"><b>{p.name}</b><span>{p.title || p.mail}</span></span>
    </button>
  )

  return (
    <main className="app">
      <div className="bar">
        <div>
          <h1>{CONFIG.title}</h1>
          <p className="where">Office 365 Users · your organization</p>
        </div>
        <form className="search" onSubmit={(e) => void find(e)} role="search">
          <input value={term} onChange={(e) => setTerm(e.target.value)} placeholder="Search people by name or email"
                 aria-label="Search people" data-testid="search" />
          <button className="btn primary" type="submit" data-testid="search-go">Search</button>
        </form>
      </div>
      <p className="status" data-testid="status" data-state={phase} role="status">{status}</p>
      {photoProblem && <p className="where">{photoProblem}</p>}
      <div className="columns">
        <div>
          {found !== null && (
            <>
              <h2 className="section-title">Search results</h2>
              {found.length === 0 && <p className="empty">Nobody matches {searched}.</p>}
              <div className="people" data-testid="results" data-count={found.length}>{found.map(tile)}</div>
            </>
          )}
          {me && (<><h2 className="section-title">You</h2><div className="people">{tile(me)}</div></>)}
          {manager && (<><h2 className="section-title">Your manager</h2><div className="people">{tile(manager)}</div></>)}
          {me && reports !== null && (
            <>
              <h2 className="section-title">Your direct reports</h2>
              {reports.length === 0
                ? <p className="empty">No direct reports in the directory.</p>
                : <div className="people" data-testid="reports" data-count={reports.length}>{reports.map(tile)}</div>}
            </>
          )}
        </div>
        {selected && (
          <aside className="panel card" data-testid="card">
            <Avatar who={selected} photo={photos[selected.id]} big />
            <h2>{selected.name}</h2>
            <dl>
              {selected.title && (<><dt>Title</dt><dd>{selected.title}</dd></>)}
              {selected.department && (<><dt>Department</dt><dd>{selected.department}</dd></>)}
              {selected.office && (<><dt>Office</dt><dd>{selected.office}</dd></>)}
              {selected.mail && (<><dt>Email</dt><dd>{selected.mail}</dd></>)}
            </dl>
          </aside>
        )}
      </div>
    </main>
  )
}
