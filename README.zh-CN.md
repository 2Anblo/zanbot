# Zanbot

[English](README.md) | 简体中文

用 Python 和 requests 实现的终端多轮对话助手，支持 Anthropic API Key 和 Sign in with ChatGPT。

## 使用 ChatGPT plan（PowerShell）

```powershell
uv sync
uv run python main.py --login
uv run python main.py --models
uv run python main.py --provider chatgpt --model <slug-from-model-list>
```

执行最后一条命令前，将 `<slug-from-model-list>` 替换为 `--models` 返回的模型 slug。登录会打开浏览器，选择 **Continue with ChatGPT** 并授权 Zanbot 使用你的 plan。此模式不需要 Anthropic 或 OpenAI API Key。账户资格、模型权限和额度由 OpenAI 决定，登录成功不代表推理一定可用。在 [ChatGPT 设置](https://chatgpt.com/settings/usage) 中查看应用授权与用量。

可通过环境变量 `ZANBOT_PROVIDER=chatgpt` 和 `CHATGPT_MODEL` 设置默认值。未设置时仍使用 Anthropic。

- `--accounts`：查看已保存的账户注册信息和当前账户。
- `--add-account`：添加其他账户或工作区，保留已有注册。
- `--account <client-id>`：选择已保存的注册，再启动聊天。
- `--login`：使用当前注册重新登录；首次使用时注册新账户。
- `--logout`：尝试撤销远端会话并清除当前账户的本地 token；保留注册信息供以后登录。

Windows 凭据加密保存在项目目录外的 `%LOCALAPPDATA%/zanbot`，加密密钥由 `keyring` 存入系统凭据管理器。其他平台使用 `~/.config/zanbot`，需要配置安全的系统 keyring。程序自动续期，并通过文件锁防止多个进程同时刷新 token。续期失败或收到 HTTP 401 时，重新执行 `--login`。不要将 token 复制到项目文件中。

ChatGPT 模式通过公开的 Responses API 请求，设置 `store=false` 和 `stream=true`，每轮携带完整上下文，仅收到 `response.completed` 后保存问答。不调用 ChatGPT 网页内部接口，不读取其他应用的缓存凭据。目前 plan 共享接口不接受 `max_output_tokens`，因此 `Config.max_tokens` 仅用于 Anthropic。

详见[接入设计](docs/chatgpt-sign-in.md)和[官方登录文档](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)。自动化测试模拟登录和推理；需要真实浏览器授权并完成一次推理才能验证你的账户权限。

## 运行（PowerShell）

```powershell
uv sync
$env:ANTHROPIC_API_KEY = "你的 API 密钥"
uv run python main.py
```

若已设置 Windows 用户环境变量但当前终端未读取到：

```powershell
$env:ANTHROPIC_API_KEY = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")
```

默认模型沿用 `claude-sonnet-4-5`，可通过 `ANTHROPIC_MODEL` 环境变量修改。
不要将真实密钥写入项目文件。

- 输入文字并回车，开始或继续对话。
- 回答以流式方式逐段显示，无需等待整段生成完毕。
- `/clear`：清空历史，开始新对话。
- `/exit`、Ctrl+C 或输入结束：退出。
- 空输入会被忽略；失败的请求不会写入历史。

历史仅保存在内存，退出后消失。每轮发送完整历史，因此长对话会增加输入 token 用量，并可能达到上下文上限；可用 `/clear` 重置。
当前版本支持文本多轮对话和流式输出，尚未加入工具调用和持久化记忆。
收到完整的结束事件后才保存本轮问答；中途断流时，已显示的片段不会进入历史，可重新输入重试。

## 文件职责

- `main.py`：终端交互。
- `zanbot/config.py`：环境变量与配置。
- `zanbot/llm.py`：HTTP 请求与错误处理。
- `zanbot/streaming.py`：SSE 事件解析、文本增量回调和完整回答组装。
- `zanbot/agent.py`：会话历史与单轮对话。
- `zanbot/prompts.py`：系统提示词。
- `zanbot/chatgpt_auth.py`：OAuth、凭据加密、账户切换、续期和退出登录。
- `zanbot/chatgpt.py`：ChatGPT 模型列表和 Responses 流式请求。

## Token Monitor 接入

ChatGPT 回答完整结束且 API 返回用量时，自动生成 Codex 格式的 JSONL 用量日志。Windows 路径为 `%LOCALAPPDATA%/zanbot/usage/codex`，其他平台为 `~/.config/zanbot/usage/codex`。在 Token Monitor 中，将该绝对路径加入 **Codex 的自定义扫描路径**，并启用 Codex 统计。它没有原生 Zanbot 解析器，因此工具分类显示为 Codex；项目维度可按工作目录识别 Zanbot，模型维度显示实际返回的模型。

日志只包含时间、模型、工作目录和 token 计数，不包含提问、回答、邮箱或登录凭据。缓存输入和推理 token 分别是输入、输出的子集，不重复累加。同一段聊天的多轮问答共用一个会话日志，累加用量并按响应 ID 去重；执行 `/clear` 或重新启动程序时创建新会话。此前没有保存用量，不能补回历史请求；本适配器暂不导出中断、失败请求及 Anthropic 用量。

已使用本机 Token Monitor 0.66.0 的解析器验证真实 Zanbot 请求。显示费用是模型 API 单价估算，不是 ChatGPT plan 的账单或剩余额度。若启用了 Token Monitor 同步，这些用量日志也可能被同步。

实现位于 `zanbot/usage.py`。参考：[数据目录源码](https://github.com/Javis603/token-monitor/blob/main/src/shared/clientSources.js)、[自定义扫描目录源码](https://github.com/Javis603/token-monitor/blob/main/src/shared/customScanPaths.js)。

## 离线测试

```powershell
uv run python -m unittest discover -s tests -v
```

测试使用模拟 API，不消耗 API 额度。
