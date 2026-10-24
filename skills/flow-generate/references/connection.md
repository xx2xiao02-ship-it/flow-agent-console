# 连接 Flow MCP

在支持 stdio MCP 的客户端添加服务器，命名 `flow-console`。同电脑配置示例（JSON）：

```json
{
  "mcpServers": {
    "flow-console": {
      "command": "E:/FLOW/flow-agent/.venv/Scripts/python.exe",
      "args": ["-u", "E:/FLOW/flow-agent/console_mcp_entry.py"],
      "env": {"FLOW_MCP_URL": "http://127.0.0.1:8001", "FLOW_MCP_READ_ONLY": "0"}
    }
  }
}
```

不同客户端配置入口和格式不同；以上为通用JSON示例，不直接写入 Codex 的 TOML 配置。启动后先查询 `flow_capabilities` 和 `flow_accounts` 验证连接，不生成。`FLOW_MCP_READ_ONLY=1` 可强制阻止生成、上传与本地下载。

其他电脑：将 MCP 适配器所在 Python 包安装到该电脑，设置 `FLOW_MCP_URL=http://运行Flow电脑的局域网IP:8002`，在客户端进程环境设置 `FLOW_MCP_API_KEY` 为后台接口接入页配置的密钥。必须先开启后台带密钥局域网 API。密钥不要写入 Skill、提交Git或发到对话中。

完整安装和工具说明见 Flow 项目 `docs/MCP_SKILL.md`。MCP 不启动客户端服务；关闭后台后无法查询或生成。旧 `main.py mcp` 不是本 Skill 的管理后台入口。
