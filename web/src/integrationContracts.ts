export type EndpointId = 'health' | 'balance' | 'submit' | 'job' | 'jobs' | 'media' | 'download' | 'upload' | 'uploadVideo' | 'scheduler' | 'configureScheduler' | 'prepareAll' | 'preparation' | 'stopPreparation'

export type EndpointContract = {
  id: EndpointId
  name: string
  method: 'GET' | 'POST'
  path: string
  summary: string
  request: string
  response: string
  fields: string
  errors: string
  contentType: string
}

type Paths = { scheduler: string; prepareAll: string; preparation: string; health: string; balance: string; jobs: string; media: string; upload: string; uploadVideo: string; download: string }

export function integrationContracts(paths: Paths, lan: boolean): EndpointContract[] {
  const auth = lan ? 'Authorization: Bearer <API 密钥>\n' : ''
  const queuedJob = `{
  "id": "任务UUID",
  "kind": "video",
  "status": "queued",
  "request": {
    "kind": "video", "prompt": "日出时的海面", "aspect": "16:9",
    "count": 1, "model": "narwhal", "duration": "8s", "quality": "720p",
    "video_model": "omni_flash", "account_strategy": "round_robin",
    "account_id": null, "start_image_id": null
  },
  "result": null, "error": null,
  "created_at": "2026-10-13T02:00:00+00:00",
  "started_at": null, "finished_at": null, "retry_of": null
}`
  return [
    {
      id: 'scheduler', name: '查询并发设置', method: 'GET', path: paths.scheduler,
      summary: '查询已配置上限、运行数和排队数；不发起生成。',
      request: `GET ${paths.scheduler} HTTP/1.1\n${auth}请求体：无`,
      response: '{"max_concurrency":2,"min":1,"max":16,"running":1,"queued":3,"per_account_limit":1,"per_profile_limit":1,"legacy_bridge_limit":1,"running_tasks":[{"job_id":"任务UUID","account_id":"account_demo"}]}',
      fields: 'max_concurrency：本机全局生成任务上限；同账号、相同邮箱或浏览器资料不并行；原生桥接保持互斥。',
      errors: lan ? '401：密钥缺失或错误。' : '无法连接：确认程序版本。', contentType: '200 · application/json',
    },
    {
      id: 'configureScheduler', name: '设置并发数量', method: 'POST', path: paths.scheduler,
      summary: '持久保存并发上限，首次默认 16，已有自定义保留，可设置 1～16；不会提交生成。',
      request: `POST ${paths.scheduler} HTTP/1.1\n${auth}Content-Type: application/json\n\n{"max_concurrency":2}`,
      response: '{"max_concurrency":2,"min":1,"max":16,"running":0,"queued":0,"per_account_limit":1,"per_profile_limit":1,"legacy_bridge_limit":1,"running_tasks":[]}',
      fields: 'max_concurrency 必须是整数；调低不会中断已运行任务，之后按新上限调度。实际并发受可用账号数限制。',
      errors: `${lan ? '401：密钥错误；' : ''}422：不是 1～16 的整数或存在多余字段。`, contentType: '200 · application/json',
    },
    {
      id: 'prepareAll', name: '一键准备全部账号', method: 'POST', path: paths.prepareAll,
      summary: '逐个准备已连接账号，核对同步、身份和积分，不提交生成。需要本人验证时在本机处理。',
      request: `POST ${paths.prepareAll} HTTP/1.1\n${auth}${lan ? 'X-Flow-Allow-Interaction: yes\n' : ''}请求体：无`,
      response: '{"scope":"all","state":"running","generation_verified":false,"batch":{"state":"running","total":3,"completed":0,"succeeded":0,"attention":0,"skipped":0,"accounts":[{"account_id":"account_demo","label":"账号名称","email":"user@example.com","state":"queued","credits":null,"steps":[],"message":"等待检查","checked_at":null}]}}',
      fields: '202 只代表开始准备。不能自动登录、绕过验证码或保证生成权限；通过 preparation 接口查询到 checked 或 needs_attention。重复调用运行中的批次返回当前进度，不另开一批。',
      errors: `${lan ? '401：密钥错误；422：缺少 X-Flow-Allow-Interaction: yes；' : ''}409：有生成任务、其他账号准备或没有可打开的账号。`, contentType: '202 · application/json',
    },
    {
      id: 'preparation', name: '查询账号准备进度', method: 'GET', path: paths.preparation,
      summary: '返回每个账号的进度、身份积分检查结果与清理状态。',
      request: `GET ${paths.preparation} HTTP/1.1\n${auth}请求体：无`,
      response: '{"scope":"all","state":"needs_attention","generation_verified":false,"batch":{"state":"needs_attention","total":3,"completed":3,"succeeded":2,"attention":1,"skipped":0,"accounts":[{"account_id":"account_demo","label":"账号名称","state":"needs_attention","message":"需要本人登录或验证","credits":null,"steps":[]}]}}',
      fields: 'batch.accounts 返回全部账号，不省略失败项。checked 为身份积分检查通过，needs_attention 需要本人处理，skipped 未检查；generation_verified 始终为 false。成功准备后按本机自动关闭设置清理本次页面，需处理页面保留给本人。',
      errors: lan ? '401：密钥缺失或错误。' : '无法连接：确认服务。', contentType: '200 · application/json',
    },
    {
      id: 'stopPreparation', name: '停止后续账号准备', method: 'POST', path: `${paths.preparation}/stop`,
      summary: '当前账号检查结束后停止，后续账号标为 skipped；不会停止生成任务。',
      request: `POST ${paths.preparation}/stop HTTP/1.1\n${auth}请求体：无`,
      response: '{"scope":"all","state":"running","batch":{"stop_requested":true,"message":"将在当前账号检查结束后停止，后续账号不会打开。"}}',
      fields: '通过 preparation 继续查询最终进度；没有正在运行的批次时返回当前状态。',
      errors: lan ? '401：密钥缺失或错误。' : '无法连接：确认服务。', contentType: '200 · application/json',
    },
    {
      id: 'health', name: '连通检查', method: 'GET', path: paths.health,
      summary: '检查服务是否在线；不会生成内容或消耗积分。',
      request: `GET ${paths.health} HTTP/1.1\n${auth}请求体：无`,
      response: lan ? '{"status":"ok","engine":true}' : '{"status":"ok","engine":true,"binary":"<本机引擎路径>"}',
      fields: 'status：ok 表示服务已响应；engine：底层引擎文件是否可用。基础地址与端口按所选接入位置填写。',
      errors: lan ? '401：缺少密钥或密钥错误。' : '服务无法连接：确认 EXE 正在运行。', contentType: '200 · application/json',
    },
    {
      id: 'balance', name: '账号与缓存积分', method: 'GET', path: paths.balance,
      summary: '读取本地缓存的账号与积分；不会主动刷新 Google 积分。',
      request: `GET ${paths.balance} HTTP/1.1\n${auth}请求体：无`,
      response: `{
  "accounts": [{
    "id": "account_demo", "label": "账号名称", "email": "user@example.com",
    "credits": 100, "status": "active", "session": "local_file",
    "selectable": true, "can_open_browser": true,
    "session_saved_at": "2026-10-13T02:00:00+00:00"
  }],
  "total_credits": 100, "unread": 0,
  "last_refresh_at": "2026-10-13T02:00:00+00:00"
}`,
      fields: 'credits、total_credits、last_refresh_at 可能为 null；unread 是未读到积分的账号数。id 可传给创建任务的 account_id。不会返回 Cookie 或 Token。',
      errors: lan ? '401：缺少密钥或密钥错误。' : '服务无法连接：确认 EXE 正在运行。', contentType: '200 · application/json',
    },
    {
      id: 'submit', name: '提交生成任务', method: 'POST', path: paths.jobs,
      summary: '将图片或视频任务放入队列。可用 auto_retry_limit:3 启用最多3次自动重试；retry_uncertain:true 允许结果不确定时再次生成，可能重复扣积分。HTTP 202 仅表示已排队。',
      request: `POST ${paths.jobs} HTTP/1.1\n${auth}${lan ? 'X-Flow-Allow-Charge: yes\n' : ''}Content-Type: application/json\n\n${JSON.stringify({ kind: 'video', prompt: '日出时的海面', aspect: '16:9', duration: '8s', quality: '720p', video_model: 'omni_flash', account_strategy: 'round_robin', count: 1 }, null, 2)}`,
      response: queuedJob,
      fields: '必填：kind（image/video）、prompt（1–4000 字符）。可选：aspect、count、model、duration、quality、video_model、account_strategy、account_id、video_mode。首尾帧：start_image_id 与可选 end_image_id；参考图：reference_image_ids 数组（至少 1 张）；视频编辑：source_video_id。三类素材不能混用。参考图和编辑固定单条。Omni 参考图不指定比例或画质；Veo Lite / Fast 参考图固定 8s、720p，可选比例，Quality 不支持参考图。Veo 支持 veo_3_1_lite / veo_3_1_fast / veo_3_1_quality，固定 8s、720p。编辑仅 Omni，不指定时长、比例、画质。详见下方“任务参数”。',
      errors: `${lan ? '401：密钥错误；' : ''}422：字段不合法、账号或起始图片不存在${lan ? '，或缺少 X-Flow-Allow-Charge: yes' : ''}。`, contentType: '202 · application/json',
    },
    {
      id: 'job', name: '查询单个任务', method: 'GET', path: `${paths.jobs}/{任务ID}`,
      summary: '按提交时返回的 id 查询最终状态、失败原因和文件。建议间隔数秒轮询。',
      request: `GET ${paths.jobs}/任务UUID HTTP/1.1\n${auth}请求体：无；路径中的任务UUID替换为提交接口返回的 id。`,
      response: `{
  "id": "任务UUID", "kind": "video", "status": "succeeded",
  "request": { "kind": "video", "prompt": "日出时的海面", "aspect": "16:9",
    "count": 1, "model": "narwhal", "duration": "8s", "quality": "720p",
    "video_model": "omni_flash", "account_strategy": "round_robin",
    "account_id": null, "start_image_id": null },
  "result": { "engine_job_id": "引擎任务ID", "status": "succeeded",
    "account_id": "account_demo", "files": [
      { "name": "video.mp4", "url": "/api/v1/media/video.mp4" }
    ], "elapsed_seconds": 60.5 },
  "error": null, "created_at": "2026-10-13T02:00:00+00:00",
  "started_at": "2026-10-13T02:00:01+00:00",
  "finished_at": "2026-10-13T02:01:01+00:00", "retry_of": null
}`,
      fields: 'status：queued/running/succeeded/failed/interrupted/canceled。未完成时 result 为 null；失败原因见 error。interrupted 表示服务重启后结果未确认。files[].url 是相对路径，下载时拼接本次请求使用的基础地址。elapsed_seconds 单位：秒。',
      errors: `${lan ? '401：密钥错误；' : ''}404：任务 ID 不存在。`, contentType: '200 · application/json',
    },
    {
      id: 'jobs', name: '最近任务', method: 'GET', path: paths.jobs,
      summary: '返回最近 100 条任务，按创建时间从新到旧排序。',
      request: `GET ${paths.jobs} HTTP/1.1\n${auth}请求体：无`,
      response: `[
  ${queuedJob.replaceAll('\n', '\n  ')}
]`,
      fields: '返回数组，数组元素与“查询单个任务”的返回体相同；没有任务时返回 []。',
      errors: lan ? '401：缺少密钥或密钥错误。' : '服务无法连接：确认 EXE 正在运行。', contentType: '200 · application/json',
    },
    {
      id: 'media', name: '素材列表', method: 'GET', path: paths.media,
      summary: '查询已保存的图片和视频；最多返回最近 200 个文件。',
      request: `GET ${paths.media}?search=video&kind=video HTTP/1.1\n${auth}请求体：无`,
      response: `[{"id":"video.mp4","name":"video.mp4","kind":"video","size":1234567,"created_at":"2026-10-13T02:00:00+00:00","url":"/api/v1/media/video.mp4"}]`,
      fields: '查询参数：search 为可选文件名片段，kind 为 all（默认）/image/video。size 单位：字节；url 是相对下载路径。无素材时返回 []。',
      errors: `${lan ? '401：密钥错误；' : ''}422：kind 不合法。`, contentType: '200 · application/json',
    },
    {
      id: 'download', name: '下载素材', method: 'GET', path: paths.download,
      summary: '根据素材列表或任务结果中的 url 下载文件。',
      request: `GET /api/v1/media/video.mp4 HTTP/1.1\n${auth}请求体：无；文件名应使用响应中的 url，不自行拼接未编码名称。`,
      response: '二进制文件内容（MP4、JPG、PNG 或 WebP），不是 JSON。请按原文件名保存。',
      fields: 'HTTP Content-Type 由文件类型确定；文件大小可参考素材列表的 size。',
      errors: `${lan ? '401：密钥错误；' : ''}404：文件不存在。`, contentType: '200 · 文件二进制',
    },
    {
      id: 'upload', name: '上传参考图片', method: 'POST', path: paths.upload,
      summary: '首尾帧或参考图；只保存到本机，不提交生成任务。',
      request: `POST ${paths.upload} HTTP/1.1\n${auth}Content-Type: multipart/form-data\n\nfile=<JPG、PNG 或 WebP 文件；最大 20 MB>`,
      response: `{"image_id":"upload:随机文件名.png","name":"原始文件名.png","url":"${lan ? '/api/v1' : '/console/api'}/inputs/随机文件名.png"}`,
      fields: '表单字段名必须为 file；image_id 可用于 start_image_id、end_image_id 或 reference_image_ids。url 可 GET 预览，局域网预览也必须带密钥。也可使用已保存图片的 media:文件名。提交任务时可传 media_rights_confirmed:true，表示有权使用本次图片并允许自动确认 Google 使用权；默认 false，授权按图片内容保存，不代替登录或验证码。',
      errors: `${lan ? '401：密钥错误；' : ''}413：超过 20 MB；422：扩展名或文件签名不符。`, contentType: '200 · application/json',
    },
    {
      id: 'uploadVideo', name: '上传原视频', method: 'POST', path: paths.uploadVideo,
      summary: '为视频编辑保存原视频到本机；提交任务后才上传至 Flow。',
      request: `POST ${paths.uploadVideo} HTTP/1.1\n${auth}Content-Type: multipart/form-data\n\nfile=<MP4 文件；本后台限制最长 10 秒、最大 100 MB>`,
      response: `{"video_id":"upload:随机文件名.mp4","name":"原视频.mp4","url":"${lan ? '/api/v1' : '/console/api'}/inputs/随机文件名.mp4","duration_seconds":8.0,"size":1234567}`,
      fields: 'file 为文件本体；duration_seconds 单位秒，size 单位字节。video_id 原样填入 source_video_id，video_mode=edit，count=1；aspect/quality/duration 建议省略，默认值仅为兼容占位，不传给编辑引擎。仅编辑原视频，不支持动作参考或与参考图混用。url 可 GET 预览，局域网必须带密钥。',
      errors: `${lan ? '401：密钥错误；' : ''}413：超过 100 MB；422：非 MP4、缺少视频轨道、容器损坏或超过 10 秒。`, contentType: '200 · application/json',
    },
  ]
}
