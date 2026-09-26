import { useEffect, useRef, useState } from 'react'
import { SharePointService } from './bound'
import { CONFIG } from './config'
import { asBytes, dataUrl, formatBytes, latestRequest, listOf, message, need, type Phase } from './connector'
import { kindOf, typeOf, type MediaKind } from './media'
import './App.css'

// Where the media live: a SharePoint site and a folder in one of its document libraries (see config.ts).
type File = { Id?: string; Name?: string; Path?: string; Size?: number; IsFolder?: boolean }
type Playing = { name: string; kind: MediaKind; url: string; bytes: number }

async function listMedia(): Promise<File[]> {
  const folder = need(await SharePointService.GetFolderMetadataByPath(CONFIG.site, CONFIG.folder), `Folder ${CONFIG.folder}`)
  if (!folder?.Id) throw new Error(`Folder not found: ${CONFIG.folder}`)
  const items = listOf<File>(need(await SharePointService.ListFolder(CONFIG.site, folder.Id), 'Listing the folder'))
  return items
    .filter((f) => !f.IsFolder && f.Name && kindOf(f.Name, CONFIG.mime))
    .sort((a, b) => (a.Name ?? '').localeCompare(b.Name ?? ''))
}

async function load(file: File): Promise<Playing> {
  const name = file.Name ?? ''
  if ((file.Size ?? 0) > CONFIG.maxBytes) {
    throw new Error(`${name} is ${formatBytes(file.Size ?? 0)}; this app plays files up to ${formatBytes(CONFIG.maxBytes)}`)
  }
  const got = await SharePointService.GetFileContentByPath(CONFIG.site, file.Path ?? `${CONFIG.folder}/${name}`, false)
  const bytes = asBytes(need(got, `Reading ${name}`) as unknown)
  return { name, kind: kindOf(name, CONFIG.mime) as MediaKind, url: dataUrl(bytes, typeOf(name, CONFIG.mime)), bytes: bytes.length }
}

export default function App() {
  const [files, setFiles] = useState<File[]>([])
  const [playing, setPlaying] = useState<Playing | null>(null)
  const [status, setStatus] = useState('Looking in SharePoint…')
  const [phase, setPhase] = useState<Phase>('loading')
  const [time, setTime] = useState(0)
  const requests = useRef(latestRequest())

  async function open(file: File) {
    const current = requests.current.start()
    setPlaying(null)
    setTime(0)
    setPhase('loading')
    setStatus(`Loading ${file.Name}…`)
    try {
      const p = await load(file)
      if (!current()) return
      setPlaying(p)
      setPhase('ready')
      setStatus(`${p.name} · ${formatBytes(p.bytes)}`)
    } catch (e) {
      if (!current()) return
      setPhase('error')
      setStatus(message(e))
    }
  }

  useEffect(() => {
    let current = true
    listMedia()
      .then((found) => {
        if (!current) return
        setFiles(found)
        if (found.length === 0) {
          setPhase('empty')
          setStatus(`No ${CONFIG.media.join(' or ')} files in ${CONFIG.folder}`)
        } else void open(found[0])
      })
      .catch((e: unknown) => {
        if (!current) return
        setPhase('error')
        setStatus(message(e))
      })
    return () => {
      current = false
      requests.current.cancel()
    }
  }, [])

  return (
    <main className="app">
      <div className="bar">
        <div>
          <h1>{CONFIG.title}</h1>
          <p className="where">SharePoint · {CONFIG.folder}</p>
        </div>
      </div>
      <div className="stage">
        {playing?.kind === 'video' && (
          <video key={playing.name} src={playing.url} controls autoPlay muted playsInline data-testid="media"
                 onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)} />
        )}
        {playing?.kind === 'audio' && (
          <audio key={playing.name} src={playing.url} controls autoPlay data-testid="media"
                 onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)} />
        )}
        {playing?.kind === 'image' && <img key={playing.name} src={playing.url} alt={playing.name} data-testid="media" />}
      </div>
      <p className="status" data-testid="status" data-state={phase} role="status">{status}</p>
      <p className="meta" data-testid="meta">{playing && playing.kind !== 'image' ? `${time.toFixed(1)} s` : ''}</p>
      <ul className="files" data-testid="items" data-count={files.length}>
        {files.map((f) => (
          <li key={f.Id ?? f.Name}>
            <button type="button" aria-current={playing?.name === f.Name} onClick={() => void open(f)}>
              <span>{f.Name}</span>
              <span className="size">{formatBytes(f.Size ?? 0)}</span>
            </button>
          </li>
        ))}
      </ul>
    </main>
  )
}
