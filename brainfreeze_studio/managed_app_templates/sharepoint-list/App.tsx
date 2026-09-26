import { useEffect, useState } from 'react'
import { ListService } from './bound'
import { CONFIG } from './config'
import { listOf, message, need, type Phase } from './connector'
import { columnsOf, detailColumns, groupsOf, label, matchingRows, shown, type Row } from './rows'
import './App.css'

// A SharePoint list as a table data source (the add-sharepoint skill's table mode), read-only: getAll is the one call,
// so the app needs only the "get" verb.

export default function App() {
  const [rows, setRows] = useState<Row[]>([])
  const [term, setTerm] = useState('')
  const [selected, setSelected] = useState<Row | null>(null)
  const [status, setStatus] = useState(`Reading ${CONFIG.list}…`)
  const [phase, setPhase] = useState<Phase>('loading')

  useEffect(() => {
    let current = true
    ListService.getAll({ top: CONFIG.top })
      .then((r) => {
        if (!current) return
        const all = listOf<Row>(need(r, `Reading ${CONFIG.list}`))
        setRows(all)
        setSelected(all[0] ?? null)
        setPhase(all.length ? 'ready' : 'empty')
        setStatus(all.length ? `${all.length} item${all.length === 1 ? '' : 's'}` : `${CONFIG.list} has no items`)
      })
      .catch((e: unknown) => {
        if (!current) return
        setPhase('error')
        setStatus(message(e))
      })
    return () => { current = false }
  }, [])

  const columns = columnsOf(rows, CONFIG.columns)
  const details = detailColumns(rows, CONFIG.columns)
  const q = term.trim().toLowerCase()
  const matching = matchingRows(rows, term, CONFIG.columns)
  const groups = groupsOf(matching, CONFIG.groupBy)
  const detail = selected && matching.includes(selected) ? selected : matching[0] ?? null

  return (
    <main className="app">
      <div className="bar">
        <div>
          <h1>{CONFIG.title}</h1>
          <p className="where">SharePoint list · {CONFIG.list}</p>
        </div>
        <div className="search" role="search">
          <input value={term} onChange={(e) => setTerm(e.target.value)} placeholder="Filter items"
                 aria-label="Filter items" data-testid="search" />
        </div>
      </div>
      <p className="status" data-testid="status" data-state={phase} role="status">{status}</p>
      {phase === 'ready' && (
        <div className="stats">
          <div className="stat"><b>{rows.length}</b><span>items</span></div>
          {q && <div className="stat"><b data-testid="match-count">{matching.length}</b><span>match “{term.trim()}”</span></div>}
          {[...groups.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5).map(([g, n]) => (
            <div className="stat" key={g}><b>{n}</b><span>{g}</span></div>
          ))}
        </div>
      )}
      {phase === 'empty' && <p className="empty">Nothing in this list yet.</p>}
      {rows.length > 0 && (
        <div className="columns">
          <div className="table-wrap">
            {matching.length === 0 && <p className="empty">No items match “{term.trim()}”.</p>}
            {columns.length === 0 && <p className="empty">No displayable columns are present in these items.</p>}
            <table data-testid="items" data-count={matching.length}>
              <thead><tr>{columns.map((c) => <th key={c}>{label(c)}</th>)}</tr></thead>
              <tbody>
                {matching.map((r, i) => (
                  <tr key={String(r.ID ?? r.Id ?? i)} aria-current={detail === r} onClick={() => setSelected(r)}
                      data-testid="item">
                    {columns.map((c) => {
                      const text = shown(r[c])
                      return <td key={c}><div className={text.length <= 24 ? 'cell short' : 'cell'}>{text}</div></td>
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {detail && (
            <aside className="panel detail" data-testid="detail">
              <dl>
                {details.filter((c) => shown(detail[c])).map((c) => (
                  <div key={c} style={{ display: 'contents' }}><dt>{label(c)}</dt><dd>{shown(detail[c])}</dd></div>
                ))}
              </dl>
            </aside>
          )}
        </div>
      )}
    </main>
  )
}
