---
name: flow-generate
description: 通过已连接的 flow-console MCP 调用本地 Flow 管理后台，生成图片和 Omni/Veo 视频、上传参考素材、查询任务并下载结果。适用于明确要求使用 Flow 生产素材的任务。
---

# Flow 生图与视频

## 接入与核查

使用 `flow_capabilities` 查看当前参数合同，再调用 `flow_accounts` 核对可用账号与积分。工具未连接时，阅读随附 `references/connection.md`，不要改用旧引擎、网页点击或其他供应商冒充本后台。

- 本机默认连接 `http://127.0.0.1:8001`，后台客户端必须已启动。
- 远程电脑连接已开启的带密钥 LAN API；参考素材路径和下载目标路径属于 **MCP 进程所在电脑**。
- 模型权限、积分与缓存时间分别判断，缓存积分不代表生成一定可用。需要本人登录或验证时报告用户，不绕过。

## 生成前

明确提示词、类型、比例、模型、数量和用户授权范围。图片默认 `beluga`（当前2.1链路），每任务1张；视频按实际需求选择 `omni_flash`、`veo_3_1_lite`、`veo_3_1_fast` 或 `veo_3_1_quality`。Veo 当前只支持8秒、720p，不能自动改模型或改参数掩盖失败。

真实生成可能扣积分。用户仅要求检查、查余额、排错或明确不允许消耗积分时，禁止调用生成工具。只有本次任务已有明确生成/扣费授权，才传 `acknowledge_possible_charge: true`；仅授权一次就只提交一次。未知或超出授权的数量、重试需先问用户。

参考素材通过 `flow_upload_reference` 上传，使用返回的 `image_id` / `video_id`，不填客户端本地路径代替素材ID。仅在用户已确认素材使用权时设置 `media_rights_confirmed: true`。首帧用 `start_image_id`，参考图列表用 `reference_image_ids`，原视频用 `source_video_id`。以 capabilities 返回合同约束素材组合。

## 提交与跟踪

1. 调用 `flow_generate_image` 或 `flow_generate_video`，参数为 `{job: {...}, acknowledge_possible_charge: true}`。默认省略 `account_id` 或填 `null`，由后台按 `account_strategy` 自动调度；只有用户明确指定账号才填具体编号，不能把历史成功/失败记录中的账号写成批次默认值。具体 `account_id` 会优先于轮流策略；异常或暂停账号会在生成前拒绝。遇到此拒绝先报告，不重新提交已受理任务。
2. 保存返回的任务 `id`。`queued` / `running` 只是已受理，不能报告成功。
3. 使用 `flow_get_job` 查询同一个ID，间隔约5–10秒。达到客户端时间限制时返回ID及当前状态，后续继续查询，不重复提交。
4. `succeeded` 且 `result.files` 非空后，才用 `flow_download_result` 下载。提供存在父目录下的绝对目标路径；不会覆盖已有文件。核查实际下载文件后报告路径。
5. `failed`、空结果、网络超时或提交结果不确定时，查询原任务或 `flow_jobs`，报告失败信息，不自动重提、不自动切号。新的收费提交需要授权。

MCP 提交固定关闭自动重试，共用后台队列与并发设置；不批量对所有账号提交。浏览器沿用后台当前策略，不能把“任务已结束”推断成“浏览器已关闭”：如返回 `result.execution`，检查 `browser_closed` 与 `visibility`；缺少证据就标记未确认。

## 示例

已获一次生图授权时：

```json
{"job":{"prompt":"一只坐在窗边的猫，柔和自然光","model":"beluga","aspect":"1:1","count":1},"acknowledge_possible_charge":true}
```

已获一次 Veo 授权时：

```json
{"job":{"prompt":"一只猫缓慢走向窗边，镜头平稳","video_model":"veo_3_1_fast","aspect":"16:9","duration":"8s","quality":"720p","count":1},"acknowledge_possible_charge":true}
```
