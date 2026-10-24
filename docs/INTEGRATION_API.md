# 0.2.39：Veo 接入

通过现有任务接口提交，鉴权与扣费确认头不变：

```json
{"kind":"video","prompt":"一只猫在窗边晒太阳","video_model":"veo_3_1_fast","duration":"8s","quality":"720p","aspect":"16:9","count":1,"account_id":"本地账号ID"}
```

返回 202 和 id，查询至 succeeded，从 result.files[].url 下载。未确认网页实际选择该模型时，提交前失败，不改用 Omni。自动账号调度按非 Ultra 价格保守估算，不猜套餐。当前 Veo 固定 8 秒、720p；Lite / Fast 支持参考图，Quality 不支持参考图，均不支持视频编辑。原 Omni 参数不变。

本轮包含隔离队列和真实网页不生成检查，未进行收费 Veo 出片验收。模型能力来源：[Google Flow 官方说明](https://support.google.com/flow/answer/16352836?hl=en)。当前网页实际固定参数优先于通用文档。

# Flow Agent 接口接入说明

## 0.2.37：完整文档下载与本地整体迁移

“接口接入”顶部提供“一键复制文档”和“下载 .md 文件”。`GET /console/api/integration/document.md` 返回程序随附的完整 Markdown，`Content-Type: text/markdown; charset=utf-8`，文件名 `FlowAgent_API.md`，内容包含本机和局域网协议，不包含真实密钥。此文件入口仅在本机管理接口提供。

整体迁移为本机管理能力，不向局域网 API 开放。所有端点前缀为 `/console/api/media/storage/migration`：

| 方法/后缀 | 请求体 | 成功返回 |
| --- | --- | --- |
| `POST /plan` | `{"directory":"E:\\FlowMedia"}` | `id`、`target`、`sources`、`total`、`total_bytes`、`free_bytes`、`renamed`、`conflicts`；仅预览，不移动文件 |
| `POST /start` | `{"plan_id":"预览返回的id","confirmed":true}` | HTTP 202，迁移状态；预览有效期15分钟，源文件变化需重新预览 |
| `GET` 无后缀 | 无 | 迁移状态 |
| `POST /resume` | 无 | HTTP 202，继续已确认但未完成的迁移；不会生成或扣积分 |
| `POST /abandon` | 无 | 仅在迁移已暂停或失败时可用：结束迁移并保留两边现有文件，不删除已复制文件 |

迁移状态示例（容量单位为字节，进度为文件数量）：

```json
{"id":"迁移编号","state":"running","pending":true,"phase":"copy","target":"E:\\FlowMedia","sources":["C:\\...\\output"],"total":46,"total_bytes":80000000,"copied":12,"removed":0,"renamed":0,"committed":false,"message":"正在复制并逐个校验，原素材暂不清理。"}
```

`state` 为 `idle/running/interrupted/needs_attention/succeeded/abandoned`；`phase` 为 `copy/cleanup/complete`。`committed=true` 表示新目录配置已持久化，此后恢复仅核验并清理原文件，不重复复制生成。`pending=true` 时生成提交、账号网页操作和目录切换返回 409，须先完成或结束迁移。未确认启动返回422；过期或变化的预览、忙碌任务、离线源目录、同名目标突然出现等返回409或进入 `needs_attention`，不会覆盖已有素材。

迁移范围仅为登记目录中的受支持图片、视频与提示词索引，平铺合并至目标目录；其他文件及子目录保留。同名素材自动改名保留两份，原索引在目标 `.flow-migration-编号` 归档。复制和SHA-256校验完成后，配置和旧下载映射在同一数据库事务中切换；源文件清理前再次校验。磁盘错误、退出或清理受阻可继续，原任务记录不重写。即使目标文件名相同，客户端也必须使用服务器返回的 `id/url`；旧链接通过持久化映射继续访问对应文件。

## 0.2.36：本地保存目录与多目录下载

保存目录通过本机后台“本地素材库”设置，重启后保留，应用于本机和局域网任务的后续输出。旧素材保留在原位置，继续可检索和下载。有排队、运行任务或网页检查时拒绝切换。以下管理端点仅供本机后台，不向局域网开放：

| 方法与地址 | 请求体 | 成功返回 |
| --- | --- | --- |
| `GET /console/api/media/storage` | 无 | 当前目录、默认目录及历史目录状态 |
| `POST /console/api/media/storage` | `{"directory":"E:\\FlowMedia"}` | 校验并保存后的目录状态 |
| `POST /console/api/media/storage/choose` | 无 | `{"directory":"E:\\FlowMedia","cancelled":false}`；仅选择，仍需保存 |

目录状态示例：

```json
{"directory":"E:\\FlowMedia","default_directory":"C:\\...\\output","directories":[{"id":"目录标识","directory":"E:\\FlowMedia","available":true,"active":true}],"moves_existing_files":false}
```

路径不合法或无法写入返回 422；任务繁忙、选择窗口已打开或目录存在其他索引返回 409；文件夹选择窗口不可用返回 503，可改为手动输入。取消选择返回 `{"directory":null,"cancelled":true}`，不保存设置。

下载地址仍为 `/api/v1/media/{filename}`。新目录素材的 URL 增加 `?storage=目录标识`，解决不同文件夹同名问题；请直接使用任务结果或素材列表返回的 `url` 和 `id`，不要自行拼接。原始目录下载地址继续兼容。局域网下载仍需 Bearer 密钥，不返回本地绝对路径或目录设置权限。

## 0.2.35：并发默认最大值

未保存设置时，全局生成并发上限默认为 16（现行最大值）；升级保留已保存的自定义值。`GET/POST /console/api/scheduler` 与带密钥的 `/api/v1/scheduler` 协议保持不变，`max_concurrency` 仍为 1～16 的整数。设置值持久化且即时生效；同账号、邮箱和浏览器资料互斥、原生桥接串行，调低不会中断运行中任务。设置接口不会创建生成任务。

## 0.2.31：失败后最多自动重试3次

创建任务新增可选字段（本机与局域网接口相同）：

| 字段 | 类型/默认值 | 说明 |
| --- | --- | --- |
| `auto_retry_limit` | 整数 0～3，默认 0 | 0 关闭；3 表示首次之外最多再尝试3次，总共最多4次。 |
| `retry_switch_accounts` | 布尔值，默认 false | true 启用安全换号重试：每次排除所有已尝试账号及同邮箱/同浏览器目录；无可用新账号即停止。已提交结果不明时只读查询原项目，不重复生成，即使 retry_uncertain=true 也不允许重复生成。 |
| `retry_uncertain` | 布尔值，默认 false | true 明确授权在已提交但结果无法确定时重新生成，可能重复扣积分；必须同时启用自动重试。 |

```json
{"kind":"video","prompt":"日出时的海面","count":1,"auto_retry_limit":3,"retry_uncertain":false,"retry_switch_accounts":true}
```

重试间隔为10、30、60秒。设置 `retry_switch_accounts:true` 时每次换未尝试的可用账号，保持原模型、素材及参数；最多首次加3次重试，账号不足提前停止。已提交但无结果编号时只读查询原账号原项目，不重新生成；无法严格关联结果则停止待核对。界面默认启用该安全策略。旧接口不传此字段时沿用首次账号。已识别到原生成结果编号时改为只读恢复下载，不重新生成。登录、权限、素材/参数、积分不足、人工验证及平台拒绝等问题停止自动重试。旧策略（`retry_switch_accounts=false`）保留 `retry_uncertain` 兼容参数；安全换号策略不接受结果不明时重新生成的授权。

等待重试仍为 `queued`，不占用生成并发槽，可在本机取消；下一次执行为 `running`。成功立即停止；耗尽次数为 `failed`。`result.auto_retry` 返回 `limit`、`attempts_started`、`attempts_completed`、`retries_started`、`next_retry_at`（UTC ISO时间）、`action`（generate/recover）、`stop_reason` 和 `history`（每次尝试的时间、错误及脱敏执行记录）。同一任务 ID 不变，不另外创建子任务。

服务重启时排队或运行中的任务仍标记 `interrupted`，不会自动补跑。历史失败任务不会被此功能重新执行。旧客户端不传新字段时不自动重试；新版界面默认勾选“失败后自动重试3次”，重复扣费授权单独勾选，提交前确认框展示最大次数及风险。局域网仍须 Bearer 密钥及 `X-Flow-Allow-Charge: yes`。

本机手动重试接口 `POST /console/api/jobs/{id}/retry` 可传 `acknowledge_possible_charge:true`、`auto_retry_limit:3`、`retry_uncertain:false`、`retry_switch_accounts:true`。它创建新的任务 ID 并通过 `retry_of` 关联原记录，新任务内仍最多尝试4次；不覆盖原失败记录。重试参数省略时沿用原任务设置。

以上为离线调度与接口验证，不代表 Google 真实重试出图/视频已经验收。重试不能替代修复官网响应解析问题。

## 0.2.28：自定义并发与一键准备全部账号

### 自定义并发

管理后台“总览 / 创建任务 / 任务管理 / 接口接入”提供“生成并发设置”。默认 1，可输入 1～16 的整数，保存到本机数据库，重启后保留；本机与局域网任务共用这一上限。调低不终止已运行任务，待占用降至新上限后再启动后续任务。

| 操作 | 本机接口 | 局域网接口 |
| --- | --- | --- |
| 查询并发与运行状态 | `GET /console/api/scheduler` | `GET /api/v1/scheduler` |
| 保存并发上限 | `POST /console/api/scheduler` | `POST /api/v1/scheduler` |

POST 请求体（JSON）：

```json
{"max_concurrency":2}
```

GET / POST 均返回 HTTP 200：

```json
{
  "max_concurrency":2,"min":1,"max":16,"running":1,"queued":3,
  "per_account_limit":1,"per_profile_limit":1,"legacy_bridge_limit":1,
  "running_tasks":[{"job_id":"任务UUID","account_id":"account_demo"}]
}
```

`max_concurrency` 为严格整数；0、17、小数、布尔值、字符串及额外字段均返回 422。局域网接口需要 Bearer 密钥，错误或缺失返回 401。修改此设置本身不会提交生成，但已获授权的排队任务将按新上限执行。

官方网页图片 2.1 和视频任务可以在不同空闲账号及不同浏览器资料上并发；同一个账号、同一已确认邮箱或同一资料保持互斥。等待同账号的任务保持 queued，不阻塞后面的其他空闲账号任务，仍可取消。未指定账号时按原 account_strategy 从空闲且符合余额条件的账号选择；没有空闲账号时等待，没有合格账号时明确失败。旧模型与视频编辑所用原生扩展桥接仍互斥，并且不会与官方网页生成重叠。每个任务保留自己的结果绑定、下载路径、进度及浏览器清理，不在失败后自动重试或换号。

并发设置不是 Google 配额承诺。当前只允许不消耗积分的本地验证；多 Google 账号同时真实生成的稳定性尚未验收。

### 一键准备全部账号（账号激活检查）

“激活”在此指检查已连接账号的扩展同步、身份及积分。不能自动登录、解决验证码、解除 Google 风控或保证生成权限；`generation_verified` 始终为 false。不创建 Google 账号、不提交生成、不扣费验证。

| 操作 | 本机接口 | 局域网接口 |
| --- | --- | --- |
| 开始准备全部账号 | `POST /console/api/accounts/prepare-all` | `POST /api/v1/accounts/prepare-all` |
| 查询逐账号进度 | `GET /console/api/accounts/preparation` | `GET /api/v1/accounts/preparation` |
| 当前账号结束后停止后续准备 | `POST /console/api/accounts/preparation/stop` | `POST /api/v1/accounts/preparation/stop` |

请求体均为空。局域网每次都需 Bearer 密钥；开始准备还必须带 `X-Flow-Allow-Interaction: yes`，表示知晓可能在运行服务的电脑打开浏览器，需要本人登录或验证。没有该头返回 422，不启动操作。

开始返回 HTTP 202，运行中的同一批次重复调用返回当前进度，不重复开批次。GET 返回 HTTP 200，结构示意：

```json
{
  "scope":"all","state":"running","account_id":"account_demo",
  "generation_verified":false,
  "batch":{
    "state":"running","total":3,"completed":0,"succeeded":0,"attention":0,"skipped":0,
    "stop_requested":false,"message":"正在按顺序检查全部账号",
    "accounts":[
      {"account_id":"account_demo","label":"账号名称","email":"user@example.com",
       "state":"running","credits":null,"checked_at":null,"steps":[],"message":"正在检查"}
    ]
  }
}
```

以上账号数组仅展示字段样例；实际返回全部账号，不省略失败项。`queued/running` 为等待或检查中，`checked` 为检查通过，`needs_attention` 为需要本人处理，`skipped` 为未检查。批次结束进入 checked 或 needs_attention。通过时按本机自动关闭设置清理本次打开的页面；需本人处理的页面按现有准备流程保留。停止只影响后续账号，不打断当前账号检查，不取消生成任务。

有 queued/running 生成任务、原生桥接/官方页面检查占用或其他单账号准备时返回 409；没有可打开的账号也返回 409。账号准备仍逐个执行，以免同一桥接连接串号；它不使用生成并发上限。

同电脑 PowerShell（不生成）：

```powershell
$base = 'http://127.0.0.1:8001'
Invoke-RestMethod "$base/console/api/scheduler" -Method Post -ContentType 'application/json' -Body '{"max_concurrency":2}'
Invoke-RestMethod "$base/console/api/accounts/prepare-all" -Method Post
Invoke-RestMethod "$base/console/api/accounts/preparation"
```

局域网 Python（不生成）：

```python
import os, requests
base = 'http://<本机局域网IPv4>:8002'
auth = {'Authorization': 'Bearer ' + os.environ['FLOW_AGENT_API_KEY']}
r = requests.post(base + '/api/v1/scheduler', headers=auth, json={'max_concurrency':2}, timeout=15)
r.raise_for_status()
r = requests.post(base + '/api/v1/accounts/prepare-all', headers={**auth,'X-Flow-Allow-Interaction':'yes'}, timeout=15)
r.raise_for_status()
r = requests.get(base + '/api/v1/accounts/preparation', headers=auth, timeout=15)
r.raise_for_status()
print(r.json())
```


## 官方视频页面检查（0.2.26，仅本机管理接口）

`POST /console/api/accounts/{account_id}/video-page-check`，JSON 请求体：

```json
{"aspect":"16:9","duration":"8s","quality":"720p","count":1}
```

字段取值与创建视频任务一致。检查账号、Omni 1.1 Flash 参数和按钮可点击性，但不点击生成；真实生成请求被阻断。成功返回 HTTP 200，包含 `page_ready:true`、`submit_control_actionable:true`、`generation_submitted:false`、`generation_requests:0`、`browser_closed:true`。失败返回 `409 {"detail":"中文原因"}`；已有任务或账号准备进行中时拒绝检查。不会返回 Cookie、Token 或会话数据。

0.2.26 后台文生视频、首尾帧、参考图任务使用官方网页，仅提交一次，等待本次媒体编号对应的原始 MP4；任务成功后从 `result.files[].url` 下载。部分下载后失败时，已保存文件也通过该字段返回，任务仍为 `failed`。Omni 参考图模式保留原接口语义，比例和画质由网页决定（Veo 例外见 0.2.39），有效参数记录在 `result.execution.parameters`。视频编辑仍使用原生链路。网页检查通过与本地回归均不代表 Google 真实视频出片已经验收。

## 浏览器验证检查（0.2.16，仅本机管理接口）

`POST /console/api/accounts/{account_id}/browser-check`，无请求体。
`account_id` 为账号列表中的公开本地标识。检查会暂时独占浏览器连接，
有生成任务或账号准备进行中时返回 409。

成功返回：
```json
{"browser_connected":true,"verification_client_loaded":true,"captcha_provider":"flow.captcha","token_minted":false,"generation_submitted":false}
```
失败返回 `409 {"detail":"中文原因"}`；不返回 Cookie、Token、会话内容或原始日志。
此检查不获取生成令牌、不提交生成，不能证明上游生成验证已通过。

后台生成改为严格绑定所选账号的真实浏览器，不回退 HTTP 验证，也不自动
重复提交或更换账号补试。原生同步 REST 与后台队列共用互斥桥接连接；官方网页任务按自定义并发与账号隔离调度。
原生 CLI 的旧验证模式保留；本地后台不执行 `--all` 多账号生成。


在浏览器打开 `http://127.0.0.1:8001/console/#integration`，可直接复制 PowerShell / Python 示例。推荐外部项目通过后台任务队列调用；任务按全局自定义并发上限和账号互斥规则执行（默认 1）。原项目同步 REST、CLI、MCP 入口保持可用。

## 选择调用地址

| 调用位置 | 基础地址 | 鉴权 | 启动方式 |
| --- | --- | --- | --- |
| 同电脑 | `http://127.0.0.1:8001` | 无；仅绑定本机 | 双击 `dist/FlowAgentConsole.exe` |
| 同局域网 | `http://<本机局域网IPv4>:8002` | 每次请求带 `Authorization: Bearer <密钥>` | 双击 `Start-FlowConsoleLan.cmd`，或运行 EXE 加 `--lan` |

局域网密钥由 EXE 首次启用 `--lan` 时生成，位置会显示在 EXE 窗口；仓库内运行默认是 `flow-agent/data/lan_api_key.txt`。该文件不由网页或 API 返回。仅向可信任的接入项目提供密钥；不要提交到 Git。Windows 防火墙只允许 TCP 8002 的专用网络入站。局域网接口使用 HTTP，跨不可信网络需另外配置 VPN 或 HTTPS。

在本机管理页面的 **接口接入 → 局域网 API 密钥设置** 中，可查看启用状态、地址和密钥文件位置，点击“复制当前密钥到剪贴板”后粘贴到接入项目的密钥配置。网页/API 不显示或返回密钥。需要撤销旧密钥时，点击“重新生成密钥”并再次确认；旧密钥随即失效，所有接入项目必须更新。不要在接入项目的前端代码中保存密钥。

## 接口清单

| 用途 | 同电脑路径 | 同局域网路径 | 说明 |
| --- | --- | --- | --- |
| 连通检查 | `GET /health` | `GET /api/v1/health` | 返回服务状态 |
| 账号与缓存积分 | `GET /console/api/accounts` | `GET /api/v1/balance` | 只读缓存；不会触发上游刷新 |
| 创建任务 | `POST /console/api/jobs` | `POST /api/v1/jobs` | 返回 HTTP 202 与任务 `id`；可能消耗积分 |
| 查询任务 | `GET /console/api/jobs/{id}` | `GET /api/v1/jobs/{id}` | 返回状态、错误和结果 |
| 最近任务 | `GET /console/api/jobs` | `GET /api/v1/jobs` | 最近 100 条 |
| 素材列表 | `GET /console/api/media` | `GET /api/v1/media` | 支持 `search` 和 `kind=all/image/video` |
| 下载素材 | `GET /api/v1/media/{文件名}` | 同左 | 二进制文件响应 |
| 上传参考图片 | `POST /console/api/uploads/image` | `POST /api/v1/uploads/image` | `multipart/form-data` 的 `file` 字段，限 JPG/PNG/WebP、20 MB |
| OpenAPI | `GET /openapi.json` | `GET /api/v1/openapi.json` | 局域网规范也需要密钥 |

局域网 API 不提供管理页面、Cookie、Token 或原版无鉴权 REST 路由；仅新增受密钥和明确交互请求头保护的批量账号准备入口，不远程代操作登录或验证。局域网的 `POST /api/v1/jobs` 还必须带 `X-Flow-Allow-Charge: yes`；没有此头会返回 HTTP 422。本机任务接口不会要求此头，但同样可能消耗积分。不要通过局域网直接调用本机的原版 `/api/v1/video` 或 `/api/v1/image`。

## 通用接口协议

- 传输：HTTP；JSON 请求使用 `Content-Type: application/json`，文本按 UTF-8 编码。上传图片使用 `multipart/form-data`，下载返回文件二进制。
- 局域网鉴权：每次请求都带 `Authorization: Bearer <密钥>`，包括读取 OpenAPI 规范；同电脑 8001 端口无需密钥。浏览器地址栏不会自动添加密钥。
- 时间：`created_at`、`started_at`、`finished_at`、`last_refresh_at` 等为带时区的 ISO 8601 字符串，尚无值时为 `null`。`elapsed_seconds` 单位是秒，文件 `size` 单位是字节。
- 错误：通常返回 `{"detail":"原因"}`；Pydantic 参数校验失败时，`detail` 可能是字段错误数组。应同时检查 HTTP 状态码。
- 异步任务：提交返回 HTTP 202 仅表示进入队列，之后通过任务 ID 轮询；只有 `status=succeeded` 且 `result.files` 包含文件才代表得到成品。

## 创建任务

JSON 请求体示例：

```json
{
  "kind": "video",
  "prompt": "日出时的海面，广角镜头",
  "aspect": "16:9",
  "duration": "8s",
  "quality": "720p",
  "video_model": "omni_flash",
  "account_strategy": "round_robin",
  "count": 1
}
```

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `kind` | `image`、`video` | 必填 |
| `prompt` | 1–4000 字符 | 必填 |
| `aspect` | 视频 `16:9`、`9:16`；图片另可 `1:1`、`4:3`、`3:4` | 默认 `16:9` |
| `count` | 视频 1–4；图片固定 1 | 默认 1 |
| `model` | `beluga`、`narwhal`、`harbor_seal`、`gem_pix_2` | 对应 Nano Banana 2.1、旧版 Nano Banana 2、Nano Banana 2 Lite、Nano Banana Pro。API 省略参数仍默认 `narwhal` 以保留原接口；新版账号请显式传 `beluga`，界面默认选 2.1 |
| `duration` | `4s`、`6s`、`8s`、`10s` | 视频时长，默认 `8s` |
| `quality` | `360p`、`720p` | 视频画质，默认 `720p` |
| `account_id` | 账号列表中的 `id` | 可选；不填时按策略选择空闲账号 |
| `start_image_id` | 上传接口返回的 `image_id`，或 `media:文件名` | 可选；仅图生视频 |
| `media_rights_confirmed` | 布尔值，默认 `false` | 明确确认有权使用本次参考图，允许自动确认 Google 图片使用权。按图片内容保存授权；换图需重新确认，不代替登录、验证码或扣费确认 |


旧型号按[已观察到的 Flow 模型映射](https://github.com/crisng95/flowkit/blob/main/docs/IMAGE_API.md)展示。2.1 的 `BELUGA` 映射已与本机账号实时目录核对，相关协议变更见[实现方问题记录](https://github.com/TheSmallHanCat/flow2api/issues/187)。图片提交前使用所选会话只读查询模型目录；型号不可用或目录不可读时不调用生成，不自动换型号或重试。目录支持不代表生成必定通过；上游仍会独立审核请求。底层原生引擎对空响应可能内置一次重发，后台不额外自动重试。

提交后返回 `id`、`status`、`created_at` 等字段。用 `GET .../jobs/{id}` 查询，状态可能是 `queued`、`running`、`succeeded`、`failed`、`interrupted`、`canceled`。成功时 `result.files[].url` 是以 `/` 开头的相对路径，需与对应基础地址拼接后下载；失败原因在 `error`。`interrupted` 表示服务重启后结果未确认，先核对素材和积分，勿直接重试。只有排队中的任务能在管理页面取消。

### 创建任务的 HTTP 202 返回体

下例使用示意 ID 和时间。`request` 会回显经过默认值补全的请求；任务尚未完成时 `result`、`error`、`started_at`、`finished_at` 均为 `null`。

```json
{
  "id": "任务UUID", "kind": "video", "status": "queued",
  "request": {
    "kind": "video", "prompt": "日出时的海面", "aspect": "16:9",
    "count": 1, "model": "narwhal", "duration": "8s", "quality": "720p",
    "account_id": null, "start_image_id": null
  },
  "result": null, "error": null,
  "created_at": "2026-10-13T02:00:00+00:00",
  "started_at": null, "finished_at": null, "retry_of": null
}
```

### 查询任务的 HTTP 200 返回体

`GET /console/api/jobs/{id}` 和 `GET /api/v1/jobs/{id}` 返回同一结构；`GET .../jobs` 返回最多 100 个这种对象组成的数组。

```json
{
  "id": "任务UUID", "kind": "video", "status": "succeeded",
  "request": {
    "kind": "video", "prompt": "日出时的海面", "aspect": "16:9",
    "count": 1, "model": "narwhal", "duration": "8s", "quality": "720p",
    "account_id": null, "start_image_id": null
  },
  "result": {
    "engine_job_id": "引擎任务ID", "status": "succeeded",
    "account_id": "account_demo", "files": [
      {"name": "video.mp4", "url": "/api/v1/media/video.mp4"}
    ], "elapsed_seconds": 60.5
  },
  "error": null,
  "created_at": "2026-10-13T02:00:00+00:00",
  "started_at": "2026-10-13T02:00:01+00:00",
  "finished_at": "2026-10-13T02:01:01+00:00", "retry_of": null
}
```

`error` 在失败时是可展示的中文原因；`retry_of` 在重试任务中是原任务 ID。`elapsed_seconds` 或 `account_id` 可能为 `null`。`files[].url` 是相对路径，例如同电脑下载使用 `http://127.0.0.1:8001/api/v1/media/video.mp4`。

## 其他接口的请求体与返回体

| 接口 | 请求 | HTTP 200 返回体 | 主要错误 |
| --- | --- | --- | --- |
| 健康检查 | GET；无请求体 | 本机：`{"status":"ok","engine":true,"binary":"..."}`；局域网不含 `binary` | 局域网密钥错误：401 |
| 账号与积分 | GET；无请求体 | `{"accounts":[{"id":"account_demo","label":"账号名称","email":"user@example.com","credits":100,"status":"active","session":"local_file","selectable":true,"can_open_browser":true,"session_saved_at":"2026-10-13T02:00:00+00:00"}],"total_credits":100,"unread":0,"last_refresh_at":"2026-10-13T02:00:00+00:00"}` | 局域网密钥错误：401 |
| 素材列表 | GET；查询参数 `search` 可选、`kind=all/image/video`，默认 `all`；无请求体 | `[ {"id":"video.mp4","name":"video.mp4","kind":"video","size":1234567,"created_at":"2026-10-13T02:00:00+00:00","url":"/api/v1/media/video.mp4"} ]`；最多 200 个 | `kind` 无效：422 |
| 下载素材 | GET；文件名使用任务或素材列表返回的 `url`；无请求体 | MP4/JPG/PNG/WebP 文件二进制，不是 JSON | 文件不存在：404 |
| 上传参考图片 | POST；`multipart/form-data`，字段名 `file`，JPG/PNG/WebP，最大 20 MB | `{"image_id":"upload:随机文件名.png","name":"原始文件名.png"}` | 超过大小：413；格式错误：422 |

账号 `credits`、`total_credits`、`last_refresh_at` 可能为 `null`；`unread` 是未知积分的账号数。API 不返回 Cookie 或 Token。素材 `size` 单位为字节。上传返回的 `image_id` 原样传给视频任务的 `start_image_id`；也可传 `media:文件名` 引用已保存图片。

## 同电脑 PowerShell 示例

先用只读请求验证：

```powershell
$base = 'http://127.0.0.1:8001'
Invoke-RestMethod "$base/console/api/accounts"
```

以下请求会提交生成任务，可能消耗积分：

```powershell
$body = @{ kind = 'video'; prompt = '日出时的海面'; aspect = '16:9'; duration = '8s'; quality = '720p' } | ConvertTo-Json
$job = Invoke-RestMethod "$base/console/api/jobs" -Method Post -ContentType 'application/json' -Body $body
Invoke-RestMethod "$base/console/api/jobs/$($job.id)"
```

## 同局域网 Python 示例

在接入项目中安装 `requests`，通过环境变量传密钥，避免写进源码。以下只读请求不会消耗积分：

```python
import os
import requests

base = "http://<本机局域网IPv4>:8002"
headers = {"Authorization": f"Bearer {os.environ['FLOW_AGENT_API_KEY']}"}
response = requests.get(base + "/api/v1/balance", headers=headers, timeout=10)
response.raise_for_status()
print(response.json())
```

生成时向 `POST /api/v1/jobs` 发送上述 JSON，并加 `X-Flow-Allow-Charge: yes`，用返回的 `id` 查询任务；仅在业务明确同意积分消耗后调用。上传图生视频素材使用 `files={"file": open("图片.png", "rb")}` 调用 `/api/v1/uploads/image`，再传返回的 `image_id`。

## 常见响应

| HTTP 状态 | 含义 |
| --- | --- |
| 200 | 查询成功 |
| 202 | 任务已经加入队列，尚未代表生成成功 |
| 401 | 局域网密钥缺失或错误 |
| 404 | 任务、素材或该入口不存在 |
| 422 | 参数不符合约束，或局域网生成未确认可能消耗积分 |
| 409 | 本机取消非排队任务，或重试不符合状态要求 |
| 413 | 上传图片超过 20 MB |

生成任务在后台失败时，查询任务一般仍返回 HTTP 200，此时应检查 `status=failed` 与 `error`；不要只根据查询接口的 HTTP 200 判断生成成功。

本地健康检查、单元测试和接口返回 202 都不等于付费生成成功。实际效果应以任务最终状态与下载到的成品为准。


## 0.2.6 视频模型与账号积分策略

`video_model` 默认 `omni_flash`，另支持 `veo_3_1_lite`、`veo_3_1_fast`、`veo_3_1_quality`。Veo 固定 8s / 720p，支持横竖屏、文生、首尾帧；Lite / Fast 支持参考图，Quality 不支持参考图；均不支持编辑。模糊值 `veo` 仍返回 422。

`account_strategy` 在未指定 `account_id` 时生效，默认 `round_robin`：

| 值 | 行为 |
| --- | --- |
| `round_robin` | 缓存余额足够的账号依次轮流使用；服务重启后顺序重置 |
| `highest_credits` | 优先缓存积分最多的账号；相同积分按本地账号 ID 排序 |
| `lowest_credits` | 优先缓存积分最少但足够的账号；相同积分按本地账号 ID 排序 |

所有策略均服从自定义并发上限、账号和资料互斥，排除会话不完整、积分未知、非活跃账号。图片仅要求缓存积分大于 0；视频参考打包引擎估算值（4/6/8/10 秒的 360p 单条分别 4/5/6/7，720p 分别 7/10/12/15，乘以 count）。这不是 Google 价格承诺或扣费上限；真实校验与扣费由 Flow 决定。缓存可能过期，必要时先在账号管理刷新。没有符合条件的账号时任务失败且不提交生成，`error` 提示刷新或指定账号。

指定 `account_id` 优先，使用对应会话、不按策略另选文件；同一 Google 会话内的账号切换仍由底层引擎控制，因此不能保证硬性扣费归属/预算隔离。不提供免费积分/付费积分优先，因为上游数据未区分这些余额。不会在失败后自动换账号重试。

执行开始后 `request.selected_account_id` 记录选择的本地账号 ID（不含会话路径），`request.account_strategy` 保留原策略；`result.account_id` 是引擎返回的实际账号标识，可能为 null，且与本地 ID 命名不同。失败记录也保留所选 ID。重试重新按原参数调度，不沿用上次自动选择结果。


### 积分换算为视频条数

账号列表的每个账号新增 `video_estimates`：

```json
{
  "omni_flash": {"8s": {"720p": 87, "360p": 175}},
  "veo_3_1": {"fast": {"non_ultra": 52, "ultra": 105}}
}
```

上例为 1050 积分的部分字段；真实响应包含所有 Omni 时长与画质，以及 Veo Lite/Fast/Quality。账号余额未知时各计数字段为 null。条数为向下取整的单条视频数量，不是请求次数；两个模型共享余额，不能相加。合计按账号分别取整再求和，不把余数跨账号合并。

顶层 `video_credit_pricing` 返回规则、`source_url`、`checked_on`。规则来自 [Google 官方积分说明](https://support.google.com/flow/answer/16526234?hl=zh-Hans)，核对日期 2026-10-15。Veo 按非 Ultra/Ultra 两个场景估算，不表示已经识别套餐；生成参数见 video_model 说明。Ultra 低优先级零积分模式不做有限数量换算。


## 0.2.7 参考图、首尾帧与视频编辑

| 用途 | 本机路径 | 局域网路径 |
| --- | --- | --- |
| 上传图片 | POST /console/api/uploads/image | POST /api/v1/uploads/image |
| 上传原视频 | POST /console/api/uploads/video | POST /api/v1/uploads/video |
| 预览已上传文件 | GET /console/api/inputs/{name} | GET /api/v1/inputs/{name} |

上传使用 multipart/form-data 的 `file` 字段。上传只保存本机，不调用 Google；提交任务后才向 Flow 上传。局域网上传及预览均需 Bearer 密钥。

图片上传返回 `image_id`、`name`、`url`。视频上传返回：

```json
{"video_id":"upload:随机文件名.mp4","name":"原视频.mp4","url":"/console/api/inputs/随机文件名.mp4","duration_seconds":8.0,"size":1234567}
```

`duration_seconds` 单位为秒、`size` 为字节。局域网响应 url 前缀是 `/api/v1/inputs/`。原视频本版只接入 MP4，最长 10 秒、最大 100 MB（后台限制）；格式/时长校验失败返回 422，超大文件返回 413。校验容器和视频轨道不保证所有编码可解码。

任务示例（提交会消耗积分，请先取得业务授权）：

```json
{"kind":"video","prompt":"从首帧平滑过渡到尾帧","video_mode":"frames","start_image_id":"upload:首帧.png","end_image_id":"upload:尾帧.png","aspect":"9:16","quality":"720p","duration":"8s"}
```

```json
{"kind":"video","prompt":"保持人物与物品外观一致，在街道行走","video_mode":"references","reference_image_ids":["upload:人物.png","upload:物品.png"],"duration":"8s","count":1}
```

```json
{"kind":"video","prompt":"将背景改为雪景","video_mode":"edit","source_video_id":"upload:原视频.mp4","count":1}
```

- `video_mode` 可为 text/frames/references/edit；省略时按素材推断，原 start_image_id 调用兼容。
- 首帧必填，尾帧可选。参考图数组按顺序传递、不允许重复，后台不额外限制张数，实际可用数量由 Flow 决定；多图真实生成尚待验证。
- 参考图、首尾帧、原视频不可混用，图片任务不接受这些视频素材字段。
- 参考图/视频编辑每次仅 1 条。Omni 参考图与视频编辑不指定 aspect/quality，默认 16:9/720p 仅作旧协议占位，输出由 Flow 决定。Veo Lite / Fast 参考图可指定 aspect，固定 8s/720p；Quality 不支持参考图。
- 视频编辑也不指定 duration，默认 8s 仅占位、不传引擎；显式传其他值返回 422。Omni 参考图时长可选 4s/6s/8s/10s，Veo 参考图固定 8s。
- source_video_id 是待编辑原片，不是动作参考视频。当前引擎接入不支持视频参考与图片混合生成。已有本地素材可用 media:文件名，类型与相同限制仍会校验。
- 编辑任务自动调度按 40 积分检查缓存余额，来源为 [Google 官方积分说明](https://support.google.com/flow/answer/16526234?hl=en)（2026-10-17 核对），真实扣费由 Flow 决定。无失败自动换号重试。

真实 Google 生成/编辑尚未执行，不把离线替身引擎测试或本地上传成功视作成片验证。


## 0.2.34：素材提示词检索与本地索引

原素材列表接口与下载 URL 保持兼容：`GET /console/api/media`（本机）或 `GET /api/v1/media`（局域网，仍需 Bearer 密钥）。`search` 现在匹配提示词、文件名、模型及任务编号，空格分隔多个词时同时匹配；`kind=all/image/video`，列表最多 200 项，先全库筛选再截取。

每个素材额外返回 `title`、`prompt`、`task_id`、`model`、`parameters`、`index_status`、`source`、`generation_created_at`、`task_status`。`index_status` 为 `indexed`、`missing_prompt`、`ambiguous` 或 `file_changed`；无法确认原文时 `prompt=null`。原 `id/name/kind/size/created_at/url` 含义不变，size 单位字节；参数中的 duration 保留明确单位，如 `4s`。

本机独有：`GET /console/api/media/library?search=猫&kind=image&limit=60&offset=0` 返回 `{directory,index_path,total,indexed,matching,items,limit,offset}`，limit 范围 1–200；`GET /console/api/media/index` 下载 JSON 索引；`POST /console/api/media/open-folder` 无请求体，仅打开固定素材目录，不接受自定义路径。上述管理入口不向局域网开放。所有这些操作不提交生成、不消耗积分。

索引文件 `output/flow-media-index.json` 使用 `schema_version:1` 与 `generator:flow-agent-console`；包含 `items` 数组，按原文件名与相对 URL 关联素材，可连同文件一起备份。未修改原始媒体文件，不用提示词作为 Windows 文件名。



## 0.2.56：结果不明时隐藏查询180秒

生成仍只提交一次。提交后未取得明确结果时，保留本次专用浏览器，在后台隐藏查询最多180秒，不再次点击生成，不换账号重新生成。迟到的原生成响应提供有效结果ID后，仅校验和下载原结果；下载完成即关闭浏览器。

180秒内仍无法严格关联结果时，本次浏览器关闭，任务界面显示“待核对”。为兼容现有API，任务status仍为failed，result.execution.outcome为pending_review；uncertainty_query_completed为true，uncertainty_wait_seconds为180。自动重试在此处停止，包括旧retry_uncertain授权也不会重复提交。已保存的部分成品保留，不补生成。

本次更新只做离线与本地真实浏览器验证，不产生Google Flow生成请求。


## 0.2.57：官方错误诊断（不产生生成请求）

result.execution.generation_response保留原rpc_error_codes，并增加rpc_errors和diagnostic_format=allowlisted_reasons_v1。每个rpc_errors元素包含数字code、固定白名单reason_codes和reason_status。只有官方错误字段中明确出现的原因才归类；数字7本身不映射为任何原因。reason_status=not_provided_or_unrecognized表示没有说明或无法安全识别，不表示已经找到根因。

诊断不保存响应原文、请求头、Cookie、Token、链接、邮箱、提示词或不认识的字符串。任务错误信息显示固定中文分类。旧历史原文已丢失时不能还原，不回写猜测原因。保持180秒隐藏查询、防重复提交、下载验证及自动关闭策略。
