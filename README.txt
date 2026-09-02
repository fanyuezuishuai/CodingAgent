TraceCoder

Git 仓库：https://github.com/fanyuezuishuai/CodingAgent

如何运行：需要 Python 3.11+。克隆仓库后执行：
`python -m venv .venv`
Windows 激活：`.\.venv\Scripts\Activate.ps1`
Linux/macOS 激活：`source .venv/bin/activate`
安装：`python -m pip install -e ".[dev]"`
在目标工作区的 `.env` 中配置 `TRACECODER_API_KEY`、`TRACECODER_BASE_URL`、`TRACECODER_MODEL`。启动 Web：`tracecoder web --workspace .`，浏览器访问 `http://127.0.0.1:8765`；运行 CLI：`tracecoder run "编程任务" --workspace .`。

特色功能：项目未依赖 LangChain、LangGraph 等 Agent 框架，核心编排自行实现为有界的单智能体 Plan–Execute–Replan–Verify 状态机。修改文件或执行命令前必须建立显式计划，执行结果关联计划步骤，失败最多重规划一次，并保留最大步数、重复调用和验证终止门槛。Agent 可浏览、搜索、读取和直接修改本地文件；命令执行需审批。Web GUI 支持多轮对话、过程展示、代码 Diff、文件上传、项目归组，以及“先给方案、批准后落地”的项目流程。系统还提供路径隔离、参数校验、脱敏 JSONL 轨迹、基于真实 Diff 的 Proof 证据和文件改动接受/回滚。

说明：命令审批不等于系统沙箱；回滚仅覆盖文件工具产生的修改，不能保证撤销命令副作用。源码和命令输出会发送给所配置的模型提供方。
