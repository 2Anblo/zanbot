# 命令行体验与 session 核心层设计方案

状态：C1、C2、C3 已实现。分支：`feature/cli-ux`。

## 目标

1. 命令行好用：输入 `/` 就能看到命令和说明，用 Tab 补全；底部状态栏显示当前状态；记忆提示**实时**出现，不用等下一次回车。
2. 把对话逻辑从 `main.py` 抽成和界面无关的核心层，以后浏览器端和桌面端可以直接复用。

非目标（本期不做）：Markdown 渲染、Web 服务、桌面端、主题配色。

## 现状的问题

读了现在的 `main.py`，问题集中在三处：

1. **逻辑和显示混在一起。** 对话循环、`/clear`、记忆命令、后台提示都直接 `print`，换一个界面就得全部重写。
2. **命令散落在各处。** `/exit`、`/clear` 写在循环里，记忆命令在 `handle_memory_command` 里，启动提示又是另外手写的。补全、`/help` 和真正的分发逻辑没有共同的来源，很容易对不上。另外，**输错的命令会被当成普通消息发给模型**，比如 `/memroy` 会被原样发出去。
3. **交互受限于 `input()`。**
   - 记忆提示只能在你回车之后才显示：后台线程不能在你打字时往终端上 print，否则会把输入行搅乱。
   - 在回答流式输出时按 Ctrl+C 会**直接退出程序**。测试 `test_ctrl_c_closes_response_and_preserves_history` 确认了异常会一路往外抛。
   - 没有补全，也没有历史记录；多行粘贴容易被拆成好几条消息分别发送。

## 总体结构

```
main.py            参数解析、登录相关子命令、组装 session、选择前端
  │
  ▼
zanbot/cli.py      终端前端：prompt_toolkit（交互式终端）或 input()（管道/测试）
  │   只负责：读输入 → 交给 session → 把事件画出来
  ▼
zanbot/session.py  ChatSession：对话历史、system prompt、记忆、命令分发
zanbot/commands.py 命令注册表：名称、参数提示、说明、处理函数、补全来源
  │
  ▼
agent / llm / memory / memory_extract   保持不变
```

依赖方向是单向的：`session` 不知道终端的存在，`cli` 不直接碰 `messages`、`store` 和 `worker`。

## session 核心层

### 事件

session 通过回调 `emit(event)` 把发生的事告诉前端。事件是纯数据，以后 Web 端可以直接序列化成 JSON，通过 SSE 或 WebSocket 推给浏览器。

| 事件 | 字段 | 含义 |
|---|---|---|
| `ReplyStarted` | — | 开始生成回答（CLI 打印 `Zanbot: `） |
| `TextDelta` | `text` | 流式文本片段 |
| `ReplyFinished` | `text`, `truncated` | 本轮成功，已写入历史 |
| `ReplyFailed` | `message`, `interrupted` | 本轮失败或被取消，历史未改变 |
| `Info` | `lines` | 命令的输出（比如 `/memory` 列表） |
| `MemoryNotice` | `text` | 记忆变更或提取失败（**来自后台线程**） |
| `Cleared` | — | 对话已清空 |

用回调而不是生成器，是因为 `run_turn` 本身就是用 `on_text` 回调实现流式输出的，可以直接接上。

### ChatSession 接口

```python
class ChatSession:
    def __init__(self, config, store=None, worker=None, emit=print_event): ...
    def submit(self, text): ...         # 普通消息或命令，同步执行，期间不断 emit
    def cancel(self): ...               # 取消正在生成的回答（Ctrl+C）
    def close(self, timeout): ...       # 等待后台提取完成，返回是否完成
    @property
    def status(self) -> Status: ...     # provider、model、记忆条数、自动记忆开关、是否有提取在进行
```

现在 `main.py` 里的循环体（拼 system prompt → `run_turn` → 提交提取任务）整体搬进 `submit()`。`messages` 和 `session_id` 归 session 所有，`/clear` 也在 session 内部处理。

**后台提示的变化。** `MemoryWorker` 新增一个可选回调 `on_notice`，session 把它接到 `emit(MemoryNotice(...))` 上。这样一来，前端可以在提示到达时**立即**显示（prompt_toolkit 能安全地在输入行上方打印），而不必在两次输入之间去拉取。`take_notices()` 继续保留，给 `input()` 的降级前端使用。

**取消回答。** 实现时没有单独的 `cancel()`：`submit()` 自己处理 `KeyboardInterrupt`。请求放在单独的线程里执行，主线程每 0.1 秒 join 一次，所以随时能收到 Ctrl+C。之所以这样做，是因为在 Windows 上 Ctrl+C 打断不了阻塞中的 socket 读取，回答还在等第一个字时会按不动。取消后，session 立刻 emit `ReplyFailed(cancelled=True)` 并回到提示符；被丢下的线程会在下一个文本片段处停下（`on_text` 抛出 `ReplyCancelled`），或者等到自身超时结束。`run_turn` 只修改历史的副本，只有没被取消的轮次才会采用这份副本，所以取消后历史不变，也不会提交记忆提取任务。命令执行中按 Ctrl+C 会提示 `Cancelled.`。以后做 Web 端时，可以把同一个取消标志暴露成 `cancel()`。

## 命令注册表

所有命令在 `zanbot/commands.py` 里统一登记一次，补全、`/help`、分发和启动提示都从这里读取：

```python
@dataclass(frozen=True)
class Command:
    name: str                # "/forget"
    args: str                # "<id>"
    help: str                # "Delete a memory and erase its text"
    run: Callable[[ChatSession, str], None]
    complete: Callable[[ChatSession, str], Iterable[Suggestion]] | None = None
    needs_memory: bool = False
```

| 命令 | 参数补全 |
|---|---|
| `/help` | —（新增，列出所有命令） |
| `/clear` | — |
| `/exit` | — |
| `/memory` | `on` / `off` |
| `/remember` | 类别：profile、preference、project、event、relationship |
| `/forget` | 已有记忆的 id，旁边显示内容预览：`7  用户在学 Rust` |

**未知命令**：以 `/` 开头但没有登记的输入，不再发给模型，而是提示 `Unknown command /memroy. Did you mean /memory? Type /help for all commands.`。"Did you mean" 用标准库 `difflib.get_close_matches` 实现。如果确实要发一条以 `/` 开头的消息，在前面加一个空格即可。

## 终端前端（prompt_toolkit）

依赖：`prompt_toolkit>=3.0,<4`。它是纯 Python 实现，原生支持 Windows 控制台（PowerShell、Windows Terminal、cmd）。

**补全**
- 只有当输入以 `/` 开头时才自动弹出补全，避免平常用中文输入法打字时不停冒出候选。
- 补全菜单里每一项都附带说明（`display_meta`），比如 `/forget  <id>  Delete a memory`。
- 补全的数据都来自命令注册表。`/forget` 的候选来自 `store.active()`，只在补全被触发时才查询。

**底部状态栏**

```
 chatgpt · gpt-5.x · 12 memories · auto-memory on · saving…        /help
```

最后的 `saving…` 只在后台提取进行时出现。

**实时提示**：用 `patch_stdout()` 包住整个会话。后台线程 emit 的 `MemoryNotice` 会显示在输入行**上方**，你正在输入的内容不受影响。

**按键**

| 按键 | 行为 |
|---|---|
| Enter | 发送 |
| Shift+Enter / Ctrl+Enter | 换行（Windows 控制台无需配置） |
| Ctrl+J | 换行（所有终端通用的备用键） |
| 粘贴多行文本 | 作为一条消息（依赖 bracketed paste） |
| ↑ / ↓ | 输入历史 |
| Ctrl+C（输入时） | 清空当前输入行；空行时再按一次退出 |
| Ctrl+C（回答时） | 取消本轮回答，回到提示符 |
| Ctrl+D | 退出 |

**Shift+Enter 换行**：最初的方案是让终端映射 Shift+Enter，但实测 Windows Terminal 后改了做法。prompt_toolkit 在 Windows 10+ 上默认使用 VT 输入模式，这种模式下按键只是字符：Enter 和 Shift+Enter 都是 `\r`，Ctrl+Enter 和 Ctrl+J 都是 `\n`，Shift+Enter 根本分辨不出来。Legacy 模式的按键事件则带有修饰键状态：Shift+Enter 是 `\r` 加 Shift，Ctrl+Enter 是 `\n` 加 Ctrl。

所以在 Windows 上，`windows_console_input()` 改用 Legacy 模式读取（`ConsoleInputReader` 的子类），把 Shift+Enter 转成 Meta+Enter；Ctrl+Enter 由 prompt_toolkit 自己转成 Meta+Ctrl+J。这两个序列都绑定为换行，不需要修改任何终端配置。代价是依赖了 prompt_toolkit 的内部类和私有属性，所以依赖版本限定为 `<4`，并有测试覆盖。Ctrl+J 作为所有终端通用的备用键。非 Windows 终端如果要用 Shift+Enter，可以在终端里映射为发送 Escape+Enter。

**输入历史**：保存在 `%LOCALAPPDATA%/zanbot/history.txt`，和其他数据一样放在仓库之外，见文末"待决定"。

**降级**：stdin 或 stdout 不是交互式终端时（管道、重定向、测试），自动退回到现在的 `input()` 循环，也可以用 `--plain` 强制使用。Git Bash 的 mintty 不是原生 Windows 控制台，prompt_toolkit 在里面无法正常工作，这种情况也走降级。

## 测试

| 层 | 怎么测 |
|---|---|
| session | 直接构造 `ChatSession`，mock `create_message`，断言 emit 出来的事件序列。不涉及终端，原先 CLI 测试里关于对话、记忆和 `/clear` 的大部分断言都迁移到这里。 |
| 命令注册表 | 分发、未知命令提示、"Did you mean"、`/help` 输出。 |
| 补全 | 用 prompt_toolkit 的 `Document` 直接调用 completer，断言候选和说明文字。 |
| 交互前端 | 用 `create_pipe_input()` 加 `DummyOutput` 模拟键入，覆盖 Enter 发送、Ctrl+C 取消、实时提示。 |
| 降级前端 | 保留现有那组 patch `builtins.input` 的测试，确保 `--plain` 和管道行为不变。 |

现有的 57 个测试在第一阶段（纯重构）结束时必须全部通过，CLI 测试只允许修改入口参数。

## 分期

1. ✅ **C1 抽出 session 和命令注册表（纯重构）**：仍然使用 `input()`，用户看到的行为不变，只多了 `/help` 和未知命令提示。这一步风险最低，测试也最容易补齐。
2. ✅ **C2 接入 prompt_toolkit**：补全、状态栏、实时提示、输入历史、降级逻辑。
3. ✅ **C3 按键细节**：Ctrl+C 取消回答。

## 以后对接 Web 和桌面端

- 每个对话对应一个 `ChatSession`。`MemoryStore` 每次操作都打开新连接，可以被多个 session 共享。
- Web 端把 `emit` 换成"推送到 WebSocket"即可，事件是纯数据，可以直接序列化。命令注册表也能直接生成网页上的命令菜单。
- 需要补充的是 `submit()` 的线程或异步模型：CLI 里同步调用就够了，Web 服务需要在线程池里运行它，或者改写成异步。这一点等真正做 Web 时再定。

## 决定

1. 输入历史保存到 `%LOCALAPPDATA%/zanbot/history.txt`，提供 `--no-history` 只在内存里保留本次会话的历史。
2. 改 Ctrl+C 语义：回答时取消本轮，输入时清空当前行（空行再按一次退出），Ctrl+D 或 `/exit` 退出。
3. 拦截未知的 `/` 命令，给出 "Did you mean" 提示；需要发送以 `/` 开头的消息时，在前面加一个空格。
4. Shift+Enter 和 Ctrl+Enter 都换行。Windows 上用 Legacy 模式读取按键，靠修饰键状态识别，不需要终端映射。
