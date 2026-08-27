Git 仓库：https://github.com/fanyuezuishuai/CodingAgent

运行：安装 Python 3.11+，创建虚拟环境后执行 `python -m pip install -e ".[dev]"`。设置环境变量 TRACECODER_API_KEY、TRACECODER_BASE_URL、TRACECODER_MODEL，再执行 `tracecoder run "编程任务" --workspace 项目目录`。测试命令为 `python -m pytest -q`。

特色：项目未使用任何 Agent 框架。自行实现 OpenAI-compatible tool calling 解析、对话历史与确定性上下文压缩、六个本地工具、参数校验、有界 Agent 循环、重复调用/最大步数终止、命令审批与超时、工作区路径隔离、验证状态以及脱敏 JSONL 轨迹。自动化测试使用假模型，不需要真实 API key。

安全说明：凭据仅从环境变量读取，仓库不含 key。文件工具不能越出工作区，也不能访问内部轨迹目录；命令默认逐次确认，但并非操作系统沙箱。源码和命令输出会发送到所配置的模型提供方。
