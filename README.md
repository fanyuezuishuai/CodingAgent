# TraceCoder

TraceCoder 是一个从零实现的本地编程智能体。它直接使用模型原生 tool calling，自行维护消息历史、校验并执行本地工具、压缩上下文、判断循环终止，并把全过程写成 JSONL 轨迹。项目没有使用 LangChain、OpenAI Agents SDK 等 Agent 框架，也不依赖服务端代码执行或文件工具。

## 架构

```text
CLI ────────┐
            ├──> 共享 Runtime ──> 有界 Agent 循环 ──> OpenAI-compatible 模型
本地 Web UI ┘                         |
                                      +──> 5 个文件工具 + 1 个命令工具
                                      +──> 上下文压缩 / 验证状态 / JSONL 轨迹
```

核心模块：

- `agent.py`：模型—工具循环、重复调用检测、最大步数和验证状态。
- `tools/`：目录浏览、文本搜索、读取、写入、精确替换、命令执行。
- `context.py`：保留任务、运行时事实和完整 tool-call/result 组合的确定性压缩。
- `trace.py`：带会话 ID、序号、时间和脱敏的 JSONL 事件轨迹。
- `llm/`：核心协议与 OpenAI-compatible Chat Completions 适配器。
- `web.py`：单任务并发保护、命令审批、协作取消和 Web API；不包含 Agent 决策逻辑。

## 安装

需要 Python 3.11+。推荐在虚拟环境中安装：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

推荐在工作区根目录创建不会被 Git 提交的 `.env`：

```dotenv
TRACECODER_API_KEY=your-key
TRACECODER_BASE_URL=https://api.deepseek.com
TRACECODER_MODEL=deepseek-chat
```

CLI 和 Web 都会自动读取 `--workspace` 下的 `.env`；已有的系统环境变量优先于文件。也兼容 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`。不要把真实 key 写进仓库、README 或演示画面。

## 使用

```powershell
tracecoder run "阅读项目，修复失败测试并验证" --workspace .
tracecoder trace .tracecoder/traces/<session-id>.jsonl
```

命令默认逐次确认，确认界面展示实际执行的 `argv` 和工作目录；`--yes` 会自动批准命令，并授予命令当前用户已有的主机权限。需要 Shell 语法时，模型必须显式请求 `cmd /c` 或 `sh -lc`，不会发生隐式解释。

### 本地 Web GUI

```powershell
tracecoder web --workspace .
```

打开 `http://127.0.0.1:8765`。页面会展示模型回复、工具请求和结果，命令必须在页面中逐次批准；运行结束后显示终止原因、验证状态、改动文件与 trace 路径。同一工作区只允许一个活动任务，避免两个 Agent 同时写文件。停止按钮采用协作取消：会立即解除待审批命令，并在当前模型请求或工具调用返回后停止循环。

### GitHub Codespaces

仓库内的 `.devcontainer/devcontainer.json` 会安装项目并转发 8765 端口。创建 Codespace 时把 `TRACECODER_API_KEY`、`TRACECODER_BASE_URL`、`TRACECODER_MODEL` 设置为个人 Codespaces Secrets，然后执行：

```bash
tracecoder web --workspace . --host 0.0.0.0 --port 8765 --trust-proxy-auth
```

`--trust-proxy-auth` 表示你确认非回环地址前方存在已认证的反向代理；它本身不提供认证。这里只应在 GitHub 身份验证保护的 Codespaces 转发端口上使用。从 Codespaces 的 **Ports** 面板打开 `TraceCoder Web`，并确保端口保持 `Private`；不要把带有本地命令执行能力的 GUI 公开到互联网。代码编辑由 Codespaces 自带的浏览器版 VS Code 完成，TraceCoder GUI 专注于 Agent 运行与审批。

## 安全边界

- 文件工具只接收相对路径，规范路径必须位于工作区；`.env`、`.git/` 与 `.tracecoder/` 对模型文件工具保留（`.env.example` 仍可读取）。
- 命令使用 `shell=False`，有超时与运行期输出上限；子进程环境不继承 API key 等秘密变量。
- 命令不是操作系统沙箱：批准后仍可访问工作区外资源，超时只保证回收直接进程，不保证结束所有后代进程。
- 被读取的源码、提示词和命令输出会发送至配置的模型提供方；CLI 会在运行前显示 base URL 和模型，但不会显示 key。
- Web 默认只监听 `127.0.0.1`，未实现账号系统；非回环地址默认拒绝启动。只有确认前方是已认证代理时才能显式使用 `--trust-proxy-auth`，例如受 GitHub 身份验证保护的私有 Codespaces 端口。

## 开发验证

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy
python -m tracecoder --help
python -m tracecoder run --help
python -m tracecoder web --help
```

测试使用假模型覆盖完整的读写—验证—完成循环、Web 事件、命令审批和取消，不需要真实 API key。`.github/workflows/ci.yml` 会在 GitHub 的 Windows/Linux、Python 3.11/3.13 环境自动执行上述质量检查。
