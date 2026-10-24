# 0.2.57 MCP/API回归排查（不生成）

## 结论
真实任务9bf04dac-7f8d-44d9-b7ce-12333e3ef5a3收到官方YhhmEf业务错误7，明确原因类别unusual_activity。具体触发因素官方未返回，尚不能认定是MCP、隐藏窗口或账号因素。未达到用户“确认由MCP导致才回滚删除”的条件，因此未删除MCP、未修改生产生成方式。

## 真实调用关系
ConsoleMCP.call -> HTTP POST /console/api/jobs（本机）或 /api/v1/jobs（带密钥） -> JobInput -> 同一service.enqueue -> 同一队列执行 -> FlowEngine.generate_video -> OfficialVideos.run -> 官方页面按钮 -> YhhmEf -> 同一结果关联和下载。
MCP不向Google直接发生成请求；切回API仍使用同一个浏览器执行器。API和MCP都保留可用。

## 安装版对比
以releases/0.2.53/FlowAgentConsole.exe和D:/FlowAgentConsole/FlowAgentConsole.exe中实际嵌入代码比较：
- FlowEngine全部函数相同。
- 视频prepare_video、select_video_model、collect_video_results、verify_video_detail、download_video相同。
- 图片共用ready_to_submit、身份验证、直接编辑模式核对相同。
- OwnedBrowserVisibility.launch_args相同。
- 差异为错误响应摘要、运行组件预检、恢复只读检查、query_only防重提保护、原浏览器内180秒只读等待及迟到响应捕获；这些不能由静态对比推导为异常活动根因。
- 旧源码备份中单选框check改click是控件兼容修复；正式0.2.53安装包已经包含同一prepare_video实现。

## 任务对比与边界
此前7a657203、8ae1cd47、4cec2cec真实成功任务均为官方页面YhhmEf且窗口隐藏确认，无显示人工验证；与新失败同一RPC和隐藏策略。但成功为4秒、不同账号，失败为6秒、account_5bce66c57ef3，不是同条件因果实验。
MCP图片默认beluga、JobInput原默认narwhal存在历史默认值差异，但视频引擎不使用图片model字段，因此不能解释此次视频错误7。
错误7在结果下载前已经发生，不能归因于结果识别或结束后关窗。没有响应前后服务端风控评分证据，不能进一步编造触发因素。

## 无付费验证
新增tests/test_transport_parity.py：用拦截传输核对MCP本机/局域网Omni/Veo视频参数与直接API JobInput一致、各仅提交一次到离线拦截器；不会调用生产服务或Google。连同既有MCP契约测试通过。此项仅证明传输契约，不代表官方生成成功。
本次未生成、未重试、未删除代码、未修改客户端运行链；没有新安装包，因为没有已证实可修复该风控原因的运行代码改动。

## 尚未完成
异常活动具体触发条件和真实视频恢复仍未验证，当前不是最终修复。需要后续官方可见验证提示/支持反馈，或经用户另行授权的同账号同参数人工网页与API对照，才能继续区分账号/服务端限制与程序行为；本次不执行收费对照，也不修改浏览器指纹、验证码或风控校验来规避拒绝。
