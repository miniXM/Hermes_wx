# 飞书接入参考

这份笔记用于整理飞书接入相关资料，方便后续把当前 Hermes 链路扩展到飞书。

建议阅读顺序：

1. 先看 Hermes 官方飞书文档，确认 Hermes 自己的配置方式。
2. 再看飞书官方文章，补齐中文操作路径和最小配置示例。
3. 最后看 OpenClaw 的飞书接入页，主要借它的权限、事件订阅和控制台截图做对照。

## 推荐资料

- Hermes 官方中文文档：飞书 / Lark 配置
  - https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/messaging/feishu
  - 重点看：
    - `hermes gateway setup`
    - `FEISHU_APP_ID`
    - `FEISHU_APP_SECRET`
    - `FEISHU_CONNECTION_MODE=websocket`
    - `FEISHU_ALLOWED_USERS`
    - 群策略、交互式卡片、故障排查

- Hermes 官方消息网关总览
  - https://hermes-agent.nousresearch.com/docs/user-guide/messaging/
  - 重点看：
    - `hermes gateway`
    - `hermes gateway setup`
    - `/set-home`
    - `/approve` 和 `/deny`

- Hermes Feishu 适配器源码
  - https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/feishu.py
  - 适合确认当前能力边界，比如：
    - WebSocket / Webhook 两种接入模式
    - 图片、文件、音频缓存
    - 群聊 @ 提及策略
    - 去重与卡片交互

- 飞书官方文章：一步步教你如何使用“爱马仕Agent”（附飞书接入教程）
  - https://www.feishu.cn/content/article/7630758640865037530
  - 这篇更偏中文落地，里面有一套最小可运行的飞书环境变量示例。

- OpenClaw 接入飞书
  - https://www.runoob.com/ai-agent/openclaw-feishu.html
  - 这篇不是 Hermes 文档，但对下面几件事很有参考价值：
    - 飞书开放平台里怎么创建应用
    - Bot 能力怎么开
    - 权限怎么配
    - 事件订阅页面大概长什么样

## 结合当前项目时的判断

- 当前仓库主链路是企业微信 PC Hook，不是飞书。
- 如果后面要做飞书接入，优先走 Hermes 官方 `feishu` 平台，不建议照搬 OpenClaw 的命令和配置项。
- OpenClaw 文章更适合当飞书后台操作参考；Hermes 具体配置还是要以官方飞书文档为准。

## 可直接复用的 Hermes 关键点

官方文档当前明确提到：

- 推荐连接模式：`websocket`
- 手动配置放在 `~/.hermes/.env`
- 私聊默认回复每条消息
- 群聊默认需要 `@` 机器人
- 可以通过 `FEISHU_ALLOWED_USERS` 做白名单
- 可以通过 `/set-home` 设置通知聊天

## 下一步可做

- 做一份飞书接入最小配置模板
- 补一个飞书接入排错清单
- 对照当前企业微信 GUI，评估是否要做飞书入口或单独的启动器
