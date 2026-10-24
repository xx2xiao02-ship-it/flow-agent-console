import { useEffect, useState, type ReactNode } from 'react'
import CreditEstimator, { type Pricing, type VideoEstimates } from './CreditEstimator'
import './accounts.css'

type Account = { id: string; label: string; email: string | null; selectable: boolean; credits: number | null; video_estimates?: VideoEstimates; generation_blocked?: boolean; generation_block_reason?: string | null; read_error?: string | null; session_saved_at: string | null; credits_checked_at?: string | null }
type Check = { account_id: string | null; state: string; message?: string; checked_at?: string | null; requires_user_action?: boolean; browser_cleanup?: { message: string }; steps?: { id: string; label: string; state: string }[] }
type Monitor = { accounts: Check[] }
type Preparation = Check & { batch?: { accounts: Check[] } }
const stamp = (value?: string | null) => value ? Date.parse(value) || 0 : 0
const date = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '尚未检查'
const readErrorText: Record<string, string> = { network: '积分查询网络失败', session: '登录已失效，请打开 Flow 验证', refused: 'Flow 拒绝读取，请打开页面检查', unread: 'Flow 未返回可识别余额', upstream: 'Flow 服务暂未成功响应', incomplete: '会话信息不完整，请重新同步' }
const names: Record<string,string> = {ready:'身份检查通过',checked:'身份检查通过',checking:'检查中',running:'检查中',queued:'等待检查',pending:'等待检查',needs_login:'需要登录或验证',identity_error:'身份关联待核对',binding_missing:'需要关联浏览器',needs_attention:'需要处理',network_error:'网络检查失败',incomplete:'连接未完成',skipped:'尚未检查',stale:'等待更新',unread:'尚未读取'}

export function accountCheck(account: Account, checks: Check[]) {
  const candidates = checks.filter(c => c.account_id === account.id)
  const check = candidates.sort((a,b) => stamp(b.checked_at) - stamp(a.checked_at))[0]
  const passed = check?.state === 'ready' || check?.state === 'checked'
  const pending = !check || ['pending','queued','checking','running','stale'].includes(check.state)
  const readIssue = !!account.read_error && (!passed || stamp(account.credits_checked_at) > stamp(check.checked_at))
  const attention = !!account.generation_blocked || !account.selectable || readIssue || (!!check && !passed && !pending)
  const reason = account.generation_block_reason || (!passed && check?.message) || (readIssue ? (readErrorText[account.read_error!] || '账号信息未读取，请检查连接。') : !account.selectable ? '本地连接未完成，请重新连接或清理此记录。' : '')
  return { check, passed, attention, reason }
}

export default function UnifiedAccounts({accounts,pricing,actions}: { accounts: Account[]; pricing?: Pricing; actions: (a: Account, attention: boolean) => ReactNode }) {
  const [checks,setChecks] = useState<Check[]>([])
  const [error,setError] = useState('')
  useEffect(() => {
    let alive=true, reading=false
    async function poll() {
      if(reading) return
      reading=true
      try {
        const responses=await Promise.all(['/accounts/monitor','/accounts/preparation'].map(p=>fetch('/console/api'+p)))
        if(responses.some(r=>!r.ok)) throw new Error('检测状态暂时不可用，请刷新后重试。')
        const [monitor,preparation]=await Promise.all(responses.map(r=>r.json())) as [Monitor,Preparation]
        if(alive) {setChecks([...(monitor.accounts || []),...(preparation.batch?.accounts || []),...(preparation.account_id ? [preparation] : [])]);setError('')}
      } catch(e) {if(alive)setError(e instanceof Error ? e.message : '检测状态读取失败')}
      finally {reading=false}
    }
    void poll();const timer=setInterval(()=>void poll(),2000)
    return ()=>{alive=false;clearInterval(timer)}
  },[])
  const rows=accounts.map(a=>({a,...accountCheck(a,checks)})).sort((a,b)=>Number(b.attention)-Number(a.attention))
  const attention=rows.filter(r=>r.attention).length
  return <CreditEstimator accounts={accounts} pricing={pricing} renderAccounts={estimates=><div className="unified-accounts">
    <div className="panel-head"><div><h3>账号列表</h3><p>{accounts.length} 个账号 · {attention} 个需处理 · {rows.filter(r=>r.passed && !r.attention).length} 个检查正常 · 异常置顶</p></div><small>身份通过不代表生成一定成功</small></div>
    {error && <p role="alert" className="prepare-attention">{error}</p>}
    <div className="table-wrap"><table aria-label="统一账号列表"><thead><tr><th>账号</th><th>状态与检测</th><th>积分</th><th>预计可生成</th><th>操作</th></tr></thead><tbody>{rows.map(({a,check,passed,attention,reason})=>{
      const estimate=estimates.find(e=>e.id===a.id)
      return <tr key={a.id} data-account-id={a.id} data-attention={attention}>
        <td><strong>{a.label || '未完成连接的账号'}</strong><small>{a.email || '尚未识别邮箱'}</small></td>
        <td><span className={`status ${attention ? 'interrupted' : passed ? 'succeeded' : 'queued'}`}>{names[check?.state || 'pending'] || '待核对'}</span><small>{a.generation_blocked ? '生成调度：本地暂停' : a.selectable && passed && !attention ? '生成调度：可选' : '生成调度：待核验'}</small>
          {reason && <p className="account-issue">{reason}</p>}
          <details><summary>检测详情</summary><small>检测时间：{date(check?.checked_at)}</small><small>会话保存：{date(a.session_saved_at)}</small><small>积分读取：{date(a.credits_checked_at)}</small>{check?.message && check.message !== reason && <p>{check.message}</p>}{check?.browser_cleanup && !check.message?.includes(check.browser_cleanup.message) && <small>{check.browser_cleanup.message}</small>}{check?.steps?.map(s=><small key={s.id}>{s.label} · {s.state}</small>)}</details>
        </td><td><b>{a.credits ?? '未读取'}</b></td><td><small>Omni：{estimate?.omni == null ? '—' : `约 ${estimate.omni} 条`}</small><small>Veo：{estimate?.veo == null ? '—' : `约 ${estimate.veo} 条`}</small></td><td><div className="unified-account-actions">{actions(a,attention)}</div></td>
      </tr>
    })}</tbody></table></div>{!rows.length && <p className="helper">尚未连接账号，请使用下方新增账号功能。</p>}
  </div>} />
}
