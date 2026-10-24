import { useEffect, useState } from 'react'
import { integrationContracts, type EndpointId } from './integrationContracts'
import { imageModelProtocol, imageModelNote } from './imageModels'
import './integration.css'
import SchedulerSettings from './SchedulerSettings'

type Connection = 'local' | 'lan'
type Language = 'powershell' | 'python'
type IntegrationInfo = { lan_enabled: boolean; lan_port: number; lan_ipv4: string | null }
type LanKeyInfo = { configured: boolean; key_file: string; updated_at: string | null }

const localPaths = {
  health: '/health', balance: '/console/api/accounts', jobs: '/console/api/jobs',
  media: '/console/api/media', upload: '/console/api/uploads/image', uploadVideo: '/console/api/uploads/video', download: '/api/v1/media/{文件名}', scheduler: '/console/api/scheduler', prepareAll: '/console/api/accounts/prepare-all', preparation: '/console/api/accounts/preparation',
}
const lanPaths = {
  health: '/api/v1/health', balance: '/api/v1/balance', jobs: '/api/v1/jobs',
  media: '/api/v1/media', upload: '/api/v1/uploads/image', uploadVideo: '/api/v1/uploads/video', download: '/api/v1/media/{文件名}', scheduler: '/api/v1/scheduler', prepareAll: '/api/v1/accounts/prepare-all', preparation: '/api/v1/accounts/preparation',
}

export default function ApiIntegration() {
  const [connection, setConnection] = useState<Connection>('local')
  const [language, setLanguage] = useState<Language>('powershell')
  const [info, setInfo] = useState<IntegrationInfo | null>(null)
  const [keyInfo, setKeyInfo] = useState<LanKeyInfo | null>(null)
  const [keyBusy, setKeyBusy] = useState(false)
  const [keyNotice, setKeyNotice] = useState('')
  const [confirmRotate, setConfirmRotate] = useState(false)
  const [copied, setCopied] = useState('')
  const [document, setDocument] = useState('')
  const [documentError, setDocumentError] = useState('')
  const [mcpGuide, setMcpGuide] = useState('')
  const [mcpConfig, setMcpConfig] = useState('')
  const [mcpError, setMcpError] = useState('')
  const [selectedEndpoint, setSelectedEndpoint] = useState<EndpointId>('submit')

  useEffect(() => {
    fetch('/console/api/integration/mcp/guide.md').then(async response => {
      if (!response.ok) throw new Error('MCP 接入说明读取失败，请升级客户端。')
      setMcpGuide(await response.text())
    }).catch(error => setMcpError(error.message))
    fetch('/console/api/integration/mcp').then(async response => {
      if (!response.ok) throw new Error('MCP 配置读取失败')
      setMcpConfig(JSON.stringify(await response.json(), null, 2))
    }).catch(error => setMcpError(error.message))
    fetch('/console/api/integration/document.md').then(async response => {
      if (!response.ok) throw new Error('接口文档读取失败，请刷新页面或更新客户端。')
      setDocument(await response.text())
    }).catch(error => setDocumentError(error.message))
    fetch('/console/api/integration').then(async response => {
      if (!response.ok) throw new Error('读取接入状态失败')
      setInfo(await response.json() as IntegrationInfo)
    }).catch(() => setInfo(null))
    fetch('/console/api/lan-key').then(async response => {
      if (!response.ok) throw new Error('读取密钥状态失败')
      setKeyInfo(await response.json() as LanKeyInfo)
    }).catch(() => setKeyNotice('密钥状态读取失败，请刷新页面。'))
  }, [])

  const lan = connection === 'lan'
  const paths = lan ? lanPaths : localPaths
  const lanBase = `http://${info?.lan_ipv4 || '<本机局域网IP>'}:${info?.lan_port || 8002}`
  const base = lan ? lanBase : window.location.origin
  const readPowerShell = lan
    ? `$base = '${base}'\n$key = Read-Host '输入 API 密钥'\n$headers = @{ Authorization = "Bearer $key" }\nInvoke-RestMethod "$base${paths.balance}" -Headers $headers`
    : `$base = '${base}'\nInvoke-RestMethod "$base${paths.balance}"`
  const readPython = lan
    ? `# 先执行：pip install requests\nimport requests\nbase = '${base}'\nkey = input('输入 API 密钥: ')\nheaders = {'Authorization': f'Bearer {key}'}\nr = requests.get(base + '${paths.balance}', headers=headers, timeout=10)\nr.raise_for_status()\nprint(r.json())`
    : `# 先执行：pip install requests\nimport requests\nbase = '${base}'\nr = requests.get(base + '${paths.balance}', timeout=10)\nr.raise_for_status()\nprint(r.json())`
  const generatePowerShell = `$base = '${base}'\n${lan ? "$key = Read-Host '输入 API 密钥'\n$headers = @{ Authorization = \"Bearer $key\"; 'X-Flow-Allow-Charge' = 'yes' }\n" : ''}$body = @{ kind = 'video'; prompt = '<填写视频提示词>'; aspect = '16:9'; duration = '8s'; quality = '720p'; video_model = 'omni_flash'; account_strategy = 'round_robin'; count = 1 } | ConvertTo-Json\n$job = Invoke-RestMethod "$base${paths.jobs}" -Method Post ${lan ? '-Headers $headers ' : ''}-ContentType 'application/json' -Body $body\n$job.id\nInvoke-RestMethod "$base${paths.jobs}/$($job.id)"${lan ? ' -Headers $headers' : ''}`
  const generatePython = `# 先执行：pip install requests\nimport requests\nbase = '${base}'\n${lan ? "key = input('输入 API 密钥: ')\nheaders = {'Authorization': f'Bearer {key}', 'X-Flow-Allow-Charge': 'yes'}\n" : ''}payload = {'kind': 'video', 'prompt': '<填写视频提示词>', 'aspect': '16:9', 'duration': '8s', 'quality': '720p', 'video_model': 'omni_flash', 'account_strategy': 'round_robin', 'count': 1}\nr = requests.post(base + '${paths.jobs}', json=payload, ${lan ? 'headers=headers, ' : ''}timeout=15)\nr.raise_for_status()\njob_id = r.json()['id']\nprint('任务 ID:', job_id)\nprint(requests.get(base + '${paths.jobs}/' + job_id, ${lan ? 'headers=headers, ' : ''}timeout=10).json())`

  async function copy(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(label)
      window.setTimeout(() => setCopied(''), 2500)
    } catch {
      setCopied('复制失败，请手动选择代码')
    }
  }

  async function manageLanKey(action: 'copy' | 'rotate') {
    setKeyBusy(true)
    setKeyNotice('')
    try {
      const response = await fetch(`/console/api/lan-key/${action}`, {
        method: 'POST',
        ...(action === 'rotate' ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ acknowledge_revoke: true }) } : {}),
      })
      const data = await response.json() as (LanKeyInfo & { detail?: string })
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作失败，请检查本机服务。')
      if (action === 'copy') {
        setKeyNotice('密钥已复制到这台 Windows 的剪贴板。请粘贴到接入项目的密钥配置中。')
      } else {
        setKeyInfo(data)
        setConfirmRotate(false)
        setKeyNotice('已生成新密钥，旧密钥立即失效。请复制新密钥并更新所有接入项目。')
      }
    } catch (error) {
      setKeyNotice(error instanceof Error ? error.message : '操作失败，请检查本机服务。')
    } finally {
      setKeyBusy(false)
    }
  }

  const endpoints = integrationContracts(paths, lan)
  const contract = endpoints.find(endpoint => endpoint.id === selectedEndpoint) || endpoints[0]

  return <>
    <section className="panel integration-section"><h3>图片与视频接入速查 · 2026-10-24</h3><p>图片：直接 POST <code>{paths.jobs}</code>（kind=image、model=beluga、count=1），不要先请求报价，不传 dispatch_quote_id。图片报价返回422是本地能力限制，不是账号异常；图片任务不提供 max_credits 扣费封顶。</p><p>视频：也可直接提交同一个任务接口（kind=video、video_model=omni_flash；Veo Fast 使用 veo_3_1_fast、8s、720p）。可选的 dispatch-quotes 仅支持视频；它是估算，不是 Google 强制扣费上限。完整请求、quoteId 用法见下方文档。</p><p>只提交一次：auto_retry_limit=0、retry_uncertain=false、retry_switch_accounts=false。HTTP202只表示入队；用返回的 id 查询，成功后下载 result.files[].url。结果不明先查原任务，不重发。下方“一键复制文档”和“下载 .md 文件”提供完整说明。</p></section>
    <div className="page-title"><span className="eyebrow">INTEGRATION / FLOW AGENT</span><h1>接口接入</h1><p>让同电脑或局域网中的其他项目调用本机 Flow 能力。先读状态，再按需提交生成任务。</p></div>
    <section className="panel integration-document"><div><strong>完整接口文档 · Markdown</strong><p>包含接口协议、鉴权、请求体、返回体与接入示例，可直接交给其他项目使用。</p>{documentError && <p role="alert">{documentError}</p>}</div><div className="library-actions"><button className="secondary" disabled={!document} onClick={() => void copy(document, '完整 Markdown 接口文档已复制')}>一键复制文档</button><a className="secondary" href="/console/api/integration/document.md" download="FlowAgent_API.md">下载 .md 文件</a></div></section>
    <section className="panel integration-document"><div><strong>AI 接入 · MCP + Skill</strong><p>安装版已内置 MCP 程序。为 AI 客户端添加一次连接，再安装 Skill 即可跨项目使用；复制的配置默认只读，不会生成扣费。</p>{mcpError && <p role="alert">{mcpError}</p>}</div><div className="library-actions"><button className="secondary" disabled={!mcpConfig} onClick={() => void copy(mcpConfig, 'MCP 只读连接配置已复制')}>复制 MCP 配置</button><button className="secondary" disabled={!mcpGuide} onClick={() => void copy(mcpGuide, 'MCP 与 Skill 接入说明已复制')}>复制接入说明</button><a className="secondary" href="/console/api/integration/mcp/guide.md" download="Flow_MCP_Skill_接入说明.md">下载接入说明</a><a className="secondary" href="/console/api/integration/mcp/bundle.zip" download="Flow-MCP-Skill.zip">下载 MCP + Skill 接入包</a></div></section>
    <section className="panel integration-intro">
      <div><h3>1. 选择接入位置</h3><p>同电脑走本机地址；其他电脑走独立的带密钥 API。管理页面和 Google 会话不会开放到局域网。</p></div>
      <div className="segment integration-switch"><button type="button" className={!lan ? 'selected' : ''} onClick={() => setConnection('local')}>同电脑</button><button type="button" className={lan ? 'selected' : ''} onClick={() => setConnection('lan')}>同局域网</button></div>
      <div className="integration-address"><div><small>API 基础地址（供程序调用）</small><strong>{base}</strong></div><button className="secondary" onClick={() => void copy(base, '地址已复制')}>复制地址</button></div>
      {lan && <div className={`integration-state ${info?.lan_enabled ? 'ready' : 'not-ready'}`}>{info?.lan_enabled ? '局域网 API 已启用。' : '局域网 API 未启用：在本机运行 Start-FlowConsoleLan.cmd，或启动 EXE 时添加 --lan。'}密钥只保存在本机数据目录；请从 EXE 窗口显示的密钥文件读取，勿放入网页或提交到代码仓库。若接入电脑与显示地址不在同一网段，请用 ipconfig 查找正确的本机 IPv4。</div>}
      {!lan && <div className="integration-state ready">本机接口只监听 127.0.0.1，无需 API 密钥。上方地址供程序调用；在浏览器打开 <a href="/console/">中文管理页面 ↗</a>。</div>}
    </section>
    <section className="panel integration-section integration-key-panel"><div className="panel-head"><div><h3>局域网 API 密钥设置</h3><p>给其他项目接入时，使用下方地址和密钥。密钥只保存在本机，不会显示在网页或 API 返回体中。</p></div></div><div className="integration-key-details"><div><small>局域网接口</small><strong>{info?.lan_enabled ? `已启用 · ${lanBase}` : '未启用 · 请用 Start-FlowConsoleLan.cmd 启动'}</strong></div><div><small>密钥状态</small><strong>{keyInfo?.configured ? '已设置' : '未设置'}</strong></div><div><small>上次更新</small><strong>{keyInfo?.updated_at ? new Date(keyInfo.updated_at).toLocaleString('zh-CN') : '—'}</strong></div></div><p className="integration-key-path">密钥文件：<code>{keyInfo?.key_file || '读取中…'}</code></p><div className="integration-key-actions"><button type="button" className="secondary" disabled={!keyInfo?.configured || keyBusy} onClick={() => void manageLanKey('copy')}>复制当前密钥到剪贴板</button><button type="button" className="secondary" disabled={keyBusy} onClick={() => setConfirmRotate(true)}>重新生成密钥</button></div>{confirmRotate && <div className="integration-key-confirm" role="alert"><strong>重置后，旧密钥立即失效。</strong><span>所有已接入项目都会收到 401，直到改用新密钥。当前任务不会被重新提交。</span><div><button type="button" className="secondary" disabled={keyBusy} onClick={() => setConfirmRotate(false)}>取消</button><button type="button" className="primary" disabled={keyBusy} onClick={() => void manageLanKey('rotate')}>确认重置密钥</button></div></div>}{keyNotice && <p className="integration-key-notice" role="status">{keyNotice}</p>}</section>
    <div className="integration-grid">
      <section className="panel"><div className="panel-head"><div><h3>2. 验证连接与读取积分</h3><p>只读请求，不消耗 Flow 积分。</p></div><div className="segment integration-lang"><button type="button" className={language === 'powershell' ? 'selected' : ''} onClick={() => setLanguage('powershell')}>PowerShell</button><button type="button" className={language === 'python' ? 'selected' : ''} onClick={() => setLanguage('python')}>Python</button></div></div><CodeBlock value={language === 'powershell' ? readPowerShell : readPython} onCopy={() => void copy(language === 'powershell' ? readPowerShell : readPython, '读取示例已复制')} /></section>
      <section className="panel"><div className="panel-head"><div><h3>3. 提交并查询任务</h3><p>执行后可能消耗积分；请先填写提示词。</p></div></div><CodeBlock value={language === 'powershell' ? generatePowerShell : generatePython} onCopy={() => void copy(language === 'powershell' ? generatePowerShell : generatePython, '生成示例已复制')} /></section>
    </div>
    <section className="panel integration-section"><div className="panel-head"><div><h3>接口协议</h3><p>HTTP 接口；JSON 请求和响应使用 UTF-8。下方示例为字段结构示意，时间为带时区的 ISO 8601 字符串。</p></div>{!lan && <a className="text-link" href="/docs" target="_blank" rel="noreferrer">打开交互式 OpenAPI ↗</a>}</div><div className="integration-protocol"><div><strong>基础地址</strong><code>{base}</code></div><div><strong>鉴权</strong><span>{lan ? '每次请求带 Authorization: Bearer <API 密钥>' : '本机 127.0.0.1，无需密钥'}</span></div><div><strong>任务语义</strong><span>POST 返回 202 只代表已排队；用任务 ID 查询至最终状态</span></div><div><strong>错误格式</strong><code>{'{"detail":"错误原因"}'}</code></div></div>{lan && <p className="integration-foot">局域网 OpenAPI 地址：<code>{base}/api/v1/openapi.json</code>。读取时也需带密钥；浏览器地址栏不会自动附带密钥。仅在可信任的专用网络使用 HTTP。</p>}</section>
    <section className="panel integration-section"><div className="panel-head"><div><h3>接口清单与请求 / 返回体</h3><p>点击接口名称查看请求头、参数、返回结构和错误码。路径随上方“同电脑 / 同局域网”选择切换。</p></div></div><div className="table-wrap"><table><thead><tr><th>用途</th><th>方法</th><th>路径</th><th>返回 / 说明</th></tr></thead><tbody>{endpoints.map(endpoint => <tr key={endpoint.id} className={contract.id === endpoint.id ? 'integration-row-selected' : ''}><td><button className="integration-endpoint-button" type="button" onClick={() => setSelectedEndpoint(endpoint.id)} aria-pressed={contract.id === endpoint.id}>{endpoint.name} ↗</button></td><td><span className="method-pill">{endpoint.method}</span></td><td><code className="endpoint-path">{endpoint.path}</code></td><td>{endpoint.summary}</td></tr>)}</tbody></table></div><div className="integration-contract" aria-live="polite"><div className="integration-contract-head"><div><span className="method-pill">{contract.method}</span><code>{contract.path}</code><h4>{contract.name}</h4><p>{contract.summary}</p></div><span>{contract.contentType}</span></div><div className="integration-contract-grid"><div><div className="integration-contract-label"><strong>请求协议与请求体</strong><button type="button" className="text-link" onClick={() => void copy(contract.request, '请求示例已复制')}>复制</button></div><pre>{contract.request}</pre></div><div><div className="integration-contract-label"><strong>返回体示例</strong><button type="button" className="text-link" onClick={() => void copy(contract.response, '返回示例已复制')}>复制</button></div><pre>{contract.response}</pre></div></div><div className="integration-contract-notes"><p><strong>字段说明：</strong>{contract.fields}</p><p><strong>常见错误：</strong>{contract.errors} 参数校验错误的 <code>detail</code> 可能为字段错误数组。</p></div></div></section>
    <SchedulerSettings /><section className="panel integration-section"><h3>任务参数与结果</h3><div className="table-wrap"><table><thead><tr><th>字段</th><th>用途</th><th>填写方式</th></tr></thead><tbody><tr><td><code>kind</code>、<code>prompt</code></td><td>生成类型、提示词</td><td><code>image</code> 或 <code>video</code>；提示词 1–4000 字</td></tr><tr><td><code>aspect</code>、<code>count</code></td><td>比例、数量</td><td>视频 16:9 / 9:16；图片另支持 1:1、4:3、3:4；图片 count 固定 1</td></tr><tr><td><code>duration</code>、<code>quality</code></td><td>视频参数</td><td>4s / 6s / 8s / 10s；360p / 720p</td></tr><tr><td><code>model</code>、<code>account_id</code></td><td>图片模型、指定账号</td><td>模型：{imageModelProtocol}。{imageModelNote} 指定账号时优先使用该会话；不填则按 account_strategy 和并发设置调度</td></tr><tr><td><code>video_model</code></td><td>视频模型</td><td>omni_flash / veo_3_1_lite / veo_3_1_fast / veo_3_1_quality；Veo 固定 8s、720p，Quality 不支持参考图，传 veo 返回 422</td></tr><tr><td><code>account_strategy</code></td><td>账号积分策略</td><td>round_robin（默认，轮流）、highest_credits（最多优先）、lowest_credits（最少优先）；按足够的缓存积分选择初始会话，非扣费上限；同会话内切换仍由引擎控制</td></tr><tr><td><code>auto_retry_limit</code>、<code>retry_uncertain</code></td><td>失败自动重试</td><td>auto_retry_limit 为整数 0～3，默认0；3表示首次之外最多重试3次。retry_uncertain 默认false；true明确允许结果不确定时重新生成，可能重复扣积分。已知结果编号时优先恢复下载；登录、权限、积分不足及平台拒绝等停止重试。</td></tr><tr><td><code>result.auto_retry</code></td><td>重试进度与记录</td><td>包含次数、下一次时间 next_retry_at（UTC）、停止原因 stop_reason、history。等待期间仍为 queued，可取消；重启后不会补跑。</td></tr><tr><td><code>video_estimates</code>、<code>video_credit_pricing</code></td><td>账号余额的视频条数估算</td><td>账号响应含 Omni 各时长/画质与 Veo Lite/Fast/Quality 的非 Ultra/Ultra 场景条数；未知余额为 null；套餐未识别，两个模型条数不能相加</td></tr><tr><td><code>video_mode</code></td><td>视频模式</td><td>text / frames / references / edit；不填时按素材自动判断。首尾帧、参考图、视频编辑不能混用</td></tr><tr><td><code>start_image_id</code>、<code>end_image_id</code></td><td>首尾帧</td><td>首帧必填，尾帧可选。先上传图片，使用 image_id；或引用 media:文件名</td></tr><tr><td><code>reference_image_ids</code></td><td>参考图数组</td><td>按顺序填写 image_id，后台不额外限制张数，实际可用数量由 Flow 决定。count=1；Omni 省略 aspect / quality，由 Flow 决定；Veo Lite / Fast 固定 8s、720p，aspect 可选，Quality 不支持参考图</td></tr><tr><td><code>source_video_id</code></td><td>视频编辑原视频</td><td>上传 MP4 后填 video_id；count=1，省略 aspect / quality / duration，其默认值仅占位。仅编辑原视频，不是动作参考，也不能与图片混用</td></tr><tr><td><code>id</code>、<code>status</code>、<code>result.files</code></td><td>任务结果</td><td>提交返回 id；按 id 查询至 succeeded / failed；文件 URL 需拼接服务地址</td></tr></tbody></table></div><p className="integration-foot">局域网提交任务需另加请求头 <code>X-Flow-Allow-Charge: yes</code>。上传图片限 JPG / PNG / WebP，最大 20 MB；原视频限 MP4、最长 10 秒、最大 100 MB（本后台限制）。上传只保存本机，提交任务后才发送 Flow。排队任务可在本机“任务管理”取消，运行中的任务不能取消。</p></section>
    {copied && <div className="integration-copy-status" role="status">{copied}</div>}
  </>
}

function CodeBlock({ value, onCopy }: { value: string; onCopy: () => void }) {
  return <div className="code-box"><button className="secondary" type="button" onClick={onCopy}>复制代码</button><pre><code>{value}</code></pre></div>
}
