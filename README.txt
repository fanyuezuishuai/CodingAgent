Git 仓库：https://github.com/fanyuezuishuai/CodingAgent

运行：安装 Python 3.11+，创建虚拟环境并执行 `python -m pip install -e ".[dev]"`。在工作区 `.env` 中配置 TRACECODER_API_KEY、TRACECODER_BASE_URL、TRACECODER_MODEL；同名进程环境变量覆盖 `.env`。CLI：`tracecoder run "编程任务" --workspace .`。GUI：`tracecoder web --workspace .`，浏览器访问 `http://127.0.0.1:8765`。Codespaces 配置个人 Secrets 后运行 `tracecoder web --workspace . --host 0.0.0.0 --trust-proxy-auth`。

特色：未使用 Agent 框架。自行实现 tool calling 解析、多轮历史与上下文压缩、DeepSeek 思考状态回传、七个本地工具、参数校验、有界循环、命令审批、路径隔离及脱敏 JSONL 轨迹。Proof Mode 根据真实 Diff、命令退出码、验证与终止状态导出 JSON/Markdown；文件工具修改前保存快照，可接受或回滚。Web GUI 支持多轮会话、Markdown、上传、折叠过程、可读审批，以及“修复课程项目”“生成小型带测试项目”预设。GitHub Actions 在 Windows/Linux 自动测试。

安全说明：`.env` 已忽略且对模型文件工具保留，仓库不含 key。文件工具不能越出工作区；命令默认逐次确认但并非系统沙箱；回滚只覆盖文件工具，不能保证撤销命令副作用。源码和命令输出会发送到所配置的模型提供方。`--trust-proxy-auth` 不提供认证，Codespaces 端口须保持 Private。
