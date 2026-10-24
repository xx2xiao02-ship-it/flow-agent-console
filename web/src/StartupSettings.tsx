import { useEffect, useState } from 'react'
export default function StartupSettings() {
  const [state, setState] = useState<{enabled: boolean; supported: boolean} | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { fetch('/console/api/desktop/startup').then(async r => {
    if (!r.ok) throw new Error('无法读取开机启动设置')
    setState(await r.json())
  }).catch(e => setError(e.message)) }, [])
  async function update(enabled: boolean) {
    setBusy(true); setError('')
    try {
      const r = await fetch('/console/api/desktop/startup', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled})})
      const body = await r.json()
      if (!r.ok) throw new Error(body.detail || '保存失败')
      setState(body)
    } catch(e) {setError(e instanceof Error ? e.message : '保存失败')}
    finally {setBusy(false)}
  }
  return <div className="startup-setting"><label title={state?.supported ? '登录 Windows 后静默启动到托盘，不打开窗口' : '请在安装后的 Windows 客户端中设置'}><input type="checkbox" checked={state?.enabled ?? false} disabled={!state?.supported || busy} onChange={e => void update(e.target.checked)} />开机启动</label>{error && <span role="alert">{error}</span>}</div>
}
