# TraceCoder

TraceCoder 是一个从零实现的本地编程智能体。它直接使用模型原生 tool calling，自行维护消息历史、校验并执行本地工具、压缩上下文、判断循环终止，并把全过程写成 JSONL 轨迹。项目没有使用 LangChain、OpenAI Agents SDK 等 Agent 框架，也不依赖服务端代码执行或文件工具。

## 架构

```text
CLI / 环境配置
       |
       v
有界 Agent 循环 ----> OpenAI-compatible 模型适配器
       |
       +----> 工具注册表 ----> 5 个文件工具 + 1 个命令工具
       |
       +----> 上下文压缩 / 运行时验证状态 / JSONL 轨迹
```

核心模块：

- `agent.py`：模型—工具循环、重复调用检测、最大步数和验证状态。
- `tools/`：目录浏览、文本搜索、读取、写入、精确替换、命令执行。
- `context.py`：保留任务、运行时事实和完整 tool-call/result 组合的确定性压缩。
- `trace.py`：带会话 ID、序号、时间和脱敏的 JSONL 事件轨迹。
- `llm/`：核心协议与 OpenAI-compatible Chat Completions 适配器。

## 安装

需要 Python 3.11+。推荐在虚拟环境中安装：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

配置模型。不要把真实 key 写进仓库或演示画面：

```powershell
$env:TRACECODER_API_KEY = "your-key"
$env:TRACECODER_BASE_URL = "https://api.openai.com/v1"
$env:TRACECODER_MODEL = "your-tool-calling-model"
```

也兼容 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`。

## 使用

```powershell
tracecoder run "阅读项目，修复失败测试并验证" --workspace .
tracecoder trace .tracecoder/traces/<session-id>.jsonl
```

命令默认逐次确认，确认界面展示实际执行的 `argv` 和工作目录；`--yes` 会自动批准命令，并授予命令当前用户已有的主机权限。需要 Shell 语法时，模型必须显式请求 `cmd /c` 或 `sh -lc`，不会发生隐式解释。

## 安全边界

- 文件工具只接收相对路径，规范路径必须位于工作区；`.git/` 与 `.tracecoder/` 对模型文件工具保留。
- 命令使用 `shell=False`，有超时与运行期输出上限；子进程环境不继承 API key 等秘密变量。
- 命令不是操作系统沙箱：批准后仍可访问工作区外资源，超时只保证回收直接进程，不保证结束所有后代进程。
- 被读取的源码、提示词和命令输出会发送至配置的模型提供方；CLI 会在运行前显示 base URL 和模型，但不会显示 key。

## 开发验证

```powershell
python -m pytest -q
python -m tracecoder --help
python -m tracecoder run --help
```

测试使用假模型覆盖完整的读写—验证—完成循环，不需要真实 API key。

