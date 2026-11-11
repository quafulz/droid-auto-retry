# custom-model-stop-patch

Factory Droid 和 Codex 插件。Custom Model 的回合没有真正做完就停下来时，用 Stop hook 自动发送「继续」。

这个插件对应当前两边的 Stop 行为：`decision: "block"` 会带着 `reason` 再开一轮，并把 `stop_hook_active` 设为 true。它不重放底层 HTTP 请求。Droid 读自己的 transcript。Codex 读 rollout 里的 `response_item`，判定结论相同。

## 什么时候会续跑

看当前会话 transcript 里最后一条 assistant 消息，以及它之前的待办状态：

- 只有 `thinking` / `reasoning`，或者正文是空白：续跑。TUI 会多一条系统消息，形如 `Hook message: APUS Private DS v4.1 Flash #2 发生中断，正在自动重试 1/4...`。`Hook message:` 是 Droid 加的前缀。模型名优先用这条 assistant 消息的 `modelId` 在 Custom Model 设置里对应的 displayName；对不上时从 id 里取出 `custom:<名字>-<序号>` 的名字。没有模型 id 时显示 `Custom Model`。
- 正文是上下文压缩摘要：续跑。自动压缩时模型会输出 `<analysis>...</analysis><summary>...</summary>`，这类消息只是摘要，不是给用户的答复。细分场景下系统消息是 `... 任务尚未完成，正在自动续跑 1/4...`。
- 正文被截断：代码块 ` ``` ` 未闭合，或 `<analysis>` / `<summary>` 有开无闭：续跑。
- 正文以工具调用协议片段收尾：续跑。Custom Model 偶尔把 `<｜DSML｜ ...>`、`<antml:...>` 这类片段当成正文输出，回合就此结束，工具并没有真的调用。只认落在正文末尾的标记，正文中间引用、或整段写在代码块里讨论都不会命中。
- 待办列表仍有 `in_progress` / `pending`：续跑，`reason` 里会列出未完成项，并要求先对账（已完成的标 `completed`，还缺什么就说清）。证据只取 transcript 里 Droid 写的 `todo_state` 记录，插件不解读任何字样。这个记录有两种形态：模型按契约传结构化 `todos` 时，直接读每项的 `status`；模型把整份清单当字符串传时，Droid 可能逐行解析成带 `status` 的条目，也可能解析不出而把整行原样留下、状态落成默认的 `pending`。判别不靠状态词表，只看解析是否真的发生：解析成功的那一行，条目的 `content` 一定比原行短（状态标记和行首序号被剥掉）；解析失败的那一行，`content` 与原行只差行首序号。只要有任意一行两者相等，就说明这一行的状态是默认值，整份清单弃权。行数与条目数对不上、找不到对应的 `todo_state` 时同样弃权。此外，最后一次 `TodoWrite` 之后只要还发过工具调用，这份计划就算过时（模型常常先更新计划、再干完收尾的活、然后直接写总结），也弃权。弃权等于不续跑，因此模型怎么写待办都不会触发误判，代价是拿不到这份证据。
- 正文以提问收尾：不续跑。末尾是 `?` / `？`，或结尾出现「请确认」「是否需要」「要不要」这类说法，视为模型在等你回答。此时即使待办未完成也停住。
- 其他可见正文：不续跑。Droid 的 TUI 里这条 Stop hook 下面会多一行「本轮任务正常完成。」Codex 把非 JSON 输出当成钩子失败，所以正常结束时什么都不打印。
- 已经发出工具调用：不续跑，交给 Droid 执行工具。
- 出现无法识别的内容块：不续跑。
- transcript 还没写出这条消息、路径不可读，或 Stop payload 缺字段：不续跑。

同一轮里连续自动续跑默认最多 4 次。判定「连续」依据 transcript 里的最后一次工具调用：再次 Stop 时如果已经出现了新的工具调用，说明模型在两次中断之间真的干了活，连续次数从 1 重新开始计；全新的用户消息（`stop_hook_active` 为 false）同样从 1 开始。只看 `stop_hook_active` 不够：实测 Droid 在整个续跑链里始终把它置为 true，跑完 104 次工具调用之后的一次中断仍会被算成连续第 3 次，次数很快用完，之后真正的压缩摘要中断就不再续跑。所以插件自己比对 transcript 里的工具调用 id。次数用完后停住，你可以再手工输入「继续」，那一次会重新开始计数。主界面不显示 thinking，所以静默中断看起来像是工具输出之后直接停住。

提问判断是文本启发式，不是语义理解。待办判断不看 assistant 正文，也不解析模型写的自由文本：它只比较「Droid 是否真的解析了每一行」这个结构事实，再读 `todo_state` 里已经成型的状态。模型如果一边说「接下来我会继续」一边结束回合，插件不会续跑；反过来，模型如果忘了把待办标成完成，可能被多续跑一次——续跑提示会先要它对账，属于这种情况时一般一轮就收住。上限 4 次保底。

## 不会处理的情况

- 回合一直挂着、没有进入 Stop。Hook 没有机会运行。
- 你主动取消回合。Droid 发的是 Notification，不是 Stop。
- 子代理的 SubagentStop。插件只注册主会话的 Stop。

自动续跑不是事务。续跑提示要求先核对已经成功的修改和命令，再决定要不要重做。

## Codex

Codex 的 transcript 是 rollout。一次采样里的思考是单独的 `reasoning` 项，工具调用是 `function_call`、`local_shell_call` 等项，正文在 assistant `message` 的 `output_text` 里。插件看的是最后一条用户消息或工具结果之后的那一次采样。

和 Droid 的差别：

- 模型名用 Stop payload 里的 `model`，不读 `.factory/settings.json`。
- 待办读 `update_plan`（或名为 `TodoWrite` 的函数调用）里结构化的 `plan[].step` / `status`。入参不是结构化清单时弃权。最后一次计划之后又有工具调用，同样弃权。续跑提示里要求用 `update_plan` 对账。
- 只有 `compaction` / `context_compaction`、没有正文时，按压缩摘要续跑。
- 正常结束不打印「本轮任务正常完成。」`systemMessage` 仍会输出，Codex 把它显示在钩子卡片上。
- 子线程的 SubagentStop、用户取消时的 Interrupt 都不注册。

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

Droid 更新已经装好的插件不用重开会话。Droid 每一轮都会重新解析插件 hook 的命令路径，`droid plugin update` 之后下一次 Stop 就会执行新装的脚本；本机实测里 `droid plugin list` 的版本号和 transcript 中 `hookCommands` 的路径都会跟着变。只有**新安装**的插件需要新开会话才会被加载。如果 `/hooks` 里看不到变化，再重开会话。Codex 安装或更新后新开一个会话。

## 配置

环境变量由启动 Droid 或 Codex 的进程读取。改完后完全退出并重新启动对应的 CLI。

| 变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `DROID_AUTO_CONTINUE_MAX_ATTEMPTS` | `4` | `0-10`。`0` 关闭续跑 |
| `DROID_AUTO_CONTINUE_DETECT_UNFINISHED` | `1` | `0-1`。`0` 只保留静默中断检测，忽略压缩摘要、截断、协议泄漏和待办信号 |
| `DROID_AUTO_CONTINUE_BASE_DELAY` | `2` | 第一次等待秒数 |
| `DROID_AUTO_CONTINUE_MAX_DELAY` | `15` | 单次等待上限，硬上限 40 秒 |
| `DROID_AUTO_CONTINUE_BACKOFF_FACTOR` | `2` | 连续中断时的退避倍数，范围 `1-4` |
| `DROID_AUTO_CONTINUE_STATE_TTL_SECONDS` | `7200` | 连续次数的过期时间 |
| `DROID_AUTO_CONTINUE_STATE_DIR` | 空 | 状态目录。默认是 `${XDG_STATE_HOME:-~/.local/state}/custom-model-stop-patch` |
| `DROID_AUTO_CONTINUE_TRANSCRIPT_TAIL_BYTES` | `524288` | 只读 transcript 尾部 |

状态库只保存 session 和消息 id 的 SHA-256、次数、时间戳和最后一次工具调用的 id。

## 复核待办判据（开发用）

`tools/audit_todo_evidence.py` 拿真实 transcript 复算待办判定，用来判断误报和漏判，不修改任何状态：

```bash
python3 tools/audit_todo_evidence.py --since 2026-09
python3 tools/audit_todo_evidence.py --since 2026-10 --tail 300
python3 tools/audit_todo_evidence.py --json > audit.json
```

输出会列出被判续跑的会话、它们的未完成项、收尾正文，以及 `work_after_call` 的反事实结果：如果把这条过时弃权关掉，哪些会话的判定会变。判断误报的办法是看收尾正文有没有已经把结果交付出来——交付了却仍被判续跑，就是误报。默认只看 `todos`，用 `--verdict` 可以加上 `complete`、`tool_use` 等其他判定。
