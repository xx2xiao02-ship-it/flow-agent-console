import { useEffect, useState } from 'react'

export default function ImagePageCheck({ accountId, disabled }: { accountId: string; disabled: boolean }) {
  const [running, setRunning] = useState(false)
  const [message, setMessage] = useState('')
  const [activity, setActivity] = useState('')
  useEffect(() => {
    let mounted = true
    let pending = false
    async function poll() {
      if (pending) return
      pending = true
      try {
        const response = await fetch('/console/api/browser-activity')
        if (!response.ok) return
        const data = await response.json()
        if (mounted) setActivity(!data.active ? '' : data.visibility?.waiting_for_user
          ? '需要本人登录或人工验证：本次专用浏览器已显示，请在窗口中完成。超时或任务结束后自动关闭；不会重复提交生成。'
          : '专用浏览器正在后台隐藏运行，结束后自动关闭。')
      } catch { /* The regular console health view handles service disconnects. */ }
      finally { pending = false }
    }
    void poll()
    const timer = setInterval(() => void poll(), 1500)
    return () => { mounted = false; clearInterval(timer) }
  }, [])
  async function check() {
    setRunning(true)
    setMessage('正在后台隐藏检查图片页面；需要本人操作时显示，结束后自动关闭……')
    try {
      const response = await fetch(`/console/api/accounts/${encodeURIComponent(accountId)}/image-page-check`, { method: 'POST' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || '检查失败')
      setMessage(data.page_ready && data.browser_closed
        ? `账号、2.1 模型、比例与单张数量检查通过；${data.visibility?.hidden_confirmed ? '已确认后台隐藏运行；' : ''}未提交生成，本次浏览器已关闭。此检查不代表真实出图验收通过。`
        : '检查结束，页面就绪或浏览器关闭尚未确认。未提交生成。')
    } catch (error) { setMessage(error instanceof Error ? error.message : '检查失败') }
    finally { setRunning(false) }
  }
  return <section className="notice">
    <strong>2.1 图片页面检查</strong>
    <p>2.1 默认通过后台隐藏的真实浏览器运行，需要登录或人工验证时才显示，成功、失败或超时后自动关闭。先选择账号再检查；不会生成或重试任务。</p>
    <button type="button" className="secondary" disabled={disabled || running || !accountId} onClick={() => void check()}>
      {running ? '检查中……' : '检查图片页面（不生成）'}
    </button>
    {activity && <p role="status">{activity}</p>}
    {message && <p role="status">{message}</p>}
  </section>
}
