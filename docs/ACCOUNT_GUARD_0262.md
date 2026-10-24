# 0.2.62 调度拦截与原图恢复

## 根因与证据
- 2026-10-22 20:31 的8条失败均显式指定 account_5bce66c57ef3，官方脱敏错误为 code 7 / unusual_activity。
- 调用方 E:\video-shotcraft\deployment\material_bridge\build_paid_formal_run.py 将账号写死；build_variety_degraded_run.py 也沿用同一默认值。
- director-v3-paid-20261022-r10/provider-degraded-reuse.json 记录的任务编号与后台这8条一致。
- 同批第一条省略账号，由后台选择另一个账号，已成功。原有暂停名单只约束自动选择，显式账号绕过了该名单。

## 本次改动
- 两个编译脚本的新请求改为 account_id=null、account_strategy=round_robin。历史运行合同、已有收费请求和结果不改写。
- 后台在入队、调度和调用引擎前检查暂停名单及持久化官方异常活动证据。旧队列也会在提交前被拦截。
- 自动调度跳过上述账号；读取积分、登录检查、只读查询仍允许。仅有通用 code 7 不构成异常活动判断，必须有已识别原因。
- 余额刷新不解除异常限制；恢复较早的成功结果也不会覆盖后来的异常证据。
- MCP 参数说明和 Skill 默认省略账号，指定被拦截账号时返回明确的“本次未提交”说明。
- 账号接口增加 generation_blocked/generation_block_reason；账号管理显示生成暂停原因。

## 只读恢复
- 13:44 图片任务 13534942-136a-4ebd-ba2a-0de51ed41a23 已匹配原项目、原 media 7127eaf8-3846-4f3c-8181-5785be699c13 与 editor 8099325f-da10-4c6a-b15d-78e5e1c177fb。
- 下载 flow-page-e4104218905b407bbcfeebc2897ca4ef.jpg，HTTP 200，293477字节；图片内容已检查。
- 任务恢复 succeeded/outcome=recovered，保留原失败证据，生成请求0，专用浏览器已关闭。
- 未重试8条视频，未自动切账号重新生成，未消耗生成积分。

## 验收边界
使用离线队列/HTTP/MCP/本地浏览器回归，并真实只读恢复原图；不声称完成新的收费生成测试。异常活动的官方内部触发规则没有返回，无法确认具体风控因素。
