import { useCallback, useEffect, useState } from 'react'

type ProxyRow = {
  id: string
  proxy: string
  added_at: string
  last_ok_at: string | null
  last_error: string | null
}

type ProxySnapshot = {
  proxies: ProxyRow[]
  bindings: Record<string, string>
  accounts: string[]
}

export default function ProxySettings() {
  const [data, setData] = useState<ProxySnapshot | null>(null)
  const [value, setValue] = useState('')
  const [notice, setNotice] = useState('')
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [testing, setTesting] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const response = await fetch('/console/api/proxy')
      if (!response.ok) throw new Error('无法读取代理设置')
      setData(await response.json() as ProxySnapshot)
    } catch (e) {
      setFailed(true); setNotice(e instanceof Error ? e.message : '读取失败')
    }
  }, [])
  useEffect(() => { void load() }, [load])

  async function post(body: Record<string, unknown>, ok: string) {
    setBusy(true); setNotice(''); setFailed(false)
    try {
      const response = await fetch('/console/api/proxy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '操作失败')
      if (result.snapshot) setData(result.snapshot as ProxySnapshot); else void load()
      setNotice(ok)
    } catch (e) {
      setFailed(true); setNotice(e instanceof Error ? e.message : '操作失败，请重试')
    } finally { setBusy(false) }
  }

  async function test(id: string) {
    setTesting(id); setBusy(true); setNotice(''); setFailed(false)
    try {
      const response = await fetch('/console/api/proxy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'test', proxy_id: id }) })
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '测试失败')
      if (result.snapshot) setData(result.snapshot as ProxySnapshot)
      setNotice(result.ok ? `连通正常，延迟 ${result.latency_ms} ms。` : `连通失败：${result.error || '未知错误'}`)
      setFailed(!result.ok)
    } catch (e) {
      setFailed(true); setNotice(e instanceof Error ? e.message : '测试失败')
    } finally { setTesting(null); setBusy(false) }
  }

  const accountLabel = (key: string) => key
  return <section className="panel" aria-label="代理出口设置">
    <h3>代理出口（账号 ↔ IP 绑定）</h3>
    <p className="helper">每个账号固定使用一个专属代理出口，避免多个账号共用同一 IP 被平台关联。未绑定的账号会在首次任务时自动分配使用最少的代理。建议使用住宅/ISP 代理；凭据只保存在本机，页面不显示。留空则不使用代理，行为与从前一致。</p>
    <div style={{ display: 'flex', gap: 8, margin: '8px 0' }}>
      <input style={{ flex: 1 }} type="text" placeholder="http://用户:密码@主机:端口 或 socks5://主机:端口"
        value={value} disabled={busy} onChange={e => setValue(e.target.value)} />
      <button type="button" className="secondary" disabled={busy || value.trim() === ''}
        onClick={() => { void post({ action: 'add', value: value.trim() }, '代理已添加。').then(() => setValue('')) }}>添加</button>
    </div>
    {notice && <p className="helper" role="status" style={{ color: failed ? '#e11d48' : undefined }}>{notice}</p>}
    {data && data.proxies.length === 0 && <p className="helper">当前没有代理，全部流量走本机网络。</p>}
    {data && data.proxies.length > 0 && <table style={{ width: '100%', borderCollapse: 'collapse', marginBottom: 12 }}>
      <thead><tr><th style={{ textAlign: 'left' }}>代理出口</th><th style={{ textAlign: 'left' }}>最近测试</th><th></th></tr></thead>
      <tbody>
        {data.proxies.map(row => <tr key={row.id}>
          <td style={{ padding: '4px 8px 4px 0', fontFamily: 'monospace' }}>{row.proxy}</td>
          <td style={{ padding: '4px 8px' }}>
            {row.last_error ? <span style={{ color: '#e11d48' }}>失败 · {row.last_error}</span>
              : row.last_ok_at ? <span style={{ color: '#16a34a' }}>连通 · {new Date(row.last_ok_at).toLocaleString('zh-CN', { hour12: false })}</span>
                : '未测试'}
          </td>
          <td style={{ padding: '4px 0', whiteSpace: 'nowrap' }}>
            <button type="button" className="secondary" disabled={busy || testing === row.id} onClick={() => void test(row.id)}>
              {testing === row.id ? '测试中…' : '测试'}</button>{' '}
            <button type="button" className="secondary" disabled={busy}
              onClick={() => { void post({ action: 'remove', proxy_id: row.id }, '代理已删除，相关绑定已清除。') }}>删除</button>
          </td>
        </tr>)}
      </tbody>
    </table>}
    {data && Object.keys(data.bindings).length > 0 && <details>
      <summary>账号绑定（{Object.keys(data.bindings).length}）</summary>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <tbody>
          {Object.entries(data.bindings).map(([account, proxy]) => <tr key={account}>
            <td style={{ padding: '4px 8px 4px 0' }}>{accountLabel(account)}</td>
            <td style={{ padding: '4px 8px', fontFamily: 'monospace' }}>{proxy}</td>
            <td style={{ whiteSpace: 'nowrap' }}>
              <button type="button" className="secondary" disabled={busy || data.proxies.length < 2}
                title={data.proxies.length < 2 ? '至少需要两个代理才能更换' : undefined}
                onClick={() => { void post({ action: 'rebind', account_id: account }, '已为该账号更换代理出口。') }}>更换出口</button>{' '}
              <button type="button" className="secondary" disabled={busy}
                onClick={() => { void post({ action: 'unbind', account_id: account }, '已解除绑定；下次任务会自动重新分配。') }}>解绑</button>
            </td>
          </tr>)}
        </tbody>
      </table>
    </details>}
  </section>
}
