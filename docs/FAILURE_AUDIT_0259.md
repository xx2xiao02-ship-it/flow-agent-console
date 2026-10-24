# 0.2.59 失败记录复核

本轮没有提交真实生成。

## 已确认

- 34d9e635：monclovabetters@gmail.com，本地身份/浏览器关联缺失，未提交生成。自动调度此前只按会话与缓存积分过滤，新增官方网页任务的本地关联检查，跳过不完整账号。
- 345a18b9：xx2xiaoagi@gmail.com，图片提交阶段发生 Error，原结果仍不明。此前缺少图片 original_result_unknown 文案，误显示成控件变化。修正文案，补充图片脱敏 RPC 错误诊断和原出错阶段；不伪造历史原因，不重新生成。
- 对上述图片账号的实时不生成检查通过：check_completed、generation_requests=0、browser_closed=true。这不能证明旧任务生成成功。
- 1006b7d0 / 3e9c1ab4：xx2xiao03@gmail.com，官方 RPC 7 / unusual_activity。具体触发原因未提供；保留原结果查询保护，不能保证本地修改解除官方拒绝。
- 78d57a42 / 2c96de7c：接口视频成功，结果记录 download_verified=true、browser_closed=true。

## 重试边界

一次性测试脚本 resume_s01_once.py 明确授权 maximumSubmissions=1，故 auto_retry_limit=0 是有意保护，不改为收费多次尝试。正常任务可显式设置 auto_retry_limit=3、retry_switch_accounts=true；已提交但结果未知只能查询原任务，不能换号重复提交。

## 发布

完整本地真实浏览器回归另发现：图片原错误 result_identity_mismatch 被180秒观察包装成 original_result_unknown 后，已核实图片的恢复入口拒绝该状态。已接受这一明确原错误链，新增同项目校验，保留同账号、响应编号、下载校验、零生成次数、关页等原条件；不放行没有响应编号的历史任务。

本版本包含 0.2.58 的本地暂停/优先账号策略。源码测试、安装包构建、实际安装、真实生成必须分别验收；不生成检查和本地测试不代表付费全链路已经验证。
