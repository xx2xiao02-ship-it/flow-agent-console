export type StartupState = { state: 'checking' | 'ready' | 'blocked'; ready: boolean; message: string; completed?: number; total?: number; local_profiles?: {id: string; label: string; emails: string[]}[]; checks: { id: string; label: string; state: string; message: string }[] }
export default function StartupCheck({ status }: { status: StartupState | null }) {
  const state = status?.state || 'checking'
  const total = status?.total || 1
  const completed = status?.completed || 0
  const current = status?.checks.find(item => item.state === 'checking')
  return <div className="startup-screen"><section className={`startup-check ${state}`} aria-live="polite" aria-label="启动自查">
    <span className="eyebrow">FLOW AGENT</span>
    <h1>{state === 'blocked' ? '启动需要处理' : '正在准备工作空间'}</h1>
    <p>{state === 'blocked' ? status?.message : '正在检查运行环境、账号关联、会话与积分。完成后自动进入。'}</p>
    <progress aria-label="启动自查进度" max={total} value={status ? completed : undefined}/>
    <div className="startup-progress-label"><strong>{current?.label || (state === 'blocked' ? '检查未通过' : '正在连接本机服务…')}</strong><span>{status ? `${completed} / ${total} 项` : '等待连接'}</span></div>
    <details open={state === 'blocked'}><summary>查看检查详情</summary>
      <ul>{status?.checks.map(item => <li key={item.id}><b>{item.label}</b><span>{item.state === 'passed' ? '通过' : item.state === 'checking' ? '检查中…' : item.message}</span></li>)}</ul>
    </details>
    <p>检查期间操作暂不可用，不提交生成、不消耗积分。账号待处理事项将在账号管理中集中显示。</p>
  </section></div>
}
