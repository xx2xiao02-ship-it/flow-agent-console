# Flow 0.2.54 浏览器启动故障修复

## 故障证据

原任务 718ef545-0628-4717-a0af-1e55140defc8（2026-10-21 23:19:53），
account_5bce66c57ef3，Omni、6s、720p、16:9、1条。
失败阶段 launch，FileNotFoundError，generation_requests=0。

运行的是已验证的0.2.53安装文件，SHA256：
53E1752CB0AAC3B10A2AAFEDD0FB71F72375A79DF43482FB172072C5D1B4C042。
其PyInstaller归档含119个Playwright相关文件，包含driver/node.exe和
driver/package/cli.js，因此不是漏打包。

运行PID28944加载的Python位于
C:/Users/Administrator/AppData/Local/Temp/_MEI00008adc2/python314.dll。
该运行目录的playwright/driver/node.exe和playwright/driver/package/cli.js均缺失，
已加载DLL仍存在。Playwright compute_driver_executable从包目录定位这两个文件。
缺失足以解释浏览器启动前FileNotFoundError；无法从现存证据判断是谁移除了文件。
不把旧launcher-error.log中的WinError32当作本次根因。

## 修复

- 保留原onefile构建；安装器改用onedir安装版及固定app-runtime目录，不依赖TEMP/_MEI。
- 启动自查检查Playwright驱动文件，并实际运行node cli.js --version；失败不开放生成队列。
- 每次图片/视频网页操作前重新检查实际驱动文件；缺失明确报驱动故障，不报网页控件问题。
- 驱动缺失或启动失败不自动重试生成。
- 安装、构建及可选签名脚本统一使用安装版入口，旧安装包保留。

## 范围与验收

本轮不生成、不重试历史任务，不修改调用方项目、原配音、旧素材和旧结果。
48项本地回归通过；安装后同账号同参数的video-page-check结果及进程检查另存verification.json。
健康检查200不是生成就绪证据；page_ready、check_completed、generation_requests=0、
实际进程退出为本次验收标准。本轮不宣称收费生成E2E验证。
