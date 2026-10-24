import { useState } from 'react'
import { videoModelLabel } from './videoModels'

export default function VideoPageCheck({ accountId, disabled, aspect, duration, quality, count, videoModel }: {
  accountId: string; disabled: boolean; aspect: string; duration: string; quality: string; count: number; videoModel: string
}) {
  const [running, setRunning] = useState(false)
  const [message, setMessage] = useState('')
  async function check() {
    setRunning(true)
    setMessage('正在后台隐藏检查官方视频页面；不会点击生成，不消耗积分。需要登录或人工验证时才显示窗口。')
    try {
      const response = await fetch(`/console/api/accounts/${encodeURIComponent(accountId)}/video-page-check`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ aspect, duration, quality, count, video_model: videoModel }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '视频页面检查失败')
      setMessage(data.page_ready && data.browser_closed && data.generation_requests === 0
        ? `账号、${videoModelLabel(videoModel)}、比例、时长、画质、数量及生成按钮就绪检查通过；未提交生成，专用浏览器已关闭。真实视频出片和下载仍需单独验收。`
        : '检查结束，页面就绪或浏览器关闭尚未确认。未提交生成。')
    } catch (error) { setMessage(error instanceof Error ? error.message : '视频页面检查失败') }
    finally { setRunning(false) }
  }
  return <section className="notice">
    <strong>官方视频页面检查</strong>
    <p>本次检查模型：{videoModelLabel(videoModel)}（在“创建任务”中选择）</p>
    <p>视频通过后台隐藏的官方网页操作；提交一次后等待本次结果并下载原始 MP4，成功、失败或超时后关闭本次专用浏览器。先选择账号再检查。</p>
    <button type="button" className="secondary" disabled={disabled || running || !accountId} onClick={() => void check()}>
      {running ? '检查中……' : '检查视频页面（不生成）'}
    </button>
    {message && <p role="status">{message}</p>}
  </section>
}
