# Droid Auto Continue

Factory Droid 插件。Custom Model 的流在思考阶段被掐断、界面上像是没报错就停住时，用 Stop hook 自动发送一次「继续」。

这个插件对应当前 Droid CLI 里的 Stop 行为：`decision: "block"` 会带着 `reason` 再开一轮，并把 `stop_hook_active` 设为 true。它不重放底层 HTTP 请求。

## 什么时候会续跑

只看当前会话 transcript 里最后一条 assistant 消息：

- 只有 `thinking` / `reasoning`，或者正文是空白：续跑。
- 已经有可见正文：不续跑。模型问问题、做完总结，都停在这里。TUI 里这条 Stop hook 下面会多一行「本轮任务正常完成。」
- 已经发出工具调用：不续跑，交给 Droid 执行工具。
- 出现无法识别的内容块：不续跑。
- transcript 还没写出这条消息、路径不可读，或 Stop payload 缺字段：不续跑。

同一轮里连续静默中断默认最多自动续跑 4 次。中间一旦写出正文或工具调用，下一轮用户消息会把次数重置。次数用完后停住，你可以再手工输入「继续」，那一次会重新开始计数。主界面不显示 thinking，所以这种中断看起来像是工具输出之后直接停住。

## 不会处理的情况

- 回合一直挂着、没有进入 Stop。Hook 没有机会运行。
- 模型已经写出半截正文再断开。这种结尾带有 text，插件会把它当成正常结束。
- 子代理的 SubagentStop。插件只注册主会话的 Stop。

自动续跑不是事务。续跑提示要求先核对已经成功的修改和命令，再决定要不要重做。

## 安装

需要 Droid CLI 和 Python 3.10+（命令是 `python3`）。

```bash
droid plugin marketplace add https://github.com/quafulz/droid-auto-retry
droid plugin install droid-auto-continue@droid-auto-retry
```

安装后新开一个会话。用 `/hooks` 确认 Stop hook 已启用。更新插件后也要新开会话，已打开的会话不会重新加载 hook。

## 配置

环境变量由启动 Droid 的进程读取。改完后完全退出并重新启动 Droid。

| 变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `DROID_AUTO_CONTINUE_MAX_ATTEMPTS` | `4` | `0-10`。`0` 关闭续跑 |
| `DROID_AUTO_CONTINUE_BASE_DELAY` | `2` | 第一次等待秒数 |
| `DROID_AUTO_CONTINUE_MAX_DELAY` | `15` | 单次等待上限，硬上限 40 秒 |
| `DROID_AUTO_CONTINUE_BACKOFF_FACTOR` | `2` | 连续中断时的退避倍数，范围 `1-4` |
| `DROID_AUTO_CONTINUE_STATE_TTL_SECONDS` | `7200` | 连续次数的过期时间 |
| `DROID_AUTO_CONTINUE_STATE_DIR` | 空 | 状态目录。默认是 `${XDG_STATE_HOME:-~/.local/state}/droid-auto-continue` |
| `DROID_AUTO_CONTINUE_TRANSCRIPT_TAIL_BYTES` | `524288` | 只读 transcript 尾部 |

状态库只保存 session 和消息 id 的 SHA-256、次数和时间戳。
