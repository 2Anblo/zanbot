# Zanbot

用 Python 和 requests 调用 Anthropic Messages API 的终端多轮对话助手。

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

## 离线测试

```powershell
uv run python -m unittest discover -s tests -v
```

测试使用模拟 API，不消耗 API 额度。
