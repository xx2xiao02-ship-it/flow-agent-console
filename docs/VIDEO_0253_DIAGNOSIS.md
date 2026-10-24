# 视频链路核查（2026-10-21）

## 实际调用链

后台 /console/api/jobs → ConsoleService 队列 → engine.generate_video →
engine.official_videos.run（ConsoleService 初始化时绑定）→ OfficialVideos._run。
原生 CLI 分支仍保留，后台已绑定网页 runner 时不会同时执行两条生成链路。

参数：ensure_direct_composer → composer_settings → prepare_video →
视频/素材或帧/模型/比例/时长/画质/数量逐项确认 → ready_to_submit。

结果：submit_video_once（一次点击、一次请求限制）→ 官方响应 →
video_result_ids（限定项目和明确字段）→ collect_video_results →
verify_video_detail（项目、编辑ID、内容ID绑定）→ 原始720p下载 →
inspect_mp4 → ConsoleService 检查落盘文件后写 succeeded；finally 关闭本次浏览器。

## 22:07 控件故障

任务 fbbecd95-7bdb-4329-aae0-d66a0670875a，无生成请求。
真实官方页面复现 Locator.check: Clicking the checkbox did not change its state。
捕获错误后同一个视频按钮的 aria-checked 已为 true：Angular 异步更新晚于
Playwright check() 的即时检查。改成普通 click 后 expect 等待已选中状态，
并只定位可见控件，不增加生成点击重试。修复后原账号无头参数检查通过。

## 22:12 结果调查

任务 f6dbfc64-802f-4a1a-a81d-70a9523068a5，账号 account_5bce66c57ef3，
原项目 4f403138-5571-47bb-a5df-399e8228efe2。HTTP200，YhhmEf 正文为空，
wrb.fr 第六字段是状态结构。旧报告只保留类型，无法还原当时数字错误码。
两次只读查看正确账号原项目，显示“开始创建或拖放媒体”，无视频卡片。
未恢复任务、未将其他账号历史成品冒充本次结果，不能据此证明未扣费。

新代码识别明确非零数字 RPC 错误状态，与未知结构分开；仅保存数字，不保存
Cookie、Token、原始响应或平台自由文本。错误时停止自动生成重试。
无法通过不生成检查证明平台今后一定接受生成请求。

## 冗余候选（未删除）

1. flow-extension/background-v1.0.1.js 至 background-v1.0.9.js：历史扩展版本。
   当前 manifest 只加载 background-v1.0.10.js；旧文件不在当前激活链路。
   删除影响历史版本回退，须另行确认。background.js 是测试引用的源文件，保留。
2. build/video-0251-*.py、build/video-0252-*.py：单次调查/验收脚本，不是生产入口。
   建议归档而非删除，保留原始付费任务的验证证据。
3. prepare_video 内重复导入 expect、重复写 response_result_ids：局部重复，
   本次未清理，不是已经复现的故障原因。
4. engine.generate_video 的原生 CLI 分支：仍供未绑定网页 runner 的调用使用，
   是兼容链路，不应作为冗余删除。

## 验收边界

不提交 Google 生成；本地测试页的模拟请求不消耗积分。
真实网页仅核查原项目及参数，无头运行并关闭。0.2.53 未做收费生成验收。
