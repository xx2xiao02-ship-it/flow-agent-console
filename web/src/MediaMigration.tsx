import { useEffect, useRef, useState } from 'react'

type Plan = { id: string; target: string; sources: string[]; total: number; total_bytes: number; renamed: number; conflicts: { original: string; renamed: string }[] }
type Status = { state: string; pending: boolean; target?: string; total?: number; copied?: number; removed?: number; message?: string; committed?: boolean }
const size = (bytes: number) => bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`

export default function MediaMigration({ directory, disabled, onStatus }: { directory: string; disabled: boolean; onStatus: (pending: boolean, completedTarget?: string) => void }) {
  const [status, setStatus] = useState<Status>({ state: 'idle', pending: false })
  const [plan, setPlan] = useState<Plan | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const callback = useRef(onStatus)
  callback.current = onStatus
  const completed = useRef('')
  const trigger = useRef<HTMLButtonElement>(null)
  const modal = useRef<HTMLDivElement>(null)

  function apply(value: Status) {
    setStatus(value)
    const target = value.state === 'succeeded' && completed.current !== value.target ? value.target : undefined
    if (target) completed.current = target
    callback.current(value.pending, target)
  }
  useEffect(() => {
    let active = true
    async function refresh() {
      try {
        const response = await fetch('/console/api/media/storage/migration')
        if (!response.ok) throw new Error('迁移状态读取失败')
        const result = await response.json()
        if (active) apply(result)
      } catch { if (active) setError('迁移状态读取失败，请刷新页面后重试。') }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 2000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])
  useEffect(() => {
    if (!plan) return
    modal.current?.querySelector<HTMLButtonElement>('button')?.focus()
  }, [plan])

  async function request(action: string, body?: object) {
    const response = await fetch('/console/api/media/storage/migration/' + action, { method: 'POST', ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) })
    const value = await response.json()
    if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '操作失败，请检查目录和迁移状态。')
    return value
  }
  async function preview() {
    setBusy(true); setError(''); setConfirmed(false)
    try { setPlan(await request('plan', { directory: directory.trim() })) }
    catch (e) { setError(e instanceof Error ? e.message : '迁移预览失败') }
    finally { setBusy(false) }
  }
  async function execute(action: 'start' | 'resume' | 'abandon') {
    setBusy(true); setError('')
    try {
      apply(await request(action, action === 'start' ? { plan_id: plan?.id, confirmed } : undefined))
      setPlan(null)
    } catch (e) { setError(e instanceof Error ? e.message : '迁移操作失败') }
    finally { setBusy(false) }
  }
  function close() { if (!busy) { setPlan(null); trigger.current?.focus() } }

  return <div className="migration-section">
    <div className="migration-heading"><div><strong>整体迁移素材</strong><p>把已登记目录中的图片、视频和提示词索引一起转到上方目录，校验无误后清理原素材。</p></div><button ref={trigger} className="secondary" disabled={disabled || busy || status.pending || !directory.trim()} onClick={() => void preview()}>{busy && !plan ? '检查目录…' : '整体迁移到此目录'}</button></div>
    {status.state !== 'idle' && <div className="migration-progress" role="status"><strong>{status.state === 'running' ? '正在迁移' : status.state === 'succeeded' ? '迁移完成' : status.state === 'abandoned' ? '已保留两边文件' : '迁移需要处理'}</strong><span>已复制校验 {status.copied ?? 0}/{status.total ?? 0} · 已清理原文件 {status.removed ?? 0}/{status.total ?? 0}</span><p>{status.message}</p>{status.pending && status.state !== 'running' && <div className="library-actions"><button className="secondary" disabled={busy} onClick={() => void execute('resume')}>继续迁移</button><button className="text-link" disabled={busy} onClick={() => void execute('abandon')}>结束迁移，保留两边文件</button></div>}</div>}
    {error && <p className="danger-text storage-notice" role="alert">{error}</p>}
    {plan && <div className="modal-backdrop"><div ref={modal} className="modal migration-dialog" role="dialog" aria-modal="true" aria-labelledby="migration-title" onKeyDown={event => {
      if (event.key === 'Escape') close()
      if (event.key === 'Tab') {
        const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),[tabindex="0"]'))
        const first = controls[0], last = controls[controls.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }}><h3 id="migration-title">确认整体迁移素材</h3><p>待迁移 <strong>{plan.total} 个素材 · {size(plan.total_bytes)}</strong></p><dl><dt>来源目录</dt><dd>{plan.sources.map(source => <div key={source}>{source}</div>)}</dd><dt>目标目录</dt><dd>{plan.target}</dd></dl><p>逐个复制并校验内容后切换保存位置，再清理原文件。提示词索引一并迁移，旧任务下载继续可用。其他文件、账号和任务历史不移动。</p>{plan.renamed > 0 && <details><summary>有 {plan.renamed} 个同名文件，将自动改名保留两份</summary><ul>{plan.conflicts.map((file, index) => <li key={index}>{file.original} → {file.renamed}</li>)}</ul></details>}<p>请保持源盘和目标盘连接。中断后可继续迁移；复制校验前不会清理原素材。</p><label className="migration-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />我确认整体迁移，并同意校验后清理原目录中的已迁移素材</label>{error && <p role="alert" className="danger-text">{error}</p>}<div className="dialog-actions"><button className="secondary" disabled={busy} onClick={close}>取消</button><button className="primary" disabled={busy || !confirmed || plan.total === 0} onClick={() => void execute('start')}>{busy ? '正在启动…' : '确认并开始迁移'}</button></div></div></div>}
  </div>
}
