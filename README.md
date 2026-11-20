# droid-auto-retry

Factory Droid 和 Codex 共用的插件市场。目前只有一个插件：[custom-model-stop-patch](plugins/custom-model-stop-patch/README.md)。

Custom Model 的回合没有真正做完就停下来时（静默中断、压缩摘要、正文截断、待办未完成），Stop hook 会自动发送「继续」。Droid 的静默中断只对 `modelId` 以 `custom:` 开头的模型生效，Codex 不受这条约束。

采样请求在 API 层失败时（429、5xx、流中断）也会续跑。Droid 这时已经进入 Stop，插件确认是可重试错误后，退避并用 `decision: block` 在当前进程里再开一轮。Codex 不发 Stop，插件改听 `PostModelRequest`；退避后先执行 `codex queue --thread <session_id> --message <续跑提示> --resume`。queue 成功只表示消息已持久入队，只有 rollout 出现不同 `turn_id` 的新 `task_started` 才算恢复；短 settle 窗口内没看到新 turn 时，job 保持 `queued`，由后续 hook 在验证期限内继续核对，超期仍未确认则标为 `completed_unverified`，仍不等于恢复。只有 queue 明确不支持时，才回退到等待写锁后执行 `codex exec resume`；超时或其它非零结果视为未知，不自动 fallback，也不重复投递。同一 session 和 turn 的多个不同错误只投递一次。exec fallback 会还原原命令行上的 provider、沙箱和审批设置。认证失败、配额、上下文超限、内容策略和敏感词不重试。

## 安装

需要对应的 CLI，以及 Python 3.10+（命令是 `python3`）。

Droid：

```bash
droid plugin marketplace add https://github.com/quafulz/droid-auto-retry
droid plugin install custom-model-stop-patch@droid-auto-retry
```

Codex：

```bash
codex plugin marketplace add https://github.com/quafulz/droid-auto-retry
codex plugin add custom-model-stop-patch@droid-auto-retry
```

安装后新开一个会话。Droid 用 `/hooks` 确认 Stop hook 已启用。

## 文档

判定规则、不会处理的情况、安装细节、环境变量和开发用的复核脚本，都写在[插件 README](plugins/custom-model-stop-patch/README.md)。那份是唯一来源，本文件只做入口。

## 开发

```bash
python3 -m pytest -q
```
