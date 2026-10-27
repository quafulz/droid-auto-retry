# droid-auto-retry

Factory Droid 插件市场。目前只有一个插件：[custom-model-stop-patch](plugins/custom-model-stop-patch/README.md)。

Custom Model 的回合没有真正做完就停下来时（静默中断、压缩摘要、正文截断、待办未完成），Stop hook 会自动发送「继续」。

## 安装

需要 Droid CLI 和 Python 3.10+（命令是 `python3`）。

```bash
droid plugin marketplace add https://github.com/quafulz/droid-auto-retry
droid plugin install custom-model-stop-patch@droid-auto-retry
```

安装后新开一个会话，让 Droid 注册插件。用 `/hooks` 确认 Stop hook 已启用。

## 文档

判定规则、不会处理的情况、安装细节、环境变量和开发用的复核脚本，都写在[插件 README](plugins/custom-model-stop-patch/README.md)。那份是唯一来源，本文件只做入口。

## 开发

```bash
python3 -m pytest -q
```
