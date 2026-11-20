# custom-model-stop-patch

Factory Droid 和 Codex 插件。Custom Model 的回合没有真正做完就停下来时，用 Stop hook 自动发送「继续」。

这个插件对应当前两边的 Stop 行为：`decision: "block"` 会带着 `reason` 再开一轮，并把 `stop_hook_active` 设为 true。它不重放底层 HTTP 请求。Droid 读自己的 transcript。Codex 读 rollout 里的 `response_item`。压缩摘要、截断、待办和 API 失败两边相同。Droid 的静默中断只续跑 `modelId` 以 `custom:` 开头的模型；Codex 不看这个前缀。

## 什么时候会续跑

看当前会话 transcript 里最后一条 assistant 消息，以及它之前的待办状态：

- 只有 `thinking` / `reasoning`，或者正文是空白：Droid 仅当这条 assistant 消息的 `modelId` 以 `custom:` 开头时续跑。自带模型（如 `claude-opus-5-5`）、没有 `modelId`、或前缀不是 `custom:` 时不续跑，也不打印「本轮任务正常完成。」。Codex 的静默中断仍续跑，不看这个前缀。TUI 会多一条系统消息，形如 `Hook message: APUS Private DS v4.1 Flash #2 发生中断，正在自动重试 1/4...`。`Hook message:` 是 Droid 加的前缀。模型名优先用这条 assistant 消息的 `modelId` 在 Custom Model 设置里对应的 displayName；对不上时从 id 里取出 `custom:<名字>-<序号>` 的名字。没有模型 id 时显示 `Custom Model`。
- 两种空回复不续跑，也不打印「本轮任务正常完成。」：上一条是只给模型看的系统提醒，写明没有可做的事时用无正文、无工具调用结束回合；或者上一条是短的工具结果，写明后台任务仍在运行（`Status: running` 且 `Task is still running`），或任务启动时写明不要用 `TaskOutput` 轮询。用户自己把这些句子贴进对话，或普通工具结果后面只剩思考，Droid 上仍只对 `custom:` 模型按中断续跑；Codex 仍续跑。
- 正文是上下文压缩摘要：续跑。自动压缩时模型会输出 `<analysis>...</analysis><summary>...</summary>`，这类消息只是摘要，不是给用户的答复。细分场景下系统消息是 `... 任务尚未完成，正在自动续跑 1/4...`。
- 正文被截断：代码块 ` ``` ` 未闭合，或 `<analysis>` / `<summary>` 有开无闭：续跑。
- 正文以工具调用协议片段收尾：续跑。Custom Model 偶尔把 `<｜DSML｜ ...>`、`<antml:...>` 这类片段当成正文输出，回合就此结束，工具并没有真的调用。只认落在正文末尾的标记，正文中间引用、或整段写在代码块里讨论都不会命中。
- 后台任务还在这一轮里跑：工具结果里有一条不长的记录，写明 `Status: running` 且 `Task is still running`，或写明不要用 `TaskOutput` 轮询，并且之后没有这个任务 id 的 `Status: completed`，也没有 `Background task completed` 且 `reason: completed` 的完成通知。模型已经写出完整正文时，待办未完成也不续跑，按正常结束打印「本轮任务正常完成。」中间夹着 `TodoWrite` 的结果也一样。新的用户消息会清掉这个等待。用户自己贴出这些句子、超长工具结果、压缩摘要、截断正文和工具协议泄漏，仍按各自的规则。空回复只看紧前面的那一条工具结果，不因为更早的后台任务而跳过静默中断。
- 待办列表仍有 `in_progress` / `pending`：续跑，`reason` 里会列出未完成项，并要求先对账（已完成的标 `completed`，还缺什么就说清）。证据只取 transcript 里 Droid 写的 `todo_state` 记录，插件不解读任何字样。这个记录有两种形态：模型按契约传结构化 `todos` 时，直接读每项的 `status`；模型把整份清单当字符串传时，Droid 可能逐行解析成带 `status` 的条目，也可能解析不出而把整行原样留下、状态落成默认的 `pending`。判别不靠状态词表，只看解析是否真的发生：解析成功的那一行，条目的 `content` 一定比原行短（状态标记和行首序号被剥掉）；解析失败的那一行，`content` 与原行只差行首序号。只要有任意一行两者相等，就说明这一行的状态是默认值，整份清单弃权。行数与条目数对不上、找不到对应的 `todo_state` 时同样弃权。此外，最后一次 `TodoWrite` 之后只要还发过工具调用，这份计划就算过时（模型常常先更新计划、再干完收尾的活、然后直接写总结），也弃权。弃权等于不续跑，因此模型怎么写待办都不会触发误判，代价是拿不到这份证据。
- 正文以提问收尾：不续跑。末尾是 `?` / `？`，或结尾出现「请确认」「是否需要」「要不要」这类说法，视为模型在等你回答。此时即使待办未完成也停住。
- 其他可见正文：不续跑。Droid 的 TUI 里这条 Stop hook 下面会多一行「本轮任务正常完成。」Codex 把非 JSON 输出当成钩子失败，所以正常结束时什么都不打印。
- 已经发出工具调用：不续跑，交给 Droid 执行工具。
- 出现无法识别的内容块：不续跑。
- transcript 还没写出这条消息、路径不可读，或 Stop payload 缺字段：不续跑。
- 采样请求因限流、5xx 或连接中断失败，回合停在一条只展示给用户的供应商错误上：续跑。系统消息形如 `Hook message: Custom Model 因 API 失败中断，正在自动重试 1/4...`。`Hook message:` 是 Droid 加的前缀。认证失败、配额、上下文超限、内容策略、模型不存在和请求参数错误不续跑。

同一轮里连续自动续跑默认最多 4 次。判定「连续」依据 transcript 里的最后一次工具调用：再次 Stop 时如果已经出现了新的工具调用，说明模型在两次中断之间真的干了活，连续次数从 1 重新开始计；全新的用户消息（`stop_hook_active` 为 false）同样从 1 开始。只看 `stop_hook_active` 不够：实测 Droid 在整个续跑链里始终把它置为 true，跑完 104 次工具调用之后的一次中断仍会被算成连续第 3 次，次数很快用完，之后真正的压缩摘要中断就不再续跑。所以插件自己比对 transcript 里的工具调用 id。次数用完后停住；下一条新的用户消息会重新开始计数。主界面不显示 thinking，所以静默中断看起来像是工具输出之后直接停住。

提问判断是文本启发式，不是语义理解。待办判断不看 assistant 正文，也不解析模型写的自由文本：它只比较「Droid 是否真的解析了每一行」这个结构事实，再读 `todo_state` 里已经成型的状态。模型如果一边说「接下来我会继续」一边结束回合，插件不会续跑；反过来，模型如果忘了把待办标成完成，可能被多续跑一次——续跑提示会先要它对账，属于这种情况时一般一轮就收住。上限 4 次保底。

## 不会处理的情况

- 回合一直挂着、没有进入 Stop。Hook 没有机会运行。
- 你主动取消回合。Droid 发的是 Notification，不是 Stop。
- 子代理的 SubagentStop。插件只注册主会话的 Stop。

自动续跑不是事务。续跑提示要求先核对已经成功的修改和命令，再决定要不要重做。

## Droid 上的 API 失败

Droid 在供应商请求失败时仍然进入 Stop。失败文字先写入 transcript，`agent_turn_outcome` 要等钩子返回之后才追加，所以插件不读 outcome。它只看最新一条真实消息，并跳过钩子自己的执行记录：

- 必须是 `visibility` 为 `user_only` 或 `both` 的用户消息，正文像 `BYOK Error:`、`错误：` 这类供应商报错。
- `llm_only` 的后台任务错误不是这一轮的结尾，不续跑。你自己发的消息也不是。
- 可重试的范围和 Codex 相同：429、500、502、503、504，以及网络中断、流被掐断、套接字意外关闭。Droid 的原文经常不带状态码，例如 `Your request to the LLM failed due to network issues`、`stream_read_error`、`Unexpected EOF`、`socket closed unexpectedly`、`no usable output`，这些也算。
- 续跑用 `decision: "block"`，不另起 `droid exec`。当前进程还握着模型、自主级别和权限，再拉一个进程只会和它抢会话。
- 退避睡在 Stop hook 里：默认 5 秒起，每次乘 2，封顶 40 秒。`hooks.json` 的超时是 70 秒，给这次等待留出余量。次数和 Codex 共用 `DROID_AUTO_CONTINUE_FAILURE_MAX_ATTEMPTS`，按会话累计。同一条 transcript 记录重复上报不记第二次；同一句错误换了一条新记录，算下一次。

这不是重放刚才那次 HTTP 请求。模型会看到一条对账说明，先核对已经成功的修改，再从中断处继续。

## Codex

Codex 的 transcript 是 rollout。一次采样里的思考是单独的 `reasoning` 项，工具调用是 `function_call`、`local_shell_call` 等项，正文在 assistant `message` 的 `output_text` 里。插件看的是最后一条用户消息或工具结果之后的那一次采样。

和 Droid 的差别：

- 模型名用 Stop payload 里的 `model`，不读 `.factory/settings.json`。
- 待办读 `update_plan`（或名为 `TodoWrite` 的函数调用）里结构化的 `plan[].step` / `status`。入参不是结构化清单时弃权。最后一次计划之后又有工具调用，同样弃权。续跑提示里要求用 `update_plan` 对账。
- 只有 `compaction` / `context_compaction`、没有正文时，按压缩摘要续跑。
- 正常结束不打印「本轮任务正常完成。」`systemMessage` 仍会输出，Codex 把它显示在钩子卡片上。
- 子线程的 SubagentStop、用户取消时的 Interrupt 都不注册。

### API 失败（429、5xx、流中断）

Droid 走上面的 Stop 路径。下面只讲 Codex。

这是 Codex 侧最容易漏掉的一类中断：**采样请求在 API 层失败时，Codex 不发 Stop，也不发任何其它回合结束事件**。`core/src/session/turn.rs` 里的重试循环在错误分支直接结束回合，`run_turn_stop_hooks` 只挂在正常收尾的路径上。钩子消费方会看到「用户提交了提示词，然后什么都没有」，误以为会话还在忙。

Codex 唯一能观测到这类失败的是 `PostModelRequest`：它是观察型钩子，失败时带 `status: "failed"` 和 `error` 文本，插件注册了它。hook 输出的 `systemMessage` 只用于在 TUI 上显示提示，不能证明后台辅助进程已经投递消息，也不能证明会话已经恢复；恢复结论只由 rollout 的新 `task_started` 决定。

拿到可重试失败后，插件按指数退避（默认 5 秒起，每次乘 2，封顶 40 秒），再拉起一个脱离 hook 的后台辅助进程。辅助进程采用 queue-first，只有 queue 明确不可用时才回退到 exec resume。同一 session 和 `turn_id` 的多个不同错误共享一个恢复 job，不会因为 429 之后又报流中断而重复投递；进入新的 turn 后才会再次尝试。没有 `turn_id` 时仍按错误指纹去重：

1. **queue-first**：执行
   ```bash
   codex queue --thread <session_id> --message <续跑提示> --resume
   ```
   把一条对账提示持久投递到原 thread。queue 使用原 thread 的 model、provider、cwd 和权限，不抢 `thread-writer-locks/<session>.lock`，也不需要 `resume_launch_overrides()`。TUI 长期持有写锁时，queue 仍可把消息写进共享 app-server 队列，由已加载的线程在 idle 后启动。

2. **验证新 turn**：命令返回 0 只表示消息已经持久入队，不能直接记为恢复。辅助进程读原 rollout，只有看到与失败 turn 不同的 `turn_id` 的新 `task_started`，才把 job 记为 `recovered`。`DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS`（默认 10 秒）只是辅助进程退出前的短观察窗口；没有看到新 turn 时，job 保持 `queued`，`verify_until` 按 `DROID_AUTO_CONTINUE_QUEUE_VERIFY_SECONDS`（默认 900 秒）设置待核对期限。后续任意 Stop 或 `PostModelRequest` hook 会按持久化的 transcript 路径、偏移和失败 `turn_id` 继续读取 rollout，看到新 turn 后转为 `recovered`。`DROID_AUTO_CONTINUE_QUEUE_TIMEOUT_SECONDS`（默认 60 秒）内 queue 命令没有结束，或其它非零结果的含义不明确时，也保留 `queued` 待核对，不自动 fallback，也不重复投递。

3. **exec fallback**：只有 queue 明确不受支持（例如未知子命令、`thread/queue/add` 不存在或 `method not found`）时，辅助进程才等待写锁释放，然后执行 `codex exec resume <session_id> <续跑提示>`。exec 若报 `already has an active writer` 且 rollout 还没有新 turn，会再补一次 queue。等待写锁超过 `DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS`（默认 180 秒）时，job 落成 `blocked_active_writer`，`detail` 和事件日志保留原因，不会静默退出。

`codex queue` 依赖 Codex 0.169+ 的 CLI 和底层的 `thread/queue/add`。命令返回 0 只证明消息已持久入队；超时和非零只说明结果未知，服务端可能已经入队，因此不能据此再发一次 exec。只有明确的“不支持 queue”错误才允许 fallback。恢复结论仍只来自 rollout 的新 `task_started`。codex 可执行文件或辅助进程无法启动时，状态记为 `failed_to_launch`，不会误报为已恢复。

**为什么要从完整 JSONL 行开始观察**：job 创建时，插件把 transcript 路径、失败 `turn_id` 和当前完整 JSONL 行的末尾写入 `resume_jobs`。直接保存文件大小可能落在半行中间；下一次从那里读取会丢掉这行的开头，若它恰好是新 turn 的 `task_started`，就会漏报恢复。后台观察和后续 hook 对账都保留末尾不完整行，下一轮从同一偏移重读，等整行写完后重新解析。

**为什么不直接重新发请求**：Codex 没有这个入口。`turn/start` 虽然接受空 `input`，但没有 CLI 或 app-server 方法让外部进程触发「零消息重采样」；`ContinueIfIdle` 那种空唤醒只在内部恢复路径（`daemon_continuation.rs`）使用。所以续跑必然要带一条消息。指令写成对账式，先核对已完成的修改再继续，避免重做有副作用的操作。

**为什么要自己等**：Codex 自己的重试基数是 200ms（`async-utils/src/backoff.rs`），默认最多 4 次请求 + 5 次流重连，总窗口约 3 秒，对 429 的限流窗口远远不够。它还有一个 `unbounded_connection_retries`（默认开启），但只覆盖 `ConnectionFailed`，不含 429。

**为什么要脱离 hook 进程**：退避和等锁都在一个独立的后台进程里做。原因有两个。

其一，hook 有自己的超时，睡在 hook 里会挤占 Codex 的收尾时间。

其二，也是更关键的：插件在 `PostModelRequest` 里运行时，发起回合的 Codex 进程**仍然持有该会话的写锁**（实测 `PostModelRequest` 执行期间 `thread-writer-locks/<session>.lock` 是 HELD，进程退出才释放）。此时直接执行 `codex exec resume` 会被拒绝。queue 路径不依赖这把锁；只有 fallback 才由后台进程轮询共享锁，默认最多等 `DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS`。

**回退路径为什么要还原启动参数**：`codex exec resume` 是**新进程**，只从 `config.toml` 和自身参数取值。原进程那些只存在于命令行的设置会全部丢掉，实测有两类后果：

1. 自定义 provider（很多网关的启动脚本这么写）丢失，报 `model provider is no longer configured`。
2. **沙箱静默降级**：原进程用 `-s danger-full-access` 启动时，续跑进程会退回默认的 `read-only`，模型接着写文件、跑命令全部失败。

插件往上找最近的 codex 进程，把它的 `-c` / `--profile` / `-s` / `--sandbox` / `--ask-for-approval` / `--dangerously-bypass-approvals-and-sandbox` 全部还原。`codex exec resume` 不接受 `-s/--sandbox`，所以沙箱换成等价的 `-c sandbox_mode="..."`。

覆盖必须在 **hook 进程内**算好再传给后台进程——后台进程跑起来时 hook 已退出，父进程已经换人，那时再算什么都拿不到（这个坑实测踩过）。

读取原进程命令行分平台：

| 平台 | 方式 | 说明 |
| --- | --- | --- |
| Linux | `/proc/<pid>/cmdline` | NUL 分隔的原始字节 |
| macOS | `sysctl(KERN_PROCARGS2)` | 通过 `ctypes` 调用；NUL 分隔的原始字节 |
| Windows | 无 | 退化成只用 `config.toml` |

macOS 这一档**不能**用 `ps -o args=`。它把 argv 用空格拼成一行，参数内部的空格无法还原。`apus_codex.sh` 传的就是 `-c 'model_providers.azure-openai={name="Azure OpenAI",...}'`，按空格切会得到 `name="Azure` 和 `OpenAI",...}` 两段，续跑进程拿到一个坏掉的 provider 定义。`KERN_PROCARGS2` 返回的是原始字节，能精确还原每一个参数。

**已知限制**：queue 成功不等于恢复。如果原 TUI/app-server 已经不存在、线程没有 runtime 消费队列，或者消息没有在待核对期限内启动新 turn，最终状态仍不是 `recovered`。超过 `DROID_AUTO_CONTINUE_QUEUE_VERIFY_SECONDS` 仍未见新 turn 的待核对 job、fallback 等写锁超时留下的 `blocked_active_writer`，以及 exec 正常退出但没有确认新 turn 的 `completed_unverified`，都不能当成已恢复。应结合 rollout 的 `task_started`、`resume_jobs.state` / `detail` 和 `${DROID_AUTO_CONTINUE_STATE_DIR}/logs/<job_id>.jsonl` 判断。日志目录不可写时，辅助进程仍会启动，只是降级为没有事件日志。Windows 上因为读不到命令行，只有把 provider 写在 `config.toml` 里才能让 exec fallback 续跑成功；queue 路径仍沿用原 thread 配置。

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
| `DROID_AUTO_CONTINUE_FAILURE_MAX_ATTEMPTS` | `4` | `0-10`。Droid 和 Codex 的 API 失败共用这个次数，`0` 关闭 |
| `DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY` | `5` | API 失败后的第一次等待秒数 |
| `DROID_AUTO_CONTINUE_FAILURE_MAX_DELAY` | `40` | API 失败后的单次等待上限，硬上限 40 秒 |
| `DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS` | `180` | `0-3600`。exec fallback 等待 thread writer lock 的上限；超时状态为 `blocked_active_writer`，`0` 表示立即判定 |
| `DROID_AUTO_CONTINUE_LOCK_POLL_SECONDS` | `1` | `0.05-10`。等待写锁的轮询间隔（秒） |
| `DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS` | `10` | `0.05-120`。辅助进程退出前观察 rollout 出现新 turn 的短窗口（秒）；未见新 turn 时 job 保持 `queued` 待后续对账 |
| `DROID_AUTO_CONTINUE_QUEUE_VERIFY_SECONDS` | `900` | `0.05-86400`。`queued` job 的待核对期限（秒），写入 `verify_until`；后续 hook 可在此期限内观察新 turn |
| `DROID_AUTO_CONTINUE_QUEUE_TIMEOUT_SECONDS` | `60` | `1-300`。`codex queue` 命令超时（秒）；超时视为结果未知，不重复投递 |
| `DROID_AUTO_CONTINUE_RESUME_TIMEOUT_SECONDS` | `3600` | `1-86400`。exec fallback 的最大运行时间（秒）；也用于回收遗留未完成 job |

`DROID_AUTO_CONTINUE_BACKOFF_FACTOR` 同时用于这两条退避路径。

`DROID_AUTO_CONTINUE_STATE_DIR` 默认是 `${XDG_STATE_HOME:-~/.local/state}/custom-model-stop-patch`。状态库是其中的 `streaks.sqlite3`：`streaks` 记录连续中断和 API 失败次数；`resume_jobs` 记录每次恢复尝试的 session/turn/error 哈希、transcript 路径和完整 JSONL 行偏移、失败 `turn_id`、基线时间、`verify_until`、统一状态、派发/API 尝试次数、helper PID、退出码、是否看到新 turn、`detail` 和时间戳。同 session 和 turn 的 job 以 turn 标识去重，不同错误只更新同一个 job 的诊断信息。每次 job 的事件写入 `logs/<job_id>.jsonl`，其中命令和输出尾部会做敏感信息清理；日志目录不可写不会阻止辅助进程启动，只会失去这份事件日志。

从 0.5.0 升级到 0.5.1 时，第一次打开状态库会在同一个事务里重建旧的 `resume_jobs` 表，补上包含 `queued` 的 CHECK；已有 job、后加列和 `resume_jobs_scope_created` 索引都会保留。迁移失败则整次回滚，不会留下删掉旧表的新库。

`resume_jobs.state` 使用以下状态：

| 状态 | 含义 |
| --- | --- |
| `scheduled` | hook 已建立恢复 job 并拉起辅助进程，等待后续 queue 或 fallback |
| `queued` | queue 命令已启动、返回成功或结果未知；消息可能已持久入队，但不代表新 turn 已开始。短窗口未见新 turn 时保持此状态，后续 hook 继续核对直到 `verify_until`；期限到后仍未确认则转为 `completed_unverified` |
| `waiting_lock` | 辅助进程已启动并准备 queue，或 queue 明确不受支持后等待 exec fallback 所需的 thread writer lock |
| `dispatching` | exec fallback 已启动 |
| `recovered` | rollout 出现与失败 turn 不同 `turn_id` 的 `task_started` |
| `completed_unverified` | exec fallback 正常退出却没有新 turn，或 `queued` 的待核对期限到期仍没有新 turn；这不是 queue settle 的恢复结论 |
| `blocked_active_writer` | exec fallback 等写锁超时 |
| `failed` | exec 返回非零、超时，或辅助进程以其它方式失败 |
| `failed_to_launch` | codex、辅助进程或派发进程无法启动，恢复未真正发出 |
| `superseded` | queue、等锁或派发前发现已有新 turn，当前 job 不再需要动作 |

诊断时先看 `resume_jobs.state`、`detail` 和 `verify_until`，再用 `job_id` 打开对应日志。`queued` 表示消息可能已入队但新 turn 尚未得到确认；`blocked_active_writer` 表示 fallback 没等到写锁；`completed_unverified` 表示 exec 正常退出或 `queued` 待核对期限到期，但仍没有确认新 turn。这些状态都不能当成成功，恢复只以 rollout 中不同 `turn_id` 的 `task_started` 为准。日志里可关注 `queue_*`、`lock_wait_*`、`dispatch_*`、`superseded*` 事件；日志目录不可写时没有这份事件记录。

## 复核待办判据（开发用）

`tools/audit_todo_evidence.py` 拿真实 transcript 复算待办判定，用来判断误报和漏判，不修改任何状态：

```bash
python3 tools/audit_todo_evidence.py --since 2026-09
python3 tools/audit_todo_evidence.py --since 2026-10 --tail 300
python3 tools/audit_todo_evidence.py --json > audit.json
```

输出会列出被判续跑的会话、它们的未完成项、收尾正文，以及 `work_after_call` 的反事实结果：如果把这条过时弃权关掉，哪些会话的判定会变。判断误报的办法是看收尾正文有没有已经把结果交付出来——交付了却仍被判续跑，就是误报。默认只看 `todos`，用 `--verdict` 可以加上 `complete`、`tool_use` 等其他判定。
