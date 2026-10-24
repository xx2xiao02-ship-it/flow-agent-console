# 本地浏览器验证引擎补丁

上游：<https://github.com/kodelyx/flow-go>，固定提交
`70ec73861091267dd1f57184bcf809a922bea85b`。

`browser-required.patch` 增加本地后台需要的严格模式；原 CLI 的
`auto`、`broker`、`http` 模式保留。后台使用新增 `--captcha browser`：

- 等待并绑定与 `--cookies` 完全相同的账号身份。
- 必须有支持 `flow.consoleScope` 的扩展和本次新开标签的归属记录。
- 只使用该网页原有的验证组件，不注入组件，不回退 HTTP 或空令牌。
- 需要本人登录、人工验证或页面未就绪时，在生成提交前失败。
- `FLOW_AUTO_RETRY=0` 与 `FLOW_NO_SUBMISSION_REPLAY=1` 禁止自动重复提交。
- 不重发仍保留首次响应的拒绝原因、帧数及历史错误；严格模式使用当前页面构建号。
- 后台空闲监听设置 `FLOW_BRIDGE_SYNC_ONLY=1`，不运行会打开项目页的引擎启动探测；CLI 原有默认启动方式保留。
- 新增 `browser-check --cookies <账号文件>` 只探测组件是否加载，不获取令牌，
  不提交生成。后台额外设置 `FLOW_BLOCK_GENERATION=1`，在传输层拒绝生成 RPC。

Windows 构建：先准备 Go 1.26.4 或更高兼容版本，再运行
`powershell -ExecutionPolicy Bypass -File .\Build-FlowEngine.ps1`。
脚本读取固定提交、应用补丁、执行五个模块的离线回归后才替换打包用引擎；
原二进制按 SHA-256 保留在 `build/`。不会改动用户会话、素材或任务。
`Build-FlowConsoleExe.ps1` 也会调用此构建步骤。

该修复可验证连接、身份、页面和传输保护；无付费生成测试时，不能据此声称
Google 已接受生成验证，或图片/视频已经可以成功生成。
