import { useState, type ReactNode } from 'react'

export type VideoEstimates = {
  omni_flash: Record<string, Record<string, number | null>>
  veo_3_1: Record<string, Record<string, number | null>>
}
export type Pricing = {
  source_url: string; checked_on: string
  omni_flash: Record<string, Record<string, number>>
  veo_3_1: Record<string, Record<string, number>>
}
type Wallet = { id: string; label: string; email: string | null; selectable: boolean; credits: number | null; video_estimates?: VideoEstimates }

export default function CreditEstimator({ accounts, pricing, duration, quality, compact = false, renderAccounts }: {
  accounts: Wallet[]; pricing?: Pricing; duration?: string; quality?: string; compact?: boolean
  renderAccounts?: (rows: { id: string; omni: number | null; veo: number | null }[]) => ReactNode
}) {
  const [seconds, setSeconds] = useState('8s')
  const [resolution, setResolution] = useState('720p')
  const [veo, setVeo] = useState('fast')
  const [tier, setTier] = useState('non_ultra')
  const omniDuration = duration || seconds
  const omniQuality = quality || resolution
  const wallets = accounts.filter(a => a.selectable)
  const rows = wallets.map(a => ({ ...a,
    omni: a.video_estimates?.omni_flash[omniDuration]?.[omniQuality] ?? null,
    veo: a.video_estimates?.veo_3_1[veo]?.[tier] ?? null,
  }))
  const known = rows.filter(a => a.credits !== null)
  const total = (key: 'omni' | 'veo') => known.length ? known.reduce((sum, a) => sum + (a[key] ?? 0), 0) : '—'
  const title = `Veo 3.1 ${veo === 'quality' ? 'Quality' : veo === 'lite' ? 'Lite' : 'Fast'} · 8s`
  const controls = <>
      {!duration && <div className="form-grid"><label>Omni 时长<select aria-label="换算 Omni 时长" value={seconds} onChange={e => setSeconds(e.target.value)}>{Object.keys(pricing?.omni_flash || {}).map(s => <option key={s}>{s}</option>)}</select></label><label>Omni 画质<select aria-label="换算 Omni 画质" value={resolution} onChange={e => setResolution(e.target.value)}><option>720p</option><option>360p</option></select></label></div>}
      <div className="form-grid"><label>Veo 换算档位<select aria-label="Veo 换算档位" value={veo} onChange={e => setVeo(e.target.value)}><option value="lite">Veo 3.1 Lite</option><option value="fast">Veo 3.1 Fast</option><option value="quality">Veo 3.1 Quality</option></select></label><label>套餐换算场景<select aria-label="套餐换算场景" value={tier} onChange={e => setTier(e.target.value)}><option value="non_ultra">非 Ultra</option><option value="ultra">Ultra</option></select></label></div>
  </>
  return <section className={`panel credit-estimator ${compact ? 'compact-estimator' : ''} ${renderAccounts ? 'unified-estimator' : ''}`}>
    <div className="panel-head"><div><h3>积分可生成多少条？</h3><p>同一余额分别换算为 Omni 和 Veo，不能相加。</p></div></div>
    {!pricing ? <p className="helper">正在读取换算规则…</p> : <>
      {renderAccounts ? <details className="estimate-options"><summary>调整换算参数 · {omniDuration} / {omniQuality} · {title}</summary>{controls}</details> : controls}
      <div className="estimate-totals"><div><small>Omni Flash · {omniDuration} / {omniQuality}</small><strong data-testid="omni-total">约 {total('omni')} 条</strong><small>{pricing.omni_flash[omniDuration][omniQuality]} 积分 / 条</small></div><div><small>{title}</small><strong data-testid="veo-total">约 {total('veo')} 条</strong><small>{pricing.veo_3_1[veo][tier]} 积分 / 条 · 仅供换算</small></div></div>
      {!compact && !renderAccounts && <div className="table-wrap"><table><thead><tr><th>账号</th><th>缓存积分</th><th>Omni 预计条数</th><th>Veo 预计条数</th></tr></thead><tbody>{rows.map(a => <tr key={a.id}><td>{a.label}<br/><small>{a.email}</small></td><td>{a.credits ?? '未读取'}</td><td>{a.omni === null ? '—' : `约 ${a.omni} 条`}</td><td>{a.veo === null ? '—' : `约 ${a.veo} 条`}</td></tr>)}</tbody></table></div>}
      <details className="estimate-notes"><summary>换算说明与来源</summary>
      <p className="helper">逐账号向下取整后合计；余额未知不计入。套餐未自动识别，场景仅用于估算。Veo 可在创建任务中选择，当前网页固定 8 秒、720p。未计入升频、编辑或 Ultra 低优先级零积分模式。</p>
      <p className="helper">规则核对：{pricing.checked_on} · <a href={pricing.source_url} target="_blank" rel="noreferrer">Google 官方扣分说明 ↗</a>；实际以 Flow 提交时为准。</p>      </details>
    </>}
    {renderAccounts?.(rows)}
  </section>
}

