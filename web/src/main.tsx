import WorkLog from './WorkLog'
import StartupCheck, { type StartupState } from './StartupCheck'
﻿import { RetryOptions, RetryProgress, type RetryState } from './AutoRetry'
import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import ApiIntegration from './ApiIntegration'
import SchedulerSettings from './SchedulerSettings'
import MaterialInputs, { type VideoMode, type Materials } from './MaterialInputs'
import CreditEstimator, { type Pricing, type VideoEstimates } from './CreditEstimator'
import PrepareAccount from './PrepareAccount'
import UnifiedAccounts from './UnifiedAccounts'
import ProxySettings from './ProxySettings'
import ManualAttentionBanner from './ManualAttentionBanner'
import StartupSettings from './StartupSettings'
import ImagePageCheck from './ImagePageCheck'
import VideoPageCheck from './VideoPageCheck'
import { videoModels, videoModelLabel, supportsVideoMode, type VideoModel } from './videoModels'
import { imageModels, imageModelNote } from './imageModels'
import MediaLibrary, { modelLabel, type LibraryMedia } from './MediaLibrary'
import './style.css'

declare const __FLOW_CLIENT_VERSION__: string

type Kind = 'image' | 'video'
const strategyNames: Record<string, string> = { round_robin: '轮流使用', highest_credits: '积分最多优先', lowest_credits: '积分最少优先' }
type Status = 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted' | 'canceled'
type Account = { generation_blocked?: boolean; generation_block_reason?: string | null; video_estimates?: VideoEstimates; id: string; label: string; email: string | null; credits: number | null; credits_checked_at?: string | null; read_error?: string | null; status: string; session: string; selectable: boolean; can_open_browser: boolean; session_saved_at: string | null }
type Accounts = { video_credit_pricing?: Pricing; accounts: Account[]; total_credits: number | null; unread: number; stale_balance_rows: number; last_refresh_at: string | null; refresh_error?: string | null }
type LocalProfile = { id: string; label: string; emails: string[] }
type Media = LibraryMedia
type Job = { id: string; kind: Kind; status: Status; request: { prompt: string; aspect: string; account_id: string | null; account_strategy?: string; selected_account_id?: string; video_model?: string; video_mode?: VideoMode; reference_image_ids?: string[]; source_video_id?: string; end_image_id?: string; start_image_id: string | null }; result: { auto_retry?: RetryState; execution?: { route?: string; browser_closed?: boolean; recovered_without_generation?: boolean; outcome?: string; uncertainty_query_started?: boolean; generation_response?: { rpc_error_codes?: number[]; rpc_errors?: { code: number; reason_codes: string[]; reason_status: string }[]; diagnostic_format?: string } }; files: { name: string; url: string }[]; engine_job_id?: string; account_id?: string } | null; error: string | null; created_at: string; started_at: string | null; finished_at: string | null; retry_of: string | null }
type History = { generations: { job_id: string; kind: Kind; account_id: string; status: string; aspect: string; credits_spent: number | null; created_at: string }[]; total: number; limit: number; offset: number }
type View = 'overview' | 'create' | 'jobs' | 'media' | 'accounts' | 'integration'
const views: View[] = ['overview', 'create', 'jobs', 'media', 'accounts', 'integration']

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/console/api${path}`, {...options, headers: {...options?.headers, 'X-Flow-Client':'desktop'}})
  let body: unknown
  try { body = await res.json() } catch { body = {} }
  if (!res.ok) {
    const detail = (body as { detail?: unknown }).detail
    throw new Error(typeof detail === 'string' ? detail : `请求失败（HTTP ${res.status}）`)
  }
  return body as T
}
const json = (value: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) })
const date = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
const statusText: Record<Status, string> = { queued: '排队中', running: '生成中', succeeded: '已完成', failed: '失败', interrupted: '结果待核查', canceled: '已取消' }
const jobErrorText = (message: string) => message === '验证未通过，请检查浏览器扩展和 Flow 页面。' ? '旧版未保存具体上游原因，不能确认是验证码失败。请核对引擎历史和积分，勿连续重试。' : message

function App() {
  const [startup, setStartup] = useState<StartupState | null>(null)
  const startupProfilesLoaded = useRef(false)
  const [view, setView] = useState<View>(() => {
    const hash = window.location.hash.slice(1) as View
    return views.includes(hash) ? hash : 'overview'
  })
  const [accounts, setAccounts] = useState<Accounts>({ accounts: [], total_credits: null, unread: 0, stale_balance_rows: 0, last_refresh_at: null })
  const [jobs, setJobs] = useState<Job[]>([])
  const [media, setMedia] = useState<Media[]>([])
  const [history, setHistory] = useState<History>({ generations: [], total: 0, limit: 50, offset: 0 })
  const historyQuery = useRef({ search: '', kind: 'all', status: 'all', offset: 0 })
  const [historySearch, setHistorySearch] = useState('')
  const [historyKind, setHistoryKind] = useState('all')
  const [historyStatus, setHistoryStatus] = useState('all')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [localProfiles, setLocalProfiles] = useState<LocalProfile[] | null>(null)
  const [localProfileOpened, setLocalProfileOpened] = useState(false)
  const [extensionDir, setExtensionDir] = useState('')
  const pendingAccountCount = useRef<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [removeTarget, setRemoveTarget] = useState<Account | null>(null)
  const [removalError, setRemovalError] = useState('')
  const [removedAccounts, setRemovedAccounts] = useState<{ id: string; label: string; email: string | null; removed_at: string }[]>([])
  const [preview, setPreview] = useState<Media | null>(null)
  const [kind, setKind] = useState<Kind>('video')
  const [mode, setMode] = useState<VideoMode>('text')
  const [prompt, setPrompt] = useState('')
  const [aspect, setAspect] = useState('16:9')
  const [duration, setDuration] = useState('8s')
  const [quality, setQuality] = useState('720p')
  const [model, setModel] = useState('beluga')
  const [videoModel, setVideoModel] = useState<VideoModel>('omni_flash')
  const isVeo = videoModel !== 'omni_flash'
  const flowDetermines = mode === 'edit' || (mode === 'references' && !isVeo)
  useEffect(() => {
    if (kind !== 'video' || !isVeo) return
    if (duration !== '8s') setDuration('8s')
    if (quality !== '720p') setQuality('720p')
  }, [kind, isVeo, duration, quality, mode])
  const [count, setCount] = useState(1)
  const [accountId, setAccountId] = useState('')
  const [accountStrategy, setAccountStrategy] = useState(() => { const saved = localStorage.getItem('flow-account-strategy'); return saved && strategyNames[saved] ? saved : 'round_robin' })
  const [materials, setMaterials] = useState<Materials>({ references: [] })
  const [autoRetry, setAutoRetry] = useState(true)
  const [retrySwitchAccounts, setRetrySwitchAccounts] = useState(true)
  const [mediaRightsConfirmed, setMediaRightsConfirmed] = useState(false)
  useEffect(() => setMediaRightsConfirmed(false), [materials, mode, kind])
  const [materialBusy, setMaterialBusy] = useState(false)
  const [preparing, setPreparing] = useState(false)

  async function load() {
    try {
      const status = await api<StartupState>('/startup')
      setStartup(status)
      if (!status.ready) { startupProfilesLoaded.current = false; return }
      if (!startupProfilesLoaded.current && status.local_profiles) {
        setLocalProfiles(status.local_profiles)
        startupProfilesLoaded.current = true
      }
    } catch {
      setStartup(null)
      setError('无法连接本机服务，请确认客户端已启动；若持续失败，请检查启动错误提示。')
      return
    }
    const results = await Promise.allSettled([
      api<Accounts>('/accounts'), api<Job[]>('/jobs'), api<Media[]>('/media'),
      api<History>(`/history?${new URLSearchParams({ ...historyQuery.current, offset: String(historyQuery.current.offset) })}`),
      api<typeof removedAccounts>('/accounts/removed'),
    ])
    if (results[0].status === 'fulfilled') {
      const latest = results[0].value
      setAccounts(latest)
      if (pendingAccountCount.current !== null && latest.accounts.filter(a => a.selectable).length > pendingAccountCount.current) {
        pendingAccountCount.current = null
        setNotice('Flow 账号已保存到本机，现已显示在账号列表中。')
      }
    }
    if (results[1].status === 'fulfilled') setJobs(results[1].value)
    if (results[2].status === 'fulfilled') setMedia(results[2].value)
    if (results[3].status === 'fulfilled') setHistory(results[3].value)
    if (results[4].status === 'fulfilled') setRemovedAccounts(results[4].value)
    if (results.some(x => x.status === 'rejected')) setError('部分数据读取失败，请确认本地服务已启动。')
    else setError('')
  }
  useEffect(() => { void load(); const timer = setInterval(() => void load(), 5000); return () => clearInterval(timer) }, [])
  useEffect(() => { window.history.replaceState(null, '', `#${view}`) }, [view])
  useEffect(() => {
    const onHashChange = () => {
      const hash = window.location.hash.slice(1) as View
      if (views.includes(hash)) setView(hash)
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  async function action(fn: () => Promise<unknown>, success: string) {
    setBusy(true); setError(''); setNotice('')
    try { await fn(); setNotice(success); await load() }
    catch (e) { setError(e instanceof Error ? e.message : '操作失败') }
    finally { setBusy(false) }
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!startup?.ready || materialBusy || preparing) return
    const retryNotice = autoRetry ? ` 失败后最多自动重试3次，连同首次最多4次。${retrySwitchAccounts ? '每次使用不同账号。' : '沿用原账号。'}已提交且结果不明时先查询原结果，不重复生成。` : ''
    if (!window.confirm((mode === 'edit' ? '确认提交视频编辑？原视频将上传到所选账号的 Flow，实际处理可能消耗积分。' : '确认提交生成任务？参考素材将交给所选账号的 Flow，实际生成可能消耗积分。') + retryNotice)) return
    await action(async () => {
      await api<Job>('/jobs', json({ kind, prompt: prompt.trim(), aspect: flowDetermines ? '16:9' : aspect, duration: mode === 'edit' ? '8s' : duration, quality: flowDetermines ? '720p' : quality, model, count: kind === 'image' || mode === 'references' || mode === 'edit' ? 1 : count,
        auto_retry_limit: autoRetry ? 3 : 0, retry_uncertain: false, retry_switch_accounts: retrySwitchAccounts,
        video_model: videoModel, account_strategy: accountStrategy, account_id: accountId || null, video_mode: kind === 'video' ? mode : 'text',
        start_image_id: kind === 'video' && mode === 'frames' ? materials.start?.id || null : null,
        end_image_id: kind === 'video' && mode === 'frames' ? materials.end?.id || null : null,
        reference_image_ids: kind === 'video' && mode === 'references' ? materials.references.map(a => a.id) : [],
        source_video_id: kind === 'video' && mode === 'edit' ? materials.video?.id || null : null,
        media_rights_confirmed: kind === 'video' && (mode === 'frames' || mode === 'references') && mediaRightsConfirmed }))
      setView('jobs')
    }, '任务已加入本地队列。')
  }
  async function retry(job: Job) {
    const retryNotice = autoRetry ? ` 新任务失败后最多再重试3次，共最多4次尝试。${retrySwitchAccounts ? '每次使用不同账号。' : '沿用原账号。'}结果不明时先查询原结果。` : ' 新任务不自动重试。'
    if (!window.confirm('上一次任务可能已产生积分消耗，确认再次提交相同生成参数？请先核对素材和余额。' + retryNotice)) return
    await action(() => api(`/jobs/${job.id}/retry`, json({ acknowledge_possible_charge: true, auto_retry_limit: autoRetry ? 3 : 0, retry_uncertain: false, retry_switch_accounts: retrySwitchAccounts })), '已创建新的重试任务。')
  }
  async function startConnection() {
    pendingAccountCount.current = accounts.accounts.filter(a => a.selectable).length
    await action(() => api('/accounts/connect', { method: 'POST' }), '专用 Chrome 窗口已打开。请在该窗口登录 Flow，账号会自动出现在下方。')
  }
  function applyHistoryFilters() {
    historyQuery.current = { search: historySearch.trim(), kind: historyKind, status: historyStatus, offset: 0 }
    void load()
  }
  async function removeAccount() {
    if (!removeTarget) return
    const target = removeTarget
    setBusy(true); setRemovalError('')
    try {
      await api(`/accounts/${encodeURIComponent(target.id)}/remove`, json({ confirmed: true }))
      if (accountId === target.id) setAccountId('')
      setRemoveTarget(null)
      setNotice('已删除本地连接记录，账号已退出调度。素材和历史已保留，可在“已删除的账号记录”中恢复。')
      await load()
    } catch (e) { setRemovalError(e instanceof Error ? e.message : '删除失败，请重试。') }
    finally { setBusy(false) }
  }
  function requestRemoval(target: Account) { setRemovalError(''); setRemoveTarget(target) }
  function pageHistory(offset: number) {
    historyQuery.current = { ...historyQuery.current, offset }
    void load()
  }
  const unfinishedAccounts = accounts.accounts.filter(a => a.session === 'incomplete' && !a.email && !a.can_open_browser)
  const connectedAccounts = accounts.accounts.filter(a => !unfinishedAccounts.includes(a))
  const knownCreditAccounts = connectedAccounts.filter(a => a.credits !== null).length
  const pendingCreditAccounts = connectedAccounts.filter(a => a.credits === null).length
  const activeCount = jobs.filter(j => j.status === 'queued' || j.status === 'running').length
  const nav: { id: View; icon: string; label: string }[] = [
    { id: 'overview', icon: '◫', label: '总览' }, { id: 'create', icon: '✧', label: '创建任务' },
    { id: 'jobs', icon: '≡', label: '任务管理' }, { id: 'media', icon: '▦', label: '素材库' },
    { id: 'accounts', icon: '◎', label: '账号管理' }, { id: 'integration', icon: '⌘', label: '接口接入' },
  ]
  return !startup?.ready ? <StartupCheck status={startup}/> : <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">F<span>·</span></div><div><strong>FLOW AGENT</strong><small>本地创作控制台</small></div></div>
      <div className="side-caption">工作空间</div>
      <nav>{nav.map(n => <button key={n.id} className={view === n.id ? 'nav-item active' : 'nav-item'} onClick={() => setView(n.id)}><span className="nav-icon">{n.icon}</span>{n.label}{n.id === 'jobs' && activeCount > 0 && <em>{activeCount}</em>}</button>)}</nav>
      <div className="side-bottom"><span className="live-dot" /> 本地服务 <b>127.0.0.1</b><small title="当前客户端构建版本">客户端 v{__FLOW_CLIENT_VERSION__}</small><small>单机账号隔离调度 · 不提供云端访问</small></div>
    </aside>
    <main className="main">
      <header className="topbar"><div className="breadcrumb">工作空间 <span>/</span> {nav.find(n => n.id === view)?.label}</div><div className="top-actions"><StartupSettings /><span className="local-pill"><span className="live-dot" /> 本地运行</span><button className="icon-button" aria-label="刷新数据" onClick={() => void load()}>↻</button></div></header>
      <div className="content">
        {error && <div className="alert error">{error}<button onClick={() => setError('')}>×</button></div>}
        {notice && <div className="alert success">{notice}<button onClick={() => setNotice('')}>×</button></div>}
        <ManualAttentionBanner />
        {['create', 'jobs'].includes(view) && <SchedulerSettings />}
        {view === 'overview' && <>

          <div className="section-head"><div><span className="eyebrow">AT A GLANCE</span><h2>运行概况</h2></div><span className="muted">每 5 秒自动同步</span></div>
          <div className="stat-grid"><div className="stat-card"><div className="stat-icon violet">◎</div><span>已连接账号</span><strong>{accounts.accounts.filter(a => a.selectable).length}</strong><small>本地会话文件</small></div><div className="stat-card"><div className="stat-icon blue">◈</div><span>账号总积分（已读取）</span><strong>{accounts.total_credits ?? '—'}</strong><small>{knownCreditAccounts} 个账号已读取{pendingCreditAccounts > 0 ? ` · ${pendingCreditAccounts} 个待读取` : ''}</small></div><div className="stat-card"><div className="stat-icon orange">◷</div><span>进行中任务</span><strong>{activeCount}</strong><small>按并发设置处理</small></div><div className="stat-card"><div className="stat-icon green">▧</div><span>本地素材</span><strong>{media.length}</strong><small>图片与视频</small></div></div>
          <p className="helper">积分更新时间：{date(accounts.last_refresh_at)} · 待同步账号未计入合计。 <button className="text-link" onClick={() => setView('accounts')}>查看账号与刷新积分 ↗</button></p>
          <div className="hero"><div><span className="eyebrow">FLOW AGENT / DASHBOARD</span><h1>让灵感，持续流动。</h1><p>从一句描述到可用画面。账号、任务和素材，集中在一个工作台。</p><button className="primary" onClick={() => setView('create')}>✧ &nbsp; 开始创作 <span>↗</span></button></div><div className="hero-art"><div className="orbit orbit-a"/><div className="orbit orbit-b"/><div className="hero-core">F</div></div></div>
          <SchedulerSettings />
          <div className="two-col"><section className="panel"><div className="panel-head"><div><h3>最近任务</h3><p>查看生成进度与失败原因</p></div><button className="text-link" onClick={() => setView('jobs')}>查看全部 ↗</button></div>{jobs.length ? jobs.slice(0, 5).map(j => <div className="mini-job" key={j.id}><div className="mini-kind">{j.kind === 'video' ? '▶' : '◧'}</div><div><strong>{j.request.prompt}</strong><small>{j.kind === 'video' ? '视频' : '图片'} · {date(j.created_at)}</small></div><span className={`status ${j.status}`}>{j.result?.execution?.outcome === 'pending_review' ? '待核对' : statusText[j.status]}</span></div>) : <Empty title="还没有任务" subtitle="创建第一个任务，开始生成图片或视频。"/>}</section><section className="panel"><div className="panel-head"><div><h3>账号与积分</h3><p>显示账号名称，不显示会话数据</p></div><button className="text-link" onClick={() => setView('accounts')}>管理账号 ↗</button></div>{accounts.accounts.length ? connectedAccounts.slice(0, 5).map(a => <div className="account-row" key={a.id}><div className="avatar">{a.label.slice(-2).toUpperCase()}</div><div><strong>{a.label}</strong><small>{a.email || (a.selectable ? '本地会话已保存' : '缺少本地会话')}</small></div><b>{a.credits ?? '—'} <small>积分</small></b></div>) : <Empty title="尚未连接账号" subtitle="前往账号管理，点击“连接 Flow 账号”。"/>}</section></div>
        </>}
        {view === 'create' && <><PageTitle tag="CREATE" title="创建生成任务" desc="选择创作方式，提交后由本机队列依次调用 Flow 引擎。"/><div className="create-grid"><form className="panel form-panel" onSubmit={submit}><div className="field-label">创作类型</div><div className="segment"><button type="button" className={kind === 'video' ? 'selected' : ''} disabled={materialBusy} onClick={() => { setKind('video'); setAspect('16:9') }}>▶ 视频生成</button><button type="button" className={kind === 'image' ? 'selected' : ''} disabled={materialBusy} onClick={() => { setKind('image'); setMode('text'); setCount(1) }}>▧ 图片生成</button></div>{kind === 'video' && <><div className="field-label">视频模式</div><div className="segment video-modes">{([['text', '文生视频'], ['frames', '首尾帧'], ['references', '参考图'], ['edit', '视频编辑']] as const).map(([key, label]) => <button key={key} type="button" disabled={materialBusy || !supportsVideoMode(videoModel, key)} className={mode === key ? 'selected' : ''} onClick={() => setMode(key)}>{label}</button>)}</div></>}
          {kind === 'video' && <><label className="field-label" htmlFor="video-model">视频模型</label><select id="video-model" value={videoModel} disabled={materialBusy} onChange={e => setVideoModel(e.target.value as VideoModel)}>{videoModels.map(m => <option key={m.value} value={m.value} disabled={!supportsVideoMode(m.value, mode)}>{m.label}（官方网页）</option>)}</select><p className="helper">{isVeo ? 'Veo 3.1 当前网页固定 8 秒、720p；Lite / Fast 支持参考图，Quality 不支持参考图。网页确认模型后才提交，不自动改用 Omni。' : 'Omni 支持 4 / 6 / 8 / 10 秒、360p / 720p。Veo 可在此切换；视频编辑仅支持 Omni。'}</p></>}
          {kind === 'video' && <MaterialInputs referenceSettingsExplicit={isVeo} mode={mode} value={materials} onChange={setMaterials} media={media} onBusy={setMaterialBusy} />}
          {kind === 'video' && (mode === 'frames' || mode === 'references') && <label className="media-rights-consent"><input type="checkbox" checked={mediaRightsConfirmed} onChange={e => setMediaRightsConfirmed(e.target.checked)} /> 我有权使用本次参考图，允许程序自动确认 Google 图片使用权（同一图片会记住授权）。</label>}
          <label className="field-label" htmlFor="prompt">提示词</label><textarea id="prompt" required maxLength={4000} placeholder="描述你想生成的场景、动作、镜头和氛围…" value={prompt} onChange={e => setPrompt(e.target.value)} /><div className="char-count">{prompt.length} / 4000</div>
          <div className="form-grid"><div><label className="field-label" htmlFor="aspect">画面比例</label><select id="aspect" disabled={kind === 'video' && flowDetermines} value={kind === 'video' && flowDetermines ? 'auto' : aspect} onChange={e => setAspect(e.target.value)}>{kind === 'video' && flowDetermines && <option value="auto">由 Flow 决定</option>}{(kind === 'video' ? ['16:9', '9:16'] : ['16:9', '9:16', '1:1', '4:3', '3:4']).map(a => <option key={a}>{a}</option>)}</select></div><div><label className="field-label" htmlFor="account">生成账号</label><select id="account" value={accountId} onChange={e => setAccountId(e.target.value)}><option value="">自动调度</option>{accounts.accounts.filter(a => a.selectable).map(a => <option key={a.id} value={a.id}>{a.label} · {a.credits ?? '未知'} 积分</option>)}</select></div></div>
          <label className="field-label" htmlFor="account-strategy">账号积分使用策略</label><select id="account-strategy" disabled={!!accountId} value={accountStrategy} onChange={e => { setAccountStrategy(e.target.value); localStorage.setItem('flow-account-strategy', e.target.value) }}>{Object.entries(strategyNames).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><p className="helper">{accountId ? '已指定账号，本任务不使用自动调度策略。' : '在缓存积分足够的账号中选择。积分最少优先可先使用小余额；设置保存在本机浏览器。'}</p>
          <div className="form-grid">{kind === 'video' ? mode === 'edit' ? <p className="helper">视频编辑不指定时长与画质，按原视频及 Flow 结果处理。</p> : <><div><label className="field-label" htmlFor="duration">时长</label><select id="duration" value={duration} onChange={e => setDuration(e.target.value)}>{(isVeo ? ['8s'] : ['4s', '6s', '8s', '10s']).map(x => <option key={x}>{x}</option>)}</select></div><div><label className="field-label" htmlFor="quality">画质</label><select id="quality" disabled={flowDetermines || isVeo} value={flowDetermines ? 'auto' : quality} onChange={e => setQuality(e.target.value)}><option>720p</option>{!isVeo && <option>360p</option>}{flowDetermines && <option value="auto">由 Flow 决定</option>}</select></div></> : <div><label className="field-label" htmlFor="model">图片模型</label><select id="model" aria-describedby="image-model-note" value={model} onChange={e => setModel(e.target.value)}>{imageModels.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select></div>}<div><label className="field-label" htmlFor="count">{kind === 'image' ? '数量（图片固定 1 张）' : '数量'}</label><select id="count" disabled={kind === 'image' || mode === 'references' || mode === 'edit'} value={kind === 'image' || mode === 'references' || mode === 'edit' ? 1 : count} onChange={e => setCount(Number(e.target.value))}>{[1,2,3,4].map(n => <option key={n}>{n}</option>)}</select></div></div>
          {kind === 'image' && <p className="helper" id="image-model-note">{imageModelNote}</p>}
          <RetryOptions enabled={autoRetry} switchAccounts={retrySwitchAccounts} onEnabled={setAutoRetry} onSwitchAccounts={setRetrySwitchAccounts} />
          <button className="primary submit" disabled={!startup?.ready || busy || preparing || materialBusy || !prompt.trim() || (kind === 'video' && ((mode === 'frames' && !materials.start) || (mode === 'references' && !materials.references.length) || (mode === 'edit' && !materials.video)))}>{busy ? '正在提交…' : '提交生成任务 ↗'}</button><p className="helper">生成可能消耗积分。仅排队中的任务可取消；运行中结果由底层引擎决定。</p></form><aside className="create-aside">{kind === 'video' && <VideoPageCheck videoModel={videoModel} accountId={accountId} disabled={busy || preparing || activeCount > 0} aspect={aspect} duration={duration} quality={quality} count={count} />}{kind === 'video' && mode !== 'edit' && mode !== 'references' && <CreditEstimator accounts={accounts.accounts.filter(a => a.selectable && (!accountId || a.id === accountId))} pricing={accounts.video_credit_pricing} duration={duration} quality={quality} compact />}{kind === 'video' && (mode === 'edit' || mode === 'references') && <div className="tip-card"><h3>{mode === 'edit' ? '视频编辑' : '参考图生视频'}</h3><p>{mode === 'edit' ? '官方当前编辑标准为 40 积分/条，实际以 Flow 提交时为准。' : '参考图模式不支持指定画质、比例或批量数量，单次提交 1 条。'}</p></div>}<div className="tip-card"><span>✦</span><h3>创作提示</h3><p>描述主体、动作、光线和镜头运动，可让结果更贴近预期。</p></div><div className="tip-card muted-card"><span>◎</span><h3>账号调度</h3><p>可选择轮流使用、积分最多优先、积分最少优先。视频按引擎估算用量筛选，余额未知或不足时停止并提示刷新，不会盲选账号。策略决定提交会话，非扣费上限；同一 Google 会话内的账号切换仍由底层引擎控制。</p></div></aside></div></>}
        {view === 'jobs' && <><PageTitle tag="TASKS" title="任务管理" desc="查看排队、生成进度与失败原因。历史记录来自本地 SQLite。"/><section className="panel"><h3>手动重试的后续策略</h3><p className="helper">下面设置用于点击右侧“重试”后新创建的任务，不改变已有任务。</p><RetryOptions enabled={autoRetry} switchAccounts={retrySwitchAccounts} onEnabled={setAutoRetry} onSwitchAccounts={setRetrySwitchAccounts} /></section><div className="panel"><div className="panel-head"><div><h3>后台任务</h3><p>最近 100 条 · 每 5 秒同步 · 结果不明时隐藏查询180秒，超时待核对，不重复提交</p></div><button className="secondary" onClick={() => void load()}>↻ 刷新</button></div>{jobs.length ? <div className="table-wrap"><table><thead><tr><th>任务</th><th>类型 / 比例</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{jobs.map(j => <tr key={j.id}><td><div className="task-prompt">{j.request.prompt}</div><small>{j.id.slice(0, 8)}{j.retry_of ? ' · 重试任务' : ''}</small>{j.error && <div className="task-error">{jobErrorText(j.error)}</div>}{j.result?.execution?.generation_response?.rpc_error_codes?.length ? <details className="helper"><summary>官方错误诊断（已脱敏）</summary><p>错误码：{j.result.execution.generation_response.rpc_error_codes.join('、')}。{j.result.execution.generation_response.diagnostic_format ? '具体说明见上方错误信息；只记录可识别的官方原因类别，不保留原始响应或凭据。' : '旧记录未保存官方错误说明，无法据此判定具体原因。'}</p></details> : null}{j.result?.execution?.route === 'official_page' && <small>官方网页自动生成 · {j.result.execution.browser_closed ? '本次浏览器已关闭' : '关窗未确认'}</small>}<RetryProgress state={j.result?.auto_retry} status={j.status} />{j.result?.execution?.recovered_without_generation && j.status === 'succeeded' && <small>已恢复本次原始素材（未重新生成）；原失败记录已保留。</small>}{j.result?.files.map(f => <a key={f.name} className="result-link" href={f.url} download>下载 {f.name} ↗</a>)}</td><td>{j.kind === 'video' ? '视频' : '图片'}<br/><small>{j.request.video_mode === 'edit' || (j.request.video_mode === 'references' && (!j.request.video_model || j.request.video_model === 'omni_flash')) ? '比例由 Flow 决定' : j.request.aspect}{j.kind === 'video' ? ` · ${videoModelLabel(j.request.video_model)}` : ''}</small><br/><small>{j.request.video_mode === 'edit' ? '视频编辑 · 原视频 1 个' : j.request.video_mode === 'references' ? `参考图 ${j.request.reference_image_ids?.length || 0} 张` : j.request.start_image_id ? `首帧${j.request.end_image_id ? ' + 尾帧' : ''}` : ''}</small><br/><small>{j.request.account_id ? '指定账号' : strategyNames[j.request.account_strategy || 'round_robin']}</small><br/><small>{accounts.accounts.find(a => a.id === (j.request.selected_account_id || j.request.account_id))?.label || (j.request.selected_account_id ? '已选择本地账号' : '等待选择账号')}</small></td><td><span className={`status ${j.status}`}>{j.result?.execution?.outcome === 'pending_review' ? '待核对' : statusText[j.status]}</span></td><td>{date(j.created_at)}</td><td>{j.status === 'queued' && <button className="text-link" disabled={busy} onClick={() => void action(() => api(`/jobs/${j.id}/cancel`, { method: 'POST' }), '排队任务已取消。')}>取消</button>}{(j.status === 'failed' || j.status === 'interrupted') && <button className="text-link" disabled={busy} onClick={() => void retry(j)}>重试</button>}<WorkLog id={j.id}/></td></tr>)}</tbody></table></div> : <Empty title="还没有后台任务" subtitle="创建一个生成任务后，可在这里跟踪状态。"/>}</div><div className="panel history-panel"><div className="panel-head"><div><h3>Flow 引擎历史</h3><p>原项目数据库记录，包含 CLI 与 REST 生成</p></div><span className="muted">匹配 {history.total} 条</span></div><div className="history-filters"><input className="search" placeholder="搜索任务或账号 ID" value={historySearch} onChange={e => setHistorySearch(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') applyHistoryFilters() }} /><select aria-label="历史类型" value={historyKind} onChange={e => setHistoryKind(e.target.value)}><option value="all">全部类型</option><option value="image">图片</option><option value="video">视频</option></select><select aria-label="历史状态" value={historyStatus} onChange={e => setHistoryStatus(e.target.value)}><option value="all">全部状态</option><option value="succeeded">已完成</option><option value="failed">失败</option><option value="running">生成中</option><option value="queued">排队中</option></select><button className="secondary" onClick={applyHistoryFilters}>查询</button></div>{history.generations.length ? <div className="table-wrap"><table><thead><tr><th>任务 ID</th><th>类型</th><th>账号</th><th>状态</th><th>积分</th><th>时间</th></tr></thead><tbody>{history.generations.map((h, i) => <tr key={`${h.job_id}-${i}`}><td>{h.job_id?.slice(0, 12) || '—'}</td><td>{h.kind}</td><td>{h.account_id}</td><td>{h.status}</td><td>{h.credits_spent ?? '—'}</td><td>{h.created_at}</td></tr>)}</tbody></table></div> : <Empty title="暂无引擎历史" subtitle="真实生成后会显示原项目数据库中的记录。"/>}<div className="pagination"><span>{history.total ? `${history.offset + 1}–${Math.min(history.offset + history.limit, history.total)}` : '0'} / {history.total}</span><button className="secondary" disabled={history.offset === 0} onClick={() => pageHistory(Math.max(0, history.offset - history.limit))}>上一页</button><button className="secondary" disabled={history.offset + history.limit >= history.total} onClick={() => pageHistory(history.offset + history.limit)}>下一页</button></div></div></>}
        {view === 'media' && <MediaLibrary onPreview={setPreview} />}
        {view === 'accounts' && <>
          <PageTitle tag="ACCOUNTS" title="账号管理" desc="查看本地账号会话与缓存积分。会话内容只保存在本机服务端。"/>
          <PrepareAccount accounts={accounts.accounts} accountId={accountId} onAccount={setAccountId} onRunning={setPreparing} activeJobs={activeCount} onRemove={id => { const target = accounts.accounts.find(a => a.id === id); if (target) requestRemoval(target) }} />
          <ProxySettings />
          <div className="account-summary panel">
            <div><span className="eyebrow">已读取积分合计</span><strong>{accounts.total_credits ?? '—'}</strong><small>{knownCreditAccounts} 个账号已读取 · {pendingCreditAccounts} 个待读取</small></div>
            <div><span className="eyebrow">LAST REFRESH</span><strong className="date-large">{date(accounts.last_refresh_at)}</strong><small>最近一次成功读取时间</small></div>
            <button className="primary" disabled={busy} onClick={() => void action(async () => { setAccounts(await api<Accounts>('/accounts/refresh', { method: 'POST' })) }, '已刷新可用账号的积分。')}>{busy ? '刷新中…' : '↻ 刷新积分'}</button>
          </div>
          <UnifiedAccounts accounts={accounts.accounts} pricing={accounts.video_credit_pricing} actions={(row, attention) => {
            const a = accounts.accounts.find(item => item.id === row.id)!
            return <>
              {a.can_open_browser && <button className="secondary" disabled={busy} onClick={() => void action(() => api(`/accounts/${encodeURIComponent(a.id)}/open`, { method: 'POST' }), '已打开对应账号的 Flow 页面。')} title="显示对应账号浏览器，由本人检查后关闭">打开 Flow</button>}
              {attention && a.can_open_browser && <button className="secondary" disabled={busy || preparing || activeCount > 0} onClick={() => void action(() => api('/accounts/prepare', json({ account_id: a.id })), '已开始核对所选账号，不提交生成。')}>重新连接</button>}
              {a.session !== 'missing' && <button className="secondary danger-text" disabled={busy || activeCount > 0 || preparing} onClick={() => requestRemoval(a)}>删除记录</button>}
            </>
          }} />
          <details className="panel"><summary>高级验证工具（不生成）</summary><ImagePageCheck accountId={accountId} disabled={busy || preparing || activeCount > 0} /><VideoPageCheck videoModel={videoModel} accountId={accountId} disabled={busy || preparing || activeCount > 0} aspect={aspect} duration={duration} quality={quality} count={count} /></details>
          <details className="panel connect-panel">
            <summary>复用本机已登录账号 · {localProfiles === null ? '等待扫描' : `已扫描 ${localProfiles.length} 份本机资料`}</summary>
            <div className="connect-one"><div><h3>复用本机已登录账号</h3><p>每次启动自动扫描普通 Chrome 中的账号。需要接入时展开选择，不复制登录数据库。</p><p className="connect-note">扫描结果只表示本机保存过账号；实际登录是否有效、是否已连接 Flow，需要分别验证。</p></div><button className="primary" disabled={busy} onClick={() => void action(async () => setLocalProfiles(await api<LocalProfile[]>('/accounts/local-profiles')), '已扫描本机 Chrome 账号资料。')}>重新扫描本机账号</button></div>
            {localProfiles !== null && <div className="local-profiles">{localProfiles.length ? localProfiles.map(p => <div className="account-detail" key={p.id}><div className="account-main"><strong>{p.label}</strong>{p.emails.map(email => <small key={email}>{email}</small>)}<small>本机 Chrome 资料 · 尚未验证 Flow 连接</small></div><button className="secondary" disabled={busy} onClick={() => void action(async () => { await api('/accounts/local-profiles/open', json({ profile_id: p.id })); setLocalProfileOpened(true) }, '已打开原 Chrome 账号。首次请按下方说明启用 Flow Bridge；已安装时直接打开 Flow 项目。')}>② 使用这个账号</button></div>) : <p className="helper">未找到本机 Chrome 账号资料。可使用下方“新增账号”登录一次。</p>}</div>}
            {localProfileOpened && <div className="local-setup"><h3>首次连接：在刚打开的 Chrome 中启用一次扩展</h3><ol><li>在“扩展程序”标签页右上角开启“开发者模式”。</li><li>点击下方“打开扩展文件夹”，复制资源管理器地址栏中的文件夹路径。</li><li>回到 Chrome，点击“加载已解压的扩展程序”，选择这个文件夹。</li><li>打开同一窗口的 Flow 标签页，进入一个项目，等待会话同步，再返回刷新积分。</li></ol><button className="secondary" disabled={busy} onClick={() => void action(async () => { const r = await api<{ extension_dir: string }>('/accounts/extension-folder', { method: 'POST' }); setExtensionDir(r.extension_dir) }, '扩展文件夹已打开。请复制资源管理器地址栏中的路径。')}>③ 打开扩展文件夹</button>{extensionDir && <p className="helper">扩展文件夹：{extensionDir}</p>}<p className="helper">已安装扩展时无需重复安装或登录。请每次只连接一个 Chrome 账号，避免多个扩展争用桥接。若 Google 已使登录失效，仍需在原窗口重新验证。</p></div>}
          </details>
          <section className="panel connect-panel">
            <div className="connect-one"><div><h3>在专用浏览器中新增账号</h3><p>没有可复用账号时，在专用 Chrome 登录一次。以后点击账号列表中的“打开 Flow”复用会话。</p></div><button className="secondary" disabled={busy} onClick={() => void startConnection()}>＋ 新增账号（首次登录）</button></div>
          </section>
          {accounts.refresh_error && <div className="info-box">上次积分查询失败：{accounts.refresh_error}</div>}
          {accounts.stale_balance_rows > 0 && <div className="info-box">{accounts.stale_balance_rows} 条未对应当前会话的历史积分记录已隐藏。刷新积分后，只显示引擎确认属于当前账号的余额。</div>}
          {removedAccounts.length > 0 && <details className="panel unfinished-connections"><summary>已删除的账号记录（{removedAccounts.length} 条，可恢复）</summary><p>这些账号已退出本应用调度。浏览器登录、素材和任务历史仍保留；恢复后会重新检测账号。</p>{removedAccounts.map(a => <div className="removed-account-row" key={a.id}><span>{a.label}{a.email ? ` · ${a.email}` : ''}<small>删除于 {date(a.removed_at)}</small></span><button className="secondary" disabled={busy || activeCount > 0 || preparing} onClick={() => { if (window.confirm(`恢复“${a.email || a.label}”到本地账号列表和调度？不会提交生成任务。`)) void action(() => api(`/accounts/${encodeURIComponent(a.id)}/restore`, json({ confirmed: true })), '账号记录已恢复，程序将自动重新检测身份和积分。') }}>恢复记录</button></div>)}</details>}
          <div className="info-box">账号选择使用原引擎的单账号参数。后台不展示 Cookie、Token 或会话内容，不承诺多账号同时生成。</div>
        </>}
        {view === 'integration' && <ApiIntegration />}
      </div>
    </main>
    {removeTarget && <div className="modal-backdrop"><div className="modal account-remove-dialog" role="dialog" aria-modal="true" aria-labelledby="remove-account-title"><h3 id="remove-account-title">删除本地账号记录？</h3><strong>{removeTarget.label}</strong>{removeTarget.email && <p>{removeTarget.email}</p>}<p>会话保存于 {date(removeTarget.session_saved_at)}</p><p>确认后，此记录将从本应用的账号列表和调度中移除，会话记录在本机归档，可通过“已删除的账号记录”恢复。</p><p>Google 账号、浏览器登录、已生成素材和任务历史均保留。此操作不会提交生成任务。</p>{removalError && <p role="alert" className="danger-text">{removalError}</p>}<div className="dialog-actions"><button className="secondary" disabled={busy} onClick={() => setRemoveTarget(null)}>取消</button><button className="primary danger-button" disabled={busy} onClick={() => void removeAccount()}>{busy ? '处理中…' : '确认删除本地记录'}</button></div></div></div>}
    {preview && <div className="modal-backdrop" onClick={() => setPreview(null)}><div className="modal" onClick={e => e.stopPropagation()}><div className="modal-head"><strong>{preview.name}</strong><button onClick={() => setPreview(null)}>×</button></div>{preview.kind === 'image' ? <img src={preview.url} alt={preview.name} /> : <video src={preview.url} controls autoPlay />}<div className="media-metadata"><h3>原始生成提示词</h3><pre>{preview.prompt || "未找到可确认的原提示词，未自动猜测。"}</pre><p>{modelLabel(preview.model)}{preview.parameters ? ` · ${Object.entries(preview.parameters).map(([key, value]) => `${({ aspect: "比例", count: "数量", duration: "时长", quality: "画质", video_mode: "模式" } as Record<string, string>)[key] || key}：${value}`).join(" · ")}` : ""}</p>{preview.task_id && <p>任务编号：{preview.task_id}</p>}<small>本地文件：{preview.name}</small></div><a className="primary" href={preview.url} download>下载素材 ↓</a></div></div>}
  </div>
}

function PageTitle({ tag, title, desc }: { tag: string; title: string; desc: string }) { return <div className="page-title"><span className="eyebrow">{tag} / FLOW AGENT</span><h1>{title}</h1><p>{desc}</p></div> }
function Empty({ title, subtitle }: { title: string; subtitle: string }) { return <div className="empty"><span>✧</span><strong>{title}</strong><small>{subtitle}</small></div> }

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)


