# Code Agent

供 GopherAI v1 全栈项目调用的代码智能体服务。FastAPI 提供运行与 SSE 事件接口，LangGraph 驱动 Agent Loop，文件和终端操作统一通过 MCP；终端命令在隔离的 Docker 容器中执行。

## 已实现能力

- 文件工具：`list_files`、`read_file`、`write_file`、`apply_patch`
- 终端工具：`execute_command`，返回退出码、标准输出、错误输出和超时状态
- 每个用户会话独占 MCP 会话与 Docker 容器，容器默认禁网并限制 CPU、内存和 PID
- SQLite checkpoint 保存 LangGraph 会话状态；同一 `userName + sessionId` 可继续上下文
- `ContextAssembler` 按 token 预算重建上下文，常驻任务和 `AGENTS.md`，自动压缩旧消息
- 大于 8 KB 的工具输出写入 `.code_agent/artifacts/`，模型上下文只保留头尾预览和引用
- Agent Loop 具有迭代数、工具数、总时长预算和重复无进展检测
- 危险命令先发送 `approval.required`，收到批准后才执行
- 统一 SSE 事件、运行查询、断线后按序号续传、取消和会话上下文查询
- OpenAI / Anthropic 模型切换

当前采用设计方案的轻量 Phase 1：RabbitMQ 的网关生命周期事件由 GopherAI v1 发布；MySQL 长期记忆、Redis 热状态和独立 RabbitMQ Worker 可在需要多实例与任务恢复时继续演进，当前不额外维护一套不完整的持久化真相。

## 目录

```text
api/                    FastAPI 和 SSE 接口
agent/context/          token 预算与上下文组装
agent/loop/             进度检测与危险操作策略
agent/nodes/            LLM、MCP 工具节点
mcp_client/             MCP stdio 客户端
servers/filesystem/     Filesystem MCP Server
servers/terminal/       Terminal MCP Server
sandbox/                Docker 容器管理
storage/                SQLite checkpoint
tests/                  单元测试
```

## 启动

要求 Python 3.11+ 和可用的 Docker Engine：

```powershell
cd D:\projects\code_agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

在 `.env` 中填写模型 API Key。`WORKSPACE_ROOT` 是允许 Agent 操作的宿主机根目录；Go 网关传入的 `workspace` 必须位于其内部。当前本地联调可设为 `D:\projects`。

GopherAI v1 的 `config/config.toml` 已默认连接 `http://127.0.0.1:8000`。启动 FastAPI 后，再启动 Go 后端和 Vue 前端即可通过 Go 网关访问 Agent。

## API

- `GET /health`：健康检查
- `POST /api/v1/runs/stream`：创建任务并持续返回标准 SSE
- `GET /api/v1/runs/{runId}`：查询运行状态
- `GET /api/v1/runs/{runId}/events?after=0`：重连并回放事件
- `POST /api/v1/runs/{runId}/cancel`：取消运行
- `POST /api/v1/runs/{runId}/decisions`：批准或拒绝危险操作
- `GET /api/v1/sessions/{sessionId}/context`：查看摘要和上下文预算
- `POST /tasks`、`DELETE /sessions/{sessionId}`：兼容原 MVP 接口

请求示例：

```json
{
  "prompt": "检查项目并修复失败的测试",
  "workspace": "D:\\projects\\demo",
  "sessionId": "demo-1",
  "userName": "alice",
  "environment": {}
}
```

## 安全边界

服务会校验 Workspace 根目录，Filesystem MCP 拒绝绝对路径和 `..` 越界，Terminal MCP 通过 `docker exec` 在 `/workspace` 执行命令。危险命令需要交互审批，日志和上下文仅保留大型工具结果的有限预览。生产部署仍应补充服务间鉴权、配额、持久化事件与容器垃圾回收。
