export type RetryState = {
  limit: number
  attempts_started: number
  attempts_completed?: number
  retries_started: number
  next_retry_at?: string | null
  action?: 'generate' | 'recover' | 'inspect'
  stop_reason?: string | null
  history?: { attempt: number; status: string; error: string | null; finished_at: string; account_id?: string }[]
}

export function RetryOptions({ enabled, switchAccounts, onEnabled, onSwitchAccounts }: {
  enabled: boolean; switchAccounts: boolean; onEnabled: (value: boolean) => void; onSwitchAccounts: (value: boolean) => void
}) {
  return <div className="retry-options">
    <label className="media-rights-consent"><input type="checkbox" checked={enabled} onChange={e => onEnabled(e.target.checked)} /> 失败后自动重试，最多3次（连同首次最多4次）</label>
    {enabled && <>
      <label className="media-rights-consent"><input type="checkbox" checked={switchAccounts} onChange={e => onSwitchAccounts(e.target.checked)} /> 每次重试换一个未尝试的可用账号</label>
      <p className="helper">首次之外最多重试3次，间隔10、30、60秒。账号不够时提前停止。参数保持一致；已提交且结果不明时只查询原账号原项目，有结果编号则恢复下载，不自动重复生成。权限或人工验证问题停止重试。等待期间可取消。</p>
    </>}
  </div>
}

export function RetryProgress({ state, status }: { state?: RetryState; status: string }) {
  if (!state) return null
  return <div className="retry-progress">
    <small>自动重试 {state.retries_started}/{state.limit} · 已开始 {state.attempts_started} 次尝试（含只读查询）</small>
    {status === 'queued' && state.next_retry_at && <small>{state.action === 'recover' ? '等待恢复原结果' : state.action === 'inspect' ? '等待读取原项目（不生成）' : '等待自动重试'} · {new Date(state.next_retry_at).toLocaleTimeString('zh-CN', { hour12: false })}</small>}
    {status === 'failed' && state.stop_reason && <small>{state.stop_reason}</small>}
    {!!state.history?.length && <details><summary>尝试记录</summary>{state.history.map(h => <small key={h.attempt}>第{h.attempt}次：{h.status === 'succeeded' ? '成功' : '失败'}{h.account_id ? ` · ${h.account_id}` : ''}{h.error ? ` · ${h.error}` : ''}</small>)}</details>}
  </div>
}
