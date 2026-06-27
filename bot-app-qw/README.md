# Hermes WeCom Plugin

企业微信 PC DLL hook 到 Hermes Gateway 的本机插件。当前链路不再使用旧 Bun bridge：

```text
企业微信 -> helper.dll/loader.dll -> cli.exe -> WeCom PC Hook adapter(127.0.0.1:8001) -> Hermes Gateway Agent
```

## 组件

| 路径 | 说明 |
| --- | --- |
| `electron/` | Electron 桌面 GUI，负责启动/停止 Hermes、企业微信和插件注入器。 |
| `hermes_plugins/wecom_pc_hook/` | Hermes Gateway platform adapter，接收 CLI hook 并进入 Hermes Agent 会话。 |
| `wxwork-cli.c` | C 版 CLI 主程序，负责加载 DLL、注入企业微信、上报消息和轮询下行队列。 |
| `cli.exe` | 已构建的 32 位 CLI 注入器。 |
| `loader.dll` / `helper.dll` | 企业微信注入和通信 DLL。 |
| `bot.ini` | CLI 配置文件，`hook` 指向 adapter 地址。 |
| `package-electron.ps1` | 生成免 Bun 的 Electron 插件包。 |

## 运行要求

- Windows。
- 企业微信 PC 客户端，建议使用已验证版本 `4.1.33.6009`。
- Hermes 已安装，并且 PowerShell 中可以执行 `hermes`。
- Hermes 模型/provider 在 Hermes 自身配置，不由本插件管理。

本插件包不需要 Bun。Bun bridge 已移除。

## bot.ini

`cli.exe` 读取 `[config] hook`，用于上行 `POST` 和下行 `GET`：

```ini
[config]
hook = http://127.0.0.1:8001/hook/testtoken
api = http://127.0.0.1:8001
```

## 开发运行

启动 Electron GUI：

```powershell
npm run electron
```

GUI 会把 `hermes_plugins/wecom_pc_hook` 安装/同步到 Hermes 用户插件目录，并通过：

```powershell
hermes gateway run --accept-hooks
```

启动 Hermes Gateway。插件开关启动的是：

```powershell
cli.exe loader.dll helper.dll "C:\Program Files (x86)\WXWork\WXWork.exe"
```

如果企业微信路径不同，在 GUI 里配置 `WXWork.exe` 路径。

## 策略模块

GUI 的“策略”区域会同步写入 Hermes 配置，换机器后按同一设置启动即可。

- 免审批默认开启：`wecom_pc_hook.extra.allow_all_users: true`，企业微信发送者默认可直接进入 Hermes 会话，不需要逐个执行 `hermes pairing approve`。
- 工具策略：`原生` 不禁用工具；`只聊天` 会禁用文件、终端、浏览器、代码执行、任务、记忆、生成等常见 toolset；`自定义禁用` 可填写逗号分隔的 Hermes toolset 名称，例如 `terminal,browser,file`。GUI 会写入 `agent.disabled_toolsets`，下次启动 Hermes gateway 后生效。
- 人设文件：点击“打开人设文件”会用记事本打开 `%LOCALAPPDATA%\hermes\SOUL.md`。Hermes 会把这个文件作为全局人设/系统风格读取。

如果需要改回审批模式，在策略区域关闭“免审批”并保存，然后重启 Hermes gateway。

## 打包

生成标准 Windows 安装包（NSIS）：

```powershell
npm run package
```

输出：

```text
dist\nsis\HermesPluginSetup-1.0.0.exe
```

生成 Electron 便携包：

```powershell
npm run package:portable
```

输出：

```text
dist\HermesPlugin-electron\
dist\HermesPlugin-electron.zip
```

安装包和便携包都包含 GUI、`cli.exe`、DLL 运行时和 Hermes adapter；不包含 Hermes、企业微信、Bun 或构建工具。

## 验收

1. 打开并登录企业微信。
2. 启动 `HermesPlugin.exe`。
3. 确认 Hermes 状态为运行中，Gateway Adapter 状态为监听中，CLI 注入器状态为运行中。
4. 在企业微信里发送文本或图片。
5. 查看 GUI 运行日志，以及 Hermes 日志：

```powershell
Get-Content "$env:LOCALAPPDATA\hermes\logs\gateway.log" -Tail 160
Get-Content "$env:LOCALAPPDATA\hermes\logs\agent.log" -Tail 160
```

图片链路成功时，`gateway.log` 应出现类似：

```text
matched WeCom cache mapping
cached inbound image
Image routing: native
```

## 参考资料

- 飞书 / Hermes 接入参考笔记：
  - [`docs/feishu-hermes-resources.md`](docs/feishu-hermes-resources.md)

## 构建 CLI

当前推荐 C 版 CLI：

```bash
gcc -mconsole wxwork-cli.c -o cli.exe -lcurl -lws2_32
```

如果直接在 PowerShell 运行，需要把 MINGW32 的 `libcurl-4.dll` 及其依赖 DLL 放到 `cli.exe` 同目录。

## 注意事项

- `loader.dll` 使用固定导出偏移，版本变化可能需要同步调整 `wxwork-cli.c`。
- CLI、DLL、企业微信客户端位数必须匹配；当前链路按 32 位注入器使用。
- 旧的 `server.ts` / `src/bridge` / `hermes-bridge.exe` / 8000 端口路径已移除，避免和 Hermes Gateway adapter 混用。
