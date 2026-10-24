import { useEffect, useState } from 'react'

type AttentionItem = {
  account_id: string
  reason?: string
  at?: string
  job_id?: string
}

export default function ManualAttentionBanner() {
  const [items, setItems] = useState<AttentionItem[]>([])
  useEffect(() => {
    let alive = true
    async function load() {
      try {
        const response = await fetch('/console/api/accounts/manual-attention')
        if (!response.ok) return
        const data = await response.json() as { accounts: AttentionItem[] }
        if (alive) setItems(data.accounts ?? [])
      } catch { /* keep last snapshot */ }
    }
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [])
  if (!items.length) return null
  return <section className="panel" role="alert" aria-label="需要人工处理的账号"
    style={{ borderLeft: '4px solid #e11d48', marginBottom: 16 }}>
    <h3 style={{ marginTop: 0 }}>⚠ {items.length} 个账号需要人工处理（已暂停调度）</h3>
    <p className="helper">检测到登录或人工验证挑战的账号已立即从执行池撤下，其他账号不受影响。请点击“打开 Flow 处理”，在弹出的专用浏览器中本人完成登录或验证；后台自检通过后该账号自动恢复调度。</p>
    <ul style={{ margin: '8px 0', paddingLeft: 20 }}>
      {items.map(item => <li key={item.account_id} style={{ marginBottom: 6 }}>
        <strong>{item.account_id}</strong>{item.reason ? ` · ${item.reason}` : ''}
        {item.at ? ` · ${new Date(item.at).toLocaleString('zh-CN', { hour12: false })}` : ''}
        {' '}
        <button type="button" className="secondary"
          onClick={() => void fetch(`/console/api/accounts/${encodeURIComponent(item.account_id)}/open`, { method: 'POST' })}>
          打开 Flow 处理
        </button>
      </li>)}
    </ul>
  </section>
}
