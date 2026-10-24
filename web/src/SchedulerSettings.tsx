import { useEffect, useRef, useState } from 'react'
import './scheduler.css'

type Settings = {
  max_concurrency: number; min: number; max: number; running: number; queued: number
  min_job_interval_seconds: number; min_job_interval_min: number; min_job_interval_max: number
}

type GenerationSettings = { generation_browser_visible: boolean; isolated_desktop: boolean }

export default function SchedulerSettings() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [value, setValue] = useState('')
  const [intervalValue, setIntervalValue] = useState('')
  const [generationVisible, setGenerationVisible] = useState(true)
  const [isolatedDesktop, setIsolatedDesktop] = useState(true)
  const [dirty, setDirty] = useState(false)
  const [intervalDirty, setIntervalDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState('')
  const [failed, setFailed] = useState(false)
  const revision = useRef(0)
  const savingRef = useRef(false)
  useEffect(() => {
    let alive = true
    async function load() {
      if (savingRef.current) return
      const requestedRevision = revision.current
      try {
        const response = await fetch('/console/api/scheduler')
        if (!response.ok) throw new Error('无法读取并发设置')
        const data = await response.json() as Settings
        if (alive && requestedRevision === revision.current) setSettings(data)
      } catch { if (alive && requestedRevision === revision.current) { setFailed(true); setNotice('无法读取并发设置，请确认本地服务正在运行。') } }
    }
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [])
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const response = await fetch('/console/api/generation/settings')
        if (!response.ok) throw new Error()
        const data = await response.json() as GenerationSettings
        if (alive) { setGenerationVisible(data.generation_browser_visible); setIsolatedDesktop(data.isolated_desktop) }
      } catch { /* keep default */ }
    })()
    return () => { alive = false }
  }, [])
  useEffect(() => { if (settings && !dirty) setValue(String(settings.max_concurrency)) }, [settings, dirty])
  useEffect(() => { if (settings && !intervalDirty) setIntervalValue(String(settings.min_job_interval_seconds ?? 0)) }, [settings, intervalDirty])
  const maximum = settings?.max ?? 16
  const minimum = settings?.min ?? 1
  const intervalMin = settings?.min_job_interval_min ?? 0
  const intervalMax = settings?.min_job_interval_max ?? 600
  const valid = value.trim() !== '' && Number.isInteger(Number(value)) && Number(value) >= minimum && Number(value) <= maximum
  const changed = dirty && Number(value) !== settings?.max_concurrency
  const intervalValid = intervalValue.trim() !== '' && Number.isInteger(Number(intervalValue)) && Number(intervalValue) >= intervalMin && Number(intervalValue) <= intervalMax
  const intervalChanged = intervalDirty && Number(intervalValue) !== settings?.min_job_interval_seconds
  function edit(next: string) { setValue(next); setDirty(true); setNotice(''); setFailed(false) }
  function editInterval(next: string) { setIntervalValue(next); setIntervalDirty(true); setNotice(''); setFailed(false) }
  async function save(next = Number(value), nextInterval = Number(intervalValue)) {
    if (!settings || savingRef.current || !Number.isInteger(next) || next < minimum || next > maximum) return
    if (!Number.isInteger(nextInterval) || nextInterval < intervalMin || nextInterval > intervalMax) return
    revision.current += 1
    savingRef.current = true
    setSaving(true); setNotice(''); setFailed(false)
    try {
      const response = await fetch('/console/api/scheduler', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ max_concurrency: next, min_job_interval_seconds: nextInterval }) })
      const data = await response.json()
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '并发设置保存失败')
      setSettings(data as Settings); setValue(String(data.max_concurrency)); setDirty(false)
      setIntervalValue(String(data.min_job_interval_seconds)); setIntervalDirty(false)
      setNotice(`已生效：最多同时运行 ${data.max_concurrency} 个任务，任务间隔至少 ${data.min_job_interval_seconds} 秒，重启后保留。`)
    } catch (e) { setFailed(true); setNotice(e instanceof Error ? e.message : '保存失败，请重试') }
    finally { savingRef.current = false; setSaving(false) }
  }
  async function saveGenerationSettings(patch: { visible?: boolean; isolated?: boolean }) {
    if (patch.visible !== undefined) setGenerationVisible(patch.visible)
    if (patch.isolated !== undefined) setIsolatedDesktop(patch.isolated)
    try {
      const response = await fetch('/console/api/generation/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) })
      if (!response.ok) throw new Error('保存失败，请重试')
      setNotice(patch.visible === false ? '生成任务恢复后台隐藏窗口。'
        : patch.isolated === false ? '生成任务将显示在当前桌面的下层窗口。'
        : patch.isolated ? '生成任务将在独立桌面运行，当前桌面完全不受打扰。'
        : '生成任务将使用可见的专用浏览器窗口。')
      setFailed(false)
    } catch (e) { setFailed(true); setNotice(e instanceof Error ? e.message : '保存失败，请重试') }
  }
  return <section className="panel scheduler-panel" aria-label="生成并发设置">
    <div className="scheduler-heading"><div><h3>生成并发</h3><p>按空闲账号自动分配任务，本机与局域网共用。</p></div><span className="scheduler-default">默认最大 · {maximum}</span></div>
    <div className="scheduler-body">
      <div className="scheduler-editor">
        <label className="scheduler-label">最大并发数<span>个任务同时运行</span></label>
        <div className="scheduler-controls">
          <div className="scheduler-stepper">
            <button type="button" aria-label="减少并发数" disabled={!settings || saving || !valid || Number(value) <= minimum} onClick={() => edit(String(Number(value) - 1))}>−</button>
            <input aria-label="最大并发数" aria-describedby="scheduler-help" aria-invalid={dirty && !valid} type="number" min={minimum} max={maximum} step="1" placeholder="—" value={value} disabled={!settings || saving} onChange={e => edit(e.target.value)} />
            <button type="button" aria-label="增加并发数" disabled={!settings || saving || !valid || Number(value) >= maximum} onClick={() => edit(String(Number(value) + 1))}>＋</button>
          </div>
          <label className="scheduler-label">任务最小间隔<span>秒，0 为不限制</span></label>
          <div className="scheduler-stepper">
            <button type="button" aria-label="减少任务间隔" disabled={!settings || saving || !intervalValid || Number(intervalValue) <= intervalMin} onClick={() => editInterval(String(Number(intervalValue) - 1))}>−</button>
            <input aria-label="任务最小间隔秒数" aria-describedby="scheduler-help" aria-invalid={intervalDirty && !intervalValid} type="number" min={intervalMin} max={intervalMax} step="1" placeholder="0" value={intervalValue} disabled={!settings || saving} onChange={e => editInterval(e.target.value)} />
            <button type="button" aria-label="增加任务间隔" disabled={!settings || saving || !intervalValid || Number(intervalValue) >= intervalMax} onClick={() => editInterval(String(Number(intervalValue) + 1))}>＋</button>
          </div>
          <button className="scheduler-save" type="button" disabled={!settings || saving || !valid || !intervalValid || (!changed && !intervalChanged)} onClick={() => void save()}>{saving ? '保存中…' : '保存设置'}</button>
          <button className="scheduler-max" type="button" disabled={!settings || saving || (settings.max_concurrency === maximum && !changed)} onClick={() => void save(maximum)}>使用最大值 ↗</button>
        </div>
        {(dirty && !valid) && <p className="scheduler-error" role="alert">请输入 {minimum}～{maximum} 的整数。</p>}
        {(intervalDirty && !intervalValid) && <p className="scheduler-error" role="alert">任务间隔请输入 {intervalMin}～{intervalMax} 的整数。</p>}
      </div>
      <dl className="scheduler-stats"><div><dt><i className="scheduler-dot" />运行中</dt><dd>{settings?.running ?? '—'}</dd></div><div><dt>排队中</dt><dd>{settings?.queued ?? '—'}</dd></div><div><dt>已生效上限</dt><dd>{settings?.max_concurrency ?? '—'}<small> / {maximum}</small></dd></div></dl>
    </div>
    <div className="scheduler-footer">
      <p id="scheduler-help">实际并发受空闲账号限制；同账号不同时生成。调整设置不会提交生成任务。</p>
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
        <input type="checkbox" checked={generationVisible} disabled={!generationVisible || !isolatedDesktop} onChange={e => void saveGenerationSettings({ visible: e.target.checked })} />
        <span>生成任务使用可见的专用浏览器窗口（降低平台风控特征；窗口在桌面下层运行，不抢焦点）</span>
      </label>
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
        <input type="checkbox" checked={isolatedDesktop} disabled={!generationVisible} onChange={e => void saveGenerationSettings({ isolated: e.target.checked })} />
        <span>生成任务在独立桌面运行（窗口完全不出现在当前桌面、零打扰；需要人工处理的账号仍通过通知打开可见窗口）</span>
      </label>
      <details><summary>调度规则</summary><p>同一邮箱或共用浏览器资料的账号互斥；旧版原生桥接串行。调低上限不打断正在运行的任务。相邻两次任务派发至少间隔设定的秒数（含随机抖动），0 表示不限制。新安装默认 {maximum}，已保存的自定义值继续保留。上限不代表 Google 的并发配额。</p></details>
    </div>
    {notice && <p className={`scheduler-notice${failed ? ' scheduler-error' : ''}`} role="status">{notice}</p>}
  </section>
}
