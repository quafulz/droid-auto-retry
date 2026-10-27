# custom-model-stop-patch

Factory Droid 插件。Custom Model 的回合没有真正做完就停下来时，用 Stop hook 自动发送「继续」。

这个插件对应当前 Droid CLI 里的 Stop 行为：`decision: "block"` 会带着 `reason` 再开一轮，并把 `stop_hook_active` 设为 true。它不重放底层 HTTP 请求。

## 什么时候会续跑

看当前会话 transcript 里最后一条 assistant 消息，以及它之前的待办状态：

- 只有 `thinking` / `reasoning`，或者正文是空白：续跑。TUI 会多一条系统消息，形如 `Hook message: APUS Private DS v4.1 Flash #2 发生中断，正在自动重试 1/4...`。`Hook message:` 是 Droid 加的前缀。模型名优先用这条 assistant 消息的 `modelId` 在 Custom Model 设置里对应的 displayName；对不上时从 id 里取出 `custom:<名字>-<序号>` 的名字。没有模型 id 时显示 `Custom Model`。
- 正文是上下文压缩摘要：续跑。自动压缩时模型会输出 `<analysis>...</analysis><summary>...</summary>`，这类消息只是摘要，不是给用户的答复。细分场景下系统消息是 `... 任务尚未完成，正在自动续跑 1/4...`。
- 正文被截断：代码块 ` ``` ` 未闭合，或 `<analysis>` / `<summary>` 有开无闭：续跑。
- 待办列表仍有 `in_progress` / `pending`：续跑，`reason` 里会列出未完成项。只看这一轮 `TodoWrite` 工具调用里的结构化 `todos` 列表。模型如果把整段文本塞进 `todos`（例如 `1. [completed] ...`、`completed [high] ...`），Droid 会尽力解析，解析不出的状态一律落成 `pending`，这种状态是猜的，插件不据此续跑。只有 `todo_state` 记录、找不到对应的工具调用时也不续跑。
- 正文以提问收尾：不续跑。末尾是 `?` / `？`，或结尾出现「请确认」「是否需要」「要不要」这类说法，视为模型在等你回答。此时即使待办未完成也停住。
- 其他可见正文：不续跑。TUI 里这条 Stop hook 下面会多一行「本轮任务正常完成。」
- 已经发出工具调用：不续跑，交给 Droid 执行工具。
- 出现无法识别的内容块：不续跑。
- transcript 还没写出这条消息、路径不可读，或 Stop payload 缺字段：不续跑。

同一轮里连续自动续跑默认最多 4 次。中间一旦写出真正的正文或发出工具调用，下一轮用户消息会把次数重置。次数用完后停住，你可以再手工输入「继续」，那一次会重新开始计数。主界面不显示 thinking，所以静默中断看起来像是工具输出之后直接停住。

提问判断是文本启发式，不是语义理解。待办判断不看正文文字，只认 `TodoWrite` 入参里结构化的 `todos` 列表和其中的 `status`；模型用自由文本写待办时，插件不推断完成状态。模型如果一边说「接下来我会继续」一边结束回合，插件不会续跑；反过来，模型如果忘了把待办标成完成，可能被多续跑几次。上限 4 次保底。

## 不会处理的情况

- 回合一直挂着、没有进入 Stop。Hook 没有机会运行。
- 你主动取消回合。Droid 发的是 Notification，不是 Stop。
- 子代理的 SubagentStop。插件只注册主会话的 Stop。

自动续跑不是事务。续跑提示要求先核对已经成功的修改和命令，再决定要不要重做。

## 安装

需要 Droid CLI 和 Python 3.10+（命令是 `python3`）。

```bash
droid plugin marketplace add https://github.com/quafulz/droid-auto-retry
droid plugin install custom-model-stop-patch@droid-auto-retry
```

安装后新开一个会话，让 Droid 注册插件。用 `/hooks` 确认 Stop hook 已启用。

更新已经装好的插件不用重开会话。Droid 每一轮都会重新解析插件 hook 的命令路径，`droid plugin update` 之后下一次 Stop 就会执行新装的脚本；本机实测里 `droid plugin list` 的版本号和 transcript 中 `hookCommands` 的路径都会跟着变。只有**新安装**的插件需要新开会话才会被加载。如果 `/hooks` 里看不到变化，再重开会话。

## 配置

环境变量由启动 Droid 的进程读取。改完后完全退出并重新启动 Droid。

| 变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `DROID_AUTO_CONTINUE_MAX_ATTEMPTS` | `4` | `0-10`。`0` 关闭续跑 |
| `DROID_AUTO_CONTINUE_DETECT_UNFINISHED` | `1` | `0-1`。`0` 只保留静默中断检测，忽略压缩摘要、截断和待办信号 |
| `DROID_AUTO_CONTINUE_BASE_DELAY` | `2` | 第一次等待秒数 |
| `DROID_AUTO_CONTINUE_MAX_DELAY` | `15` | 单次等待上限，硬上限 40 秒 |
| `DROID_AUTO_CONTINUE_BACKOFF_FACTOR` | `2` | 连续中断时的退避倍数，范围 `1-4` |
| `DROID_AUTO_CONTINUE_STATE_TTL_SECONDS` | `7200` | 连续次数的过期时间 |
| `DROID_AUTO_CONTINUE_STATE_DIR` | 空 | 状态目录。默认是 `${XDG_STATE_HOME:-~/.local/state}/custom-model-stop-patch` |
| `DROID_AUTO_CONTINUE_TRANSCRIPT_TAIL_BYTES` | `524288` | 只读 transcript 尾部 |

状态库只保存 session 和消息 id 的 SHA-256、次数和时间戳。
