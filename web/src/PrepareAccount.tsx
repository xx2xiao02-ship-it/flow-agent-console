import { useEffect, useRef, useState } from 'react'
import AccountMonitor from './AccountMonitor'

type PreparationResult = {
  state: 'idle' | 'running' | 'checked' | 'needs_attention'
  account_id: string | null
  checked_at?: string | null
  credits?: number
  message?: string
  browser_cleanup?: { state: string; message: string }
  steps: { id: string; label: string; state: 'pending' | 'running' | 'passed' | 'blocked' }[]
}
type BatchAccount = Omit<PreparationResult, 'state'> & {
  state: 'queued' | 'running' | 'checked' | 'needs_attention' | 'skipped'
  label: string
  email: string | null
}
type Preparation = PreparationResult & {
  auto_close_pages?: boolean
  last_execution_cleanup?: { account_id: string; operation?: string; checked_at: string; cleanup: { state: string; message: string } }
  scope?: 'single' | 'all'
  batch?: {
    total: number; completed: number; succeeded: number; attention: number; skipped: number
    stop_requested: boolean; accounts: BatchAccount[]
  }
}
type Props = {
  accounts: { id: string; label: string; email: string | null; can_open_browser: boolean }[]
  accountId: string
  onAccount: (id: string) => void
  onRunning: (running: boolean) => void
  activeJobs: number
  onRemove?: (id: string) => void
}
const symbols = { pending: '○', running: '◌', passed: '✓', blocked: '!' }


async function request(path: string, init?: RequestInit): Promise<Preparation> {
  const response = await fetch(`/console/api/accounts/${path}`, init)
  const body = await response.json()
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '账号准备请求失败')
  return body
}

export default function PrepareAccount({ accounts, accountId, onAccount, onRunning, activeJobs, onRemove }: Props) {
  const [report, setReport] = useState<Preparation>({ state: 'idle', account_id: null, steps: [] })
  const [headless, setHeadless] = useState(false)
  const [savingHeadless, setSavingHeadless] = useState(false)
  useEffect(() => { fetch('/console/api/accounts/verification/settings').then(r => r.json()).then(b => setHeadless(b.headless ?? false)).catch(() => setError('无法读取验证方式设置')) }, [])
  const [sending, setSending] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [error, setError] = useState('')
  const [savingPages, setSavingPages] = useState(false)
  const [checkingBrowser, setCheckingBrowser] = useState(false)
  const [browserResult, setBrowserResult] = useState('')
  const revision = useRef(0)
  const running = sending || checkingBrowser || report.state === 'running'
  useEffect(() => { if (activeJobs > 0) setBrowserResult('') }, [activeJobs])
  useEffect(() => { onRunning(running); return () => onRunning(false) }, [running, onRunning])
  useEffect(() => {
    let mounted = true
    let reading = false
    async function poll() {
      if (reading) return
      reading = true
      const version = revision.current
      try {
        const result = await request('preparation')
        if (mounted && version === revision.current) { setReport(result); setError('') }
      } catch (e) {
        if (mounted && version === revision.current) setError(e instanceof Error ? e.message : '无法读取准备状态')
      } finally { reading = false }
    }
    void poll()
    const timer = setInterval(() => void poll(), 2000)
    return () => { mounted = false; clearInterval(timer) }
  }, [])

  async function prepare(scope: 'single' | 'all', selectedId = accountId) {
    revision.current += 1
    setSending(true); setError('')
    try {
      const result = await request(scope === 'all' ? 'prepare-all' : 'prepare', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ account_id: selectedId || null }) })
      setReport(result)
      if (scope === 'single' && result.account_id) onAccount(result.account_id)
    } catch (e) { setError(e instanceof Error ? e.message : '账号准备失败') }
    finally { setSending(false) }
  }
  async function stopAfterCurrent() {
    revision.current += 1
    setStopping(true); setError('')
    try { setReport(await request('preparation/stop', { method: 'POST' })) }
    catch (e) { setError(e instanceof Error ? e.message : '停止后续检查失败') }
    finally { setStopping(false) }
  }
  async function checkBrowser() {
    const selected = accountId || accounts.find(a => a.can_open_browser)?.id
    if (!selected) return
    setCheckingBrowser(true); setBrowserResult('')
    try {
      const response = await fetch(`/console/api/accounts/${encodeURIComponent(selected)}/browser-check`, { method: 'POST' })
      const body = await response.json()
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '浏览器验证检查失败')
      const cleanup = typeof body.browser_cleanup?.message === 'string' ? ` ${body.browser_cleanup.message}` : ''
      setBrowserResult(body.message ? body.message + cleanup : '已连接所选账号的真实浏览器，Flow 验证组件已加载。未获取生成令牌、未提交生成、不消耗积分；这仍不代表 Google 已接受生成验证。' + cleanup)
    } catch (e) { setBrowserResult(e instanceof Error ? e.message : '浏览器验证检查失败') }
    finally { setCheckingBrowser(false) }
  }
  const selected = accounts.find(a => a.id === report.account_id)
  const canPrepare = accounts.some(a => a.can_open_browser)
  const batch = report.batch
  const cleanupRecord = report.last_execution_cleanup
  const cleanupAccount = accounts.find(a => a.id === cleanupRecord?.account_id)
  return <><AccountMonitor controlsOnly accounts={accounts} blocked={running || activeJobs > 0} onRemove={onRemove} onPrepare={id => { onAccount(id); void prepare('single', id) }}><details className="prepare-account account-connect-tools" open={running || !!error || undefined}><summary>连接与验证工具</summary>
    <label className="helper"><input type="checkbox" checked={headless} disabled={running || savingHeadless} onChange={async e => {
      const enabled = e.target.checked
      setSavingHeadless(true)
      try {
        const response = await fetch('/console/api/accounts/verification/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled})})
        const body = await response.json()
        if (!response.ok) throw new Error(body.detail || '保存失败')
        setHeadless(body.headless); setError('')
      } catch(e) {setError(e instanceof Error ? e.message : '保存失败')}
      finally {setSavingHeadless(false)}
    }} />默认使用无头浏览器验证 · 用完自动关闭</label>
    <div className="prepare-heading"><div><h3>账号连接</h3><p>自动验证默认打开可见的专用浏览器，检查后自动关闭。也可勾选无头模式；需要登录或人工验证时，请在账号列表手动打开 Flow。</p></div>
      <div className="prepare-actions"><label>准备账号<select aria-label="准备账号" value={accountId} disabled={running} onChange={e => onAccount(e.target.value)}><option value="">自动选择可打开的账号</option>{accounts.filter(a => a.can_open_browser).map(a => <option key={a.id} value={a.id}>{a.label}{a.email ? ` · ${a.email}` : ''}</option>)}</select></label>
      <button type="button" className="secondary" disabled={running || activeJobs > 0 || !canPrepare} onClick={() => void prepare('single')}>{running && report.scope !== 'all' ? '正在准备…' : headless ? '无头检查所选账号' : '打开并准备所选账号'}</button>
      <button type="button" className="secondary" disabled={running || activeJobs > 0 || !canPrepare} onClick={() => void prepare('all')}>{running && report.scope === 'all' ? '批量准备中…' : headless ? '无头检查全部账号' : '打开并准备全部账号'}</button>
      <button type="button" className="secondary" disabled={running || activeJobs > 0 || !canPrepare} onClick={() => void checkBrowser()}>{checkingBrowser ? '检查浏览器验证中…' : '检查浏览器验证（不生成）'}</button></div></div>
    {browserResult && <p className="helper" role="status">本次只读检查结果（非出图结果）：{browserResult}</p>}
    {cleanupRecord && cleanupRecord.cleanup.state !== 'closed' && <p className="helper" role="status">最近一次浏览器操作 · {cleanupRecord.operation === 'browser-check' ? '只读检查' : cleanupRecord.operation === 'image' ? '图片生成' : '任务'} · {cleanupAccount?.label || '所选账号'} · {new Date(cleanupRecord.checked_at).toLocaleString('zh-CN', { hour12: false })}：{cleanupRecord.cleanup.message}</p>}
    <label className="helper"><input type="checkbox" checked={headless || (report.auto_close_pages ?? true)} disabled={headless || savingPages || running} onChange={async e => {
      const enabled = e.target.checked
      setSavingPages(true)
      try {
        const response = await fetch('/console/api/accounts/preparation/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) })
        if (!response.ok) throw new Error('保存自动关页设置失败')
        setReport(current => ({ ...current, auto_close_pages: enabled })); setError('')
      } catch (e) { setError(e instanceof Error ? e.message : '保存设置失败') }
      finally { setSavingPages(false) }
    }} /> 账号准备完成后，自动关闭本次打开的页面</label>
    <p className="helper">自动生成任务默认使用可见的专用浏览器窗口（桌面下层运行，不抢焦点），需要本人登录或人工验证时置前显示，成功、失败或超时后自动关闭；可在并发设置里改回后台隐藏。手动“打开 Flow”显示窗口，由本人检查后关闭。</p>
    {(running || report.state === 'needs_attention') && <div className="prepare-result" role="status" aria-live="polite">
      {batch && <div className="prepare-batch-head"><strong>全部账号：已处理 {batch.completed} / {batch.total} · {batch.succeeded} 个通过 · {batch.attention} 个需处理 · {batch.skipped} 个未检查</strong>
        {running && <button type="button" className="secondary" disabled={stopping || batch.stop_requested} onClick={() => void stopAfterCurrent()}>{batch.stop_requested ? '当前检查结束后停止' : '停止后续检查'}</button>}</div>}
      {(!batch || running && report.account_id) && <><strong>{batch ? '当前检查：' : ''}{selected ? `${selected.label}${selected.email ? ` · ${selected.email}` : ''}` : '所选账号'}</strong>
      <ol className="prepare-steps">{report.steps.map(step => <li key={step.id} className={step.state}><span aria-hidden="true">{symbols[step.state]}</span>{step.label}<small>{step.state === 'passed' ? '已确认' : step.state === 'running' ? '检查中' : step.state === 'blocked' ? '需要处理' : '等待'}</small></li>)}</ol></>}
      <p className={report.state === 'needs_attention' ? 'prepare-attention' : ''}>{report.message}</p>
      {!batch && report.browser_cleanup && <p>{report.browser_cleanup.message}</p>}
      {!batch && report.state === 'checked' && <p>最新积分：<b>{report.credits}</b> · 检查于 {report.checked_at ? new Date(report.checked_at).toLocaleString('zh-CN', { hour12: false }) : '—'}</p>}

    </div>}
    {activeJobs > 0 && <p className="helper">有任务进行中，结束后可准备账号。</p>}
    {error && <p className="prepare-attention" role="alert">{error}</p>}
    <p className="helper">若提示需要登录或人工验证，请手动点击账号列表中的“打开 Flow”完成。检查通过不代表 Google 已解除生成风控。</p>
  </details></AccountMonitor></>
}
