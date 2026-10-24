import { useEffect, useRef, useState, type ReactNode } from 'react'

type Check = {
  account_id: string; state: string; message: string; checked_at: string | null
  next_check_at: string | null; credits: number | null; requires_user_action: boolean
}
type Report = {
  enabled: boolean; state: 'watching' | 'checking' | 'paused' | 'disabled'; message: string
  interval_seconds: number; last_cycle_at: string | null; accounts: Check[]
}
type Props = {
  controlsOnly?: boolean
  accounts: { id: string; label: string; email: string | null; can_open_browser?: boolean }[]
  children?: ReactNode
  onPrepare?: (id: string) => void
  blocked: boolean
  onRemove?: (id: string) => void
}
const stateNames: Record<string, string> = {
  checking: '正在后台检测', ready: '检测通过', incomplete: '等待网页同步', pending: '等待检测',
  stale: '等待更新', needs_login: '需要登录或验证', network_error: '网络查询失败',
  identity_error: '账号不一致', unread: '尚未读取', binding_missing: '需要关联浏览器', needs_attention: '需要处理',
}
const formatTime = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'

async function request(path = '', init?: RequestInit): Promise<Report> {
  const response = await fetch(`/console/api/accounts/monitor${path}`, init)
  const body = await response.json()
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '后台检测请求失败')
  return body
}

export default function AccountMonitor({ accounts, blocked, onRemove, onPrepare, children, controlsOnly = false }: Props) {
  const [report, setReport] = useState<Report | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const revision = useRef(0)
  useEffect(() => {
    let mounted = true
    let reading = false
    async function poll() {
      if (reading) return
      reading = true
      const version = revision.current
      try {
        const result = await request()
        if (mounted && version === revision.current) { setReport(result); setError('') }
      } catch (e) {
        if (mounted && version === revision.current) setError(e instanceof Error ? e.message : '无法读取后台检测状态')
      } finally { reading = false }
    }
    void poll()
    const timer = setInterval(() => void poll(), 2000)
    return () => { mounted = false; clearInterval(timer) }
  }, [])

  async function act(path: string, enabled?: boolean) {
    revision.current += 1
    setSending(true); setError('')
    try {
      setReport(await request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        ...(enabled === undefined ? {} : { body: JSON.stringify({ enabled }) }) }))
    } catch (e) { setError(e instanceof Error ? e.message : '后台检测请求失败') }
    finally { setSending(false) }
  }

  const checking = report?.state === 'checking'
  const unfinished = (report?.accounts || []).filter(check => {
    const account = accounts.find(a => a.id === check.account_id)
    return check.state === 'incomplete' && account && !account.email && !account.can_open_browser
  })
  const visibleChecks = (report?.accounts || []).filter(check => !unfinished.includes(check))
  const attention = visibleChecks.filter(check => check.requires_user_action || ['network_error', 'identity_error', 'needs_login', 'incomplete'].includes(check.state))
  const displayed = showAll ? visibleChecks : attention
  return <section className="panel account-monitor" aria-label="账号后台检测">
    <div className="monitor-heading"><div><h3>账号后台检测</h3><p>正常账号自动在后台维护，仅提醒需要处理的问题。</p></div>
      <div className="monitor-controls"><label><input type="checkbox" aria-label="后台自动检测" checked={report?.enabled ?? false} disabled={!report || sending} onChange={e => void act('/settings', e.target.checked)} />后台自动检测</label>
      <button type="button" className="primary" disabled={!report || sending || checking || blocked} onClick={() => void act('/check')}>{checking ? '正在后台检测…' : '立即后台检测全部账号'}</button></div></div>
    {report && !controlsOnly && <><div className="monitor-summary" role="status"><span>{checking ? '正在后台检测…' : report.state === 'paused' ? '任务运行中，检测暂缓' : attention.length ? `${attention.length} 个账号需要处理` : '当前没有需要处理的提醒'} · {visibleChecks.filter(a => a.state === 'ready').length} 个正常</span><button className="text-link" onClick={() => setShowAll(!showAll)}>{showAll ? '仅看待处理' : '查看全部账号状态'}</button></div>
      {displayed.length ? <div className="table-wrap"><table><thead><tr><th>账号</th><th>后台检测状态</th><th>积分 / 检测时间</th></tr></thead><tbody>{displayed.map(check => {
        const account = accounts.find(a => a.id === check.account_id)
        return <tr key={check.account_id}><td><strong>{account?.label || '待识别账号'}</strong><small>{account?.email || '尚未识别邮箱'}</small></td>
          <td><span className={`monitor-account-state ${check.state}`}>{stateNames[check.state] || check.state}</span><p>{check.state === 'incomplete' ? '网页已打开不代表同步完成。请保持对应账号的 Flow 页面开启；同步后程序会自动更新身份和积分。若一直不变，请使用“打开并准备所选账号”。' : check.message}</p></td>
          <td>{check.requires_user_action && account?.can_open_browser && onPrepare && <button className="secondary" disabled={blocked || sending} onClick={() => onPrepare(check.account_id)}>重新连接</button>}<b>{check.credits ?? '—'}</b><small>{formatTime(check.checked_at)}</small></td></tr>
      })}</tbody></table></div> : <></>}
      {unfinished.length > 0 && <details className="unfinished-connections"><summary>未完成的连接记录（{unfinished.length} 条，未计入已连接账号）</summary><p>这些记录尚未识别邮箱，也没有可打开的账号资料，不代表新增了 Google 账号。可删除此连接记录。</p>{unfinished.map(check => <div className="removed-account-row" key={check.account_id}><span>未识别账号 · 等待完成连接</span>{onRemove && <button className="secondary danger-text" disabled={blocked || sending || checking} onClick={() => onRemove(check.account_id)}>删除记录</button>}</div>)}</details>}</>}
    {error && <p className="prepare-attention" role="alert">{error}</p>}
    {children}
    <details className="monitor-help"><summary>检测说明</summary><p className="helper">每 5 秒检查本地会话变化，每 5 分钟核验身份和积分；生成期间暂缓，程序退出后停止。需要登录或人工验证时请本人完成，检测通过不代表生成风控已解除。</p></details>
  </section>
}
