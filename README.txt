Git 仓库：https://github.com/fanyuezuishuai/CodingAgent

运行：安装 Python 3.11+，创建虚拟环境并执行 `python -m pip install -e ".[dev]"`。在工作区 `.env` 中配置 TRACECODER_API_KEY、TRACECODER_BASE_URL、TRACECODER_MODEL；同名进程环境变量覆盖 `.env`。CLI：`tracecoder run "编程任务" --workspace .`。GUI：`tracecoder web --workspace .`，浏览器访问 `http://127.0.0.1:8765`。Codespaces 配置个人 Secrets 后运行 `tracecoder web --workspace . --host 0.0.0.0 --trust-proxy-auth`。

特色：未使用 Agent 框架。自行实现 tool calling 解析、多轮消息历史与确定性上下文压缩、DeepSeek 思考状态回传、六个本地工具、参数校验、有界循环、重复调用/最大步数终止、命令审批与超时、工作区路径隔离、验证状态及脱敏 JSONL 轨迹。Web GUI 支持多轮会话、安全 Markdown、文件上传、折叠工具过程、可读审批和停止；同一工作区禁止并发任务。GitHub Actions 在 Windows/Linux 自动测试并检查项目合规性。

安全说明：`.env` 已忽略且对模型文件工具保留，仓库不含 key。文件工具不能越出工作区或访问内部轨迹；命令默认逐次确认，但并非操作系统沙箱。源码和命令输出会发送到所配置的模型提供方。`--trust-proxy-auth` 本身不提供认证，Codespaces 端口须保持 Private。
