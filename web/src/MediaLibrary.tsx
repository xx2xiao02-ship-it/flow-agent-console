import { videoModels } from './videoModels'
import { useEffect, useState } from 'react'
import { imageModels } from './imageModels'
import './storage.css'
import MediaMigration from './MediaMigration'

export type LibraryMedia = {
  id: string; name: string; kind: 'image' | 'video'; size: number; created_at: string; url: string
  title?: string; prompt?: string | null; task_id?: string | null; model?: string | null
  parameters?: Record<string, string | number>; index_status?: string
}
type Library = { directory: string; index_path: string; total: number; indexed: number; matching: number; items: LibraryMedia[]; offset: number; limit: number; directories?: { id: string; directory: string; available: boolean; active: boolean }[] }
const statuses: Record<string, string> = { missing_prompt: '未找到原提示词', ambiguous: '来源存在冲突，未自动关联', file_changed: '文件已变更，需核对原提示词' }
export const modelLabel = (model?: string | null) => videoModels.find(item => item.value === model)?.label || imageModels.find(item => item.value === model)?.label || model || ''

export default function MediaLibrary({ onPreview }: { onPreview: (media: LibraryMedia) => void }) {
  const [library, setLibrary] = useState<Library | null>(null)
  const [search, setSearch] = useState('')
  const [kind, setKind] = useState<'all' | 'image' | 'video'>('all')
  const [offset, setOffset] = useState(0)
  const [revision, setRevision] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [directory, setDirectory] = useState('')
  const [editingDirectory, setEditingDirectory] = useState(false)
  const [savingDirectory, setSavingDirectory] = useState(false)
  const [choosingDirectory, setChoosingDirectory] = useState(false)
  const [storageNotice, setStorageNotice] = useState('')
  const [storageFailed, setStorageFailed] = useState(false)
  const [migrationPending, setMigrationPending] = useState(false)
  useEffect(() => { if (library && !editingDirectory) setDirectory(library.directory) }, [library, editingDirectory])
  async function chooseDirectory() {
    setChoosingDirectory(true); setStorageFailed(false); setStorageNotice('请在本机弹出的窗口中选择文件夹；选择后可仅更改保存位置，或整体迁移已有素材。')
    try {
      const response = await fetch('/console/api/media/storage/choose', { method: 'POST' })
      const body = await response.json()
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '无法打开文件夹选择窗口')
      if (body.cancelled) { setStorageNotice('已取消选择，保存位置未改变。'); return }
      setDirectory(body.directory); setEditingDirectory(true); setStorageNotice('已选择目标文件夹，请选择仅更改保存位置或整体迁移。')
    } catch (e) { setStorageFailed(true); setStorageNotice(e instanceof Error ? e.message : '选择失败，可直接输入完整路径。') }
    finally { setChoosingDirectory(false) }
  }
  async function saveDirectory() {
    if (!directory.trim() || savingDirectory || choosingDirectory) return
    setSavingDirectory(true); setStorageFailed(false); setStorageNotice('正在检查目录是否可写…')
    try {
      const response = await fetch('/console/api/media/storage', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ directory: directory.trim() }) })
      const body = await response.json()
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '保存目录失败')
      setLibrary(previous => previous ? { ...previous, directory: body.directory, directories: body.directories } : previous)
      setDirectory(body.directory); setEditingDirectory(false); setRevision(value => value + 1)
      setStorageNotice('保存路径已生效，重启后保留。后续生成存入此目录，旧素材继续保留并可检索下载。')
    } catch (e) { setStorageFailed(true); setStorageNotice(e instanceof Error ? e.message : '保存目录失败') }
    finally { setSavingDirectory(false) }
  }
  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoading(true)
    const timer = setTimeout(async () => {
      try {
        const response = await fetch(`/console/api/media/library?${new URLSearchParams({ search, kind, offset: String(offset), limit: '60' })}`, { signal: controller.signal })
        const body = await response.json()
        if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '素材读取失败')
        if (active) { setLibrary(body); setError('') }
      } catch (e) { if (active) setError(e instanceof Error ? e.message : '素材读取失败') }
      finally { if (active) setLoading(false) }
    }, 250)
    return () => { active = false; clearTimeout(timer); controller.abort() }
  }, [search, kind, offset, revision])
  // The generation service saves to disk; refresh only reads/indexes local files.
  useEffect(() => { const timer = setInterval(() => setRevision(value => value + 1), 15000); return () => clearInterval(timer) }, [])

  async function openFolder() {
    try {
      const response = await fetch('/console/api/media/open-folder', { method: 'POST' })
      const body = await response.json()
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '打开文件夹失败')
      setError('')
    } catch (e) { setError(e instanceof Error ? e.message : '打开文件夹失败') }
  }
  return <>
    <div className="page-title"><span className="eyebrow">LIBRARY / FLOW AGENT</span><h1>本地素材库</h1><p>图片和视频已保存在本机。按原始提示词、文件名、模型或任务编号搜索，不会重新生成。</p></div>
    <section className="panel library-storage storage-settings"><div className="storage-heading"><div><strong>本地保存位置</strong><p>选择目标文件夹后，可仅更改后续保存位置，也可把已有素材整体迁移。</p></div><button className="secondary" disabled={!library || choosingDirectory || savingDirectory || migrationPending} onClick={() => void chooseDirectory()}>{choosingDirectory ? '等待选择…' : '选择文件夹'}</button></div>
      <div className="storage-editor"><input aria-label="素材保存目录" placeholder="例如 E:\FlowMedia" value={directory} disabled={!library || savingDirectory || choosingDirectory || migrationPending} onChange={e => { setDirectory(e.target.value); setEditingDirectory(true); setStorageNotice('') }} /><button className="primary" disabled={!library || savingDirectory || choosingDirectory || migrationPending || !directory.trim() || directory.trim() === library.directory} onClick={() => void saveDirectory()}>{savingDirectory ? '保存中…' : '仅更改保存位置'}</button></div>
      <p className="storage-path">当前生效：{library?.directory || '正在读取…'}</p>
      {storageNotice && <p className={`storage-notice${storageFailed ? ' danger-text' : ''}`} role="status">{storageNotice}</p>}
      {(library?.directories?.length ?? 0) > 1 && <details className="storage-history"><summary>已保留 {library?.directories?.length} 个素材目录，旧素材仍可检索</summary><ul>{library?.directories?.map(item => <li key={item.id}>{item.directory} · {item.active ? '当前保存' : '历史目录'}{!item.available && ' · 磁盘未连接或目录不可用'}</li>)}</ul></details>}
      <MediaMigration directory={directory} disabled={!library || savingDirectory || choosingDirectory} onStatus={(pending, target) => { setMigrationPending(pending); if (target) { setDirectory(target); setEditingDirectory(false); setRevision(value => value + 1) } }} />
      <div className="storage-bottom"><small>共 {library?.total ?? '—'} 个素材 · {library?.indexed ?? '—'} 个已关联提示词 · 备份时请一并复制素材与 flow-media-index.json。</small><div className="library-actions"><button className="secondary" onClick={() => void openFolder()}>打开本地文件夹</button><a className="secondary" href="/console/api/media/index" download="flow-media-index.json">导出素材索引</a><button className="secondary" disabled={loading} onClick={() => setRevision(value => value + 1)}>刷新素材</button></div></div></section>
    <section className="panel media-panel"><div className="media-toolbar"><div className="segment compact">{(['all', 'image', 'video'] as const).map(value => <button key={value} className={kind === value ? 'selected' : ''} onClick={() => { setKind(value); setOffset(0) }}>{value === 'all' ? '全部' : value === 'image' ? '图片' : '视频'}</button>)}</div><input className="search" aria-label="搜索素材提示词" placeholder="搜索提示词、文件名、模型、任务编号…" value={search} onChange={e => { setSearch(e.target.value); setOffset(0) }} /></div>
      {error && <p role="alert" className="danger-text">{error}</p>}
      <p className="helper" role="status">{loading ? '正在检索本地素材…' : `找到 ${library?.matching ?? 0} 个素材`}{search.trim() && ' · 多个关键词以空格分隔，同时匹配'}</p>
      {library?.items.length ? <div className="media-grid">{library.items.map(media => <article className="media-card" key={media.id}><button className="media-thumb" onClick={() => onPreview(media)}>{media.kind === 'image' ? <img src={media.url} alt={media.title || media.name} /> : <><video src={media.url} preload="metadata" /><span className="play">▶</span></>}</button><div className="media-info"><strong className="media-prompt-title" title={media.prompt || media.name}>{media.prompt || statuses[media.index_status || 'missing_prompt']}</strong><small className="media-file-name" title={media.name}>{media.name}</small><small>{media.kind === 'image' ? '图片' : '视频'} · {(media.size / 1024 / 1024).toFixed(1)} MB{media.model ? ` · ${modelLabel(media.model)}` : ''}</small><div><button className="text-link" onClick={() => onPreview(media)}>预览与提示词</button><a className="text-link" href={media.url} download>下载 ↗</a></div></div></article>)}</div> : !loading && <div className="empty"><strong>没有找到素材</strong><small>换个关键词，或等待生成完成后刷新。</small></div>}
      {library && library.matching > library.limit && <div className="history-pages"><button className="secondary" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - library.limit))}>上一页</button><span>{offset + 1}–{Math.min(offset + library.limit, library.matching)} / {library.matching}</span><button className="secondary" disabled={loading || offset + library.limit >= library.matching} onClick={() => setOffset(offset + library.limit)}>下一页</button></div>}
    </section>
  </>
}
