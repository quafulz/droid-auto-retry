# droid-auto-retry

面向 **Factory Droid** 和 **Codex** 的自动重试插件，插件名：[custom-model-stop-patch](plugins/custom-model-stop-patch/README.md)。

## 解决什么问题

在使用 Custom Model（自定义模型/第三方 API 网关）进行长程编码任务时，Agent 常常在任务还没真正做完时意外停下来，导致用户需要反复手动输入「继续」或重新发起会话。常见中断包括：

- **静默或半途中断**：只输出了思考过程或空白内容、上下文自动压缩后停在摘要、代码/标签被截断、工具调用协议片段泄漏到正文。待办清单没勾完不算中断。
- **可重试的 API 层错误**：遇到限流（429）、服务端异常（5xx）或网络/流连接中断时，原生重试窗口过短或直接退出回合。

本插件在检测到上述「假结束」或可重试故障时，自动以指数退避的方式触发续跑；同时对真正需要人工介入的场景（如模型主动提问确认、认证失败、配额耗尽、上下文超限、内容策略拦截等）保持静默，不盲目重试。

## 原理

插件不重放底层 HTTP 请求，而是通过 Droid 和 Codex 的 **Hook 机制** 结合本地会话记录（transcript / rollout）判断回合是否真正完成，并在下一轮中要求模型先对账已完成的操作再继续：

1. **未完成回合续跑（Stop Hook）**
   在每次回合停止时检查最后一条输出。若命中未完成特征，插件会阻塞本次停止并自动注入续跑提示；若模型已经写出普通正文、后台任务仍在运行，或已达到连续重试上限，则正常放行。
2. **API 层失败自动恢复**
   - **Droid**：API 失败仍会进入 Stop 阶段，插件确认属于可重试错误后，在当前会话进程内退避并拉起下一轮。
   - **Codex**：API 失败不会触发 Stop，插件通过 `PostModelRequest` 监听失败信号，并在独立后台进程中完成退避与恢复——优先将续跑消息安全写入原会话队列，仅在不支持队列时回退到 `exec resume`（自动继承原进程的模型、Provider 与沙箱设置），并以会话记录中出现新 turn 作为恢复确认依据。

## 快速开始

### 环境要求

- Factory Droid CLI 或 Codex CLI
- Python 3.10+（需可通过 `python3` 调用）

### 安装

**Droid：**

```bash
droid plugin marketplace add https://github.com/quafulz/droid-auto-retry
droid plugin install custom-model-stop-patch@droid-auto-retry
```

**Codex：**

```bash
codex plugin marketplace add https://github.com/quafulz/droid-auto-retry
codex plugin add custom-model-stop-patch@droid-auto-retry
```

### 使用与验证

安装完成后，**新开一个会话**即可自动生效（无需修改日常使用方式）：

- 在 **Droid** 中可通过 `/hooks` 确认 Stop hook 已启用；后续通过 `droid plugin update` 更新插件通常无需重启会话。
- 在 **Codex** 中安装或更新后建议新开一个会话。
- 如需调整最大重试次数、退避时间或状态目录等行为，可通过环境变量配置。

## 详细文档

完整的判定规则、边界与不处理场景、Codex 后台恢复状态机、全部环境变量说明及排障方法，请查阅：

- [custom-model-stop-patch 完整文档](plugins/custom-model-stop-patch/README.md)

## 开发与测试

```bash
python3 -m pytest -q
```
