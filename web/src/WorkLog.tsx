import { useState } from 'react'
type Log = { events: { time: string; event: string; data: Record<string, unknown>; stack?: unknown }[]; screenshot: boolean; privacy: string }
export default function WorkLog({ id }: { id: string }) {
  const [log, setLog] = useState<Log | null>(null)
  const [opened, setOpened] = useState(false)
  const [error, setError] = useState('')
  async function show() {
    setOpened(true); setError('')
    try { const r = await fetch(`/console/api/jobs/${id}/work-log`); if (!r.ok) throw Error('日志读取失败'); setLog(await r.json()) }
    catch { setError('日志读取失败，请稍后刷新。') }
  }
  return <><button className="text-link" onClick={() => void show()}>查看日志</button>
    <a className="text-link" href={`/console/api/jobs/${id}/work-log/export`} download>导出诊断</a>
    {opened && <div role="dialog" aria-modal="true" aria-label="任务工作日志" style={{position:'fixed',inset:'8% 8%',zIndex:1000,background:'#171c27',border:'1px solid #666',borderRadius:16,padding:24,overflow:'auto',boxShadow:'0 0 0 100vmax #0009',textAlign:'left'}}>
      <button className="secondary" style={{float:'right'}} onClick={() => setOpened(false)}>关闭日志</button>
      <h3>任务工作日志</h3><p>{id}</p><button className="secondary" onClick={() => void show()}>刷新日志</button> <a href={`/console/api/jobs/${id}/work-log/export`} download>下载诊断 ZIP</a>
      <p>{error || log?.privacy || '正在读取…'}</p>
      {log?.events.map((e,i) => <section key={i} style={{borderTop:'1px solid #394052',padding:'12px 0'}}><strong>{e.event}</strong> · {new Date(e.time).toLocaleString('zh-CN')}<pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(e.data,null,2)}</pre>{Boolean(e.stack) && <details><summary>调用位置（已去除原始消息）</summary><pre>{JSON.stringify(e.stack,null,2)}</pre></details>}</section>)}
      {log?.screenshot && <img alt="失败提示区域截图" src={`/console/api/jobs/${id}/work-log/screenshot`} style={{maxWidth:'100%'}}/>}
    </div>}
  </>
}

