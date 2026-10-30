# googii · Code Agent

**用自然语言驱动代码阅读、文件修改和命令执行，让应用拥有一个可集成的代码智能体后端。**

googii 基于 **FastAPI + LangGraph + MCP + Docker** 构建。它把模型推理、工具调用、会话状态和上下文管理组织为一个 Python 服务，通过 HTTP API 与 SSE 连接应用、网关或自定义前端。

适用于代码智能体原型开发、项目维护助手及应用集成。当前采用单进程服务和 SQLite checkpoint 的轻量架构，可独立调用，也可接入自定义应用网关。

## 目录

- [核心能力](#核心能力)
- [架构与执行流程](#架构与执行流程)
- [快速开始](#快速开始)
- [发送第一个任务](#发送第一个任务)
- [API 参考](#api-参考)
- [SSE 事件与审批](#sse-事件与审批)
- [配置参考](#配置参考)
- [MCP 工具](#mcp-工具)
- [上下文与会话](#上下文与会话)
- [开发与测试](#开发与测试)
- [常见问题](#常见问题)
- [部署边界与演进方向](#部署边界与演进方向)

## 核心能力

| 能力 | 当前实现 |
| --- | --- |
| 自然语言任务 | 模型选择工具，结合执行结果继续推理 |
| 文件操作 | 列出目录、读取 UTF-8 文件、原子写入、应用 unified diff |
| 终端操作 | 在会话对应的 Docker 容器中执行 shell 命令 |
| 模型接入 | 支持 OpenAI 与 Anthropic，可配置模型名称 |
| 会话续接 | SQLite 保存 LangGraph checkpoint，同一用户和会话可继续上下文 |
| 过程反馈 | SSE 报告模型调用、工具执行、文件变更、审批和运行状态 |
| 运行控制 | 查询、取消运行，按序号回放保留事件 |
| 上下文管理 | 组合当前任务、工作区规则、历史摘录和近期消息 |
| 大结果处理 | 工具结果超过阈值后写入磁盘，向模型提供预览和引用 |
| 循环约束 | 迭代数、工具调用数、总时长预算与无进展检测 |
| 交互审批 | 流式接口对命中策略的危险命令等待人工决策 |

可以提出“说明项目结构”“定位接口错误”“修改代码并运行相关验证”等任务。完成效果取决于模型能力、工作区内容，以及容器中已准备的工具和依赖。

## 架构与执行流程

```text
客户端 / 应用网关 / 自定义前端
                  │ HTTP + SSE
                  ▼
            FastAPI API 层
                  │
                  ▼
      CodeAgentService · 运行与会话管理
           │                    │
           ▼                    ▼
      LangGraph 循环       SQLite checkpoint
           │
           ├── ContextAssembler：任务、规则、摘要、近期消息
           ├── 模型节点：推理并生成工具调用
           ├── 工具节点：审批、调用、结果处理
           └── 进展检测与结束检查
                        │
                        ▼
                    MCP Client
                        ├── Filesystem Server → 宿主机工作区
                        └── Terminal Server   → Docker /workspace
```

一次流式运行的主要步骤：

1. 校验请求及工作区路径，生成 `runId` 和事件日志。
2. 根据用户、会话获取 Agent 实例，初始化 stdio MCP 客户端。
3. 组装上下文，调用绑定工具的模型。
4. 执行工具；命中审批规则时发出事件并等待决策。
5. 检查进展和预算，继续循环或停止。
6. 完成基础结束检查，返回回复、变更文件和上下文统计。

SSE 提供阶段和结果反馈。当前模型节点等待整轮模型响应，不提供逐 token 文本输出。

## 快速开始

### 1. 环境要求

- Python **3.11+**。
- Git、Docker CLI 和正在运行的 Docker Engine。
- Windows 可使用 Docker Desktop，启用 Linux 容器。
- 支持工具调用的模型及对应 API 凭据。

Python 服务运行在宿主机；Agent 终端命令运行在 Docker。两边的依赖需要分别准备。

### 2. 获取项目与安装依赖

```powershell
git clone https://github.com/Lovinghost/googii.git
cd googii
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Linux / macOS 将激活和复制步骤替换为：

```bash
source .venv/bin/activate
cp .env.example .env
```

### 3. 配置模型与工作区

编辑 `.env`，例如使用仓库的 `workspace/` 作为允许操作的根目录：

```dotenv
MODEL_PROVIDER=openai
MODEL_NAME=gpt-4.1-mini
WORKSPACE_ROOT=./workspace
DATABASE_PATH=./storage/checkpoints.sqlite
SANDBOX_IMAGE=python:3.12-slim
```

`.env.example` 中的工作区是本地开发示例路径，请按实际环境修改。相对路径基于启动服务时的当前目录解析。

在启动服务的终端中设置模型凭据，PowerShell 示例：

```powershell
$env:OPENAI_API_KEY = "你的 OpenAI API Key"
```

Linux / macOS：

```bash
export OPENAI_API_KEY="你的 OpenAI API Key"
```

当前项目配置读取 `.env`，但 `Settings` 未声明 API Key 字段，模型客户端直接从进程环境读取凭据。因此请通过进程环境设置 Key，或让启动工具显式加载 dotenv；仅填写 `.env` 中的 Key 不保证模型客户端能够读取。

使用 Anthropic 时，将 `MODEL_PROVIDER` 改为 `anthropic`，将 `MODEL_NAME` 设为自己可用且支持工具调用的模型，并在进程环境设置 `ANTHROPIC_API_KEY`。

### 4. 准备执行镜像

```bash
docker version
docker pull python:3.12-slim
```

默认镜像提供基础 Python 与 shell 环境。需要 Git 时，可以构建仓库提供的镜像：

```bash
docker build -t googii-sandbox:local -f sandbox/Dockerfile .
```

然后在 `.env` 设置 `SANDBOX_IMAGE=googii-sandbox:local`。该 Dockerfile 安装了 Git；pytest、Node.js 和业务依赖需按任务需要加入镜像。容器运行时禁网，应提前准备依赖。

### 5. 启动服务

在项目根目录运行：

```bash
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

- [健康检查](http://127.0.0.1:8000/health)
- [Swagger UI](http://127.0.0.1:8000/docs)
- [ReDoc](http://127.0.0.1:8000/redoc)

`/health` 返回 `{"status":"ok"}` 表示服务已启动，不代表模型凭据和 Docker 可用。MCP 会话在首次任务时初始化；容器在首次执行终端命令时创建。

## 发送第一个任务

保持服务运行，在另一终端使用安装了开发依赖的虚拟环境执行：

```python
import json
import httpx

payload = {
    "prompt": "列出当前工作区的文件，说明目录结构。暂时不要修改文件。",
    "workspace": ".",
    "sessionId": "demo-1",
    "userName": "alice",
}

with httpx.stream(
    "POST",
    "http://127.0.0.1:8000/api/v1/runs/stream",
    json=payload,
    timeout=None,
) as response:
    response.raise_for_status()
    for line in response.iter_lines():
        if line.startswith("data: "):
            event = json.loads(line[6:])
            print(event["sequence"], event["type"], event["data"])
```

`workspace: "."` 表示允许根目录本身，`workspace: "demo"` 表示其下的 `demo` 子目录，不存在时会自动创建。续接任务时使用相同的 `userName`、`sessionId` 与 `workspace`；同一会话不能切换工作区。

## API 参考

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/v1/runs/stream` | 创建运行并订阅 SSE |
| `GET` | `/api/v1/runs/{runId}` | 查询状态与结果 |
| `GET` | `/api/v1/runs/{runId}/events` | 回放保留事件并接收后续事件 |
| `POST` | `/api/v1/runs/{runId}/cancel` | 请求取消运行 |
| `POST` | `/api/v1/runs/{runId}/decisions` | 批准或拒绝操作 |
| `GET` | `/api/v1/sessions/{sessionId}/context` | 查询摘要与上下文统计 |
| `POST` | `/tasks` | 兼容接口，执行结束后返回 JSON |
| `DELETE` | `/sessions/{sessionId}` | 关闭内存会话并释放 MCP 与容器资源 |

### 创建流式运行

`POST /api/v1/runs/stream` 请求体：

```json
{
  "prompt": "检查项目问题，修改后运行相关验证",
  "workspace": "demo",
  "sessionId": "demo-1",
  "userName": "alice",
  "environment": {}
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `prompt` | 是 | 任务描述，1–20,000 字符 |
| `workspace` | 是 | 根目录内的绝对路径，或相对于根目录的路径 |
| `sessionId` | 否 | 不提供时自动生成；继续任务时使用原值 |
| `userName` | 是 | 调用方提供的用户标识 |
| `environment` | 否 | 保留的兼容字段，当前未传入运行服务或容器 |

从事件中取得 `runId` 后，可查询：

```text
GET /api/v1/runs/{runId}?userName=alice
GET /api/v1/runs/{runId}/events?userName=alice&after=12
GET /api/v1/sessions/demo-1/context?userName=alice&workspace=demo
```

`after=12` 表示接收序号大于 12 的保留事件。事件受历史条数限制，仅在当前进程中保存。

运行查询包含 `runId`、`sessionId`、`userName`、`workspace`、`status`、`createdAt`、`finishedAt`、`result`、`error`。最终结果包含模型回复、变更文件、命令结果与上下文统计。

### 取消运行

向 `POST /api/v1/runs/{runId}/cancel` 提交：

```json
{"userName": "alice"}
```

返回 `cancelling` 表示已发出取消请求，通过运行查询或 `run.cancelled` 事件确认结果。取消不会回滚已经写入工作区的文件。

### 兼容接口

`POST /tasks` 使用 snake_case 字段：

```json
{
  "task": "说明当前项目的目录结构",
  "session_id": "local-demo",
  "workspace": ".",
  "user_name": "local"
}
```

此接口等待任务结束后返回 JSON，没有交互审批通道；当前审批函数在缺少回调时直接放行。需要人工审批时，应使用流式接口。

关闭会话使用 `DELETE /sessions/{sessionId}?userName=alice`，不会删除工作区文件或 SQLite 中已有的 checkpoint。

## SSE 事件与审批

SSE 外层事件名称固定为 `agent.event`，具体类型在 JSON 的 `type` 字段。下面为结构示例，ID 和时间为示意值：

```text
event: agent.event
data: {"type":"run.started","runId":"example-run","sessionId":"demo-1","sequence":1,"eventId":"example-event","timestamp":"2026-10-31T00:00:00+00:00","data":{"workspace":"demo"}}

```

| 事件 | 含义 |
| --- | --- |
| `run.started` | 运行开始 |
| `context.built` / `compact.completed` | 上下文组装 / 历史摘录完成 |
| `llm.started` / `llm.completed` | 模型调用开始 / 结束 |
| `tool.started` / `tool.completed` / `tool.failed` | 工具生命周期 |
| `file.changed` | 文件工具报告写入或补丁操作 |
| `approval.required` / `approval.decided` | 请求审批 / 收到决策 |
| `progress.checked` | 进展检查 |
| `verification.completed` | 基础结束检查 |
| `turn.failed` / `turn.waiting_user` | 预算耗尽或需要人工介入 |
| `run.completed` / `run.failed` / `run.cancelled` / `run.waiting_user` | 运行结束状态 |

收到 `approval.required` 后，从 `data.requestId` 取出审批 ID，向 `POST /api/v1/runs/{runId}/decisions` 提交：

```json
{
  "requestId": "从事件中取得的 requestId",
  "decision": "approved",
  "userName": "alice"
}
```

拒绝时使用 `"decision": "denied"`。等待审批的时间计入流式运行总时长。

审批策略通过正则匹配部分命令，例如 `rm -rf`、`git reset --hard`、`git clean -fd`、格式化与关机命令。它是有限的规则检查，不是完整的 shell 权限系统。

## 配置参考

以下为代码默认值，可能被 `.env` 和进程环境覆盖。模型名称应与自己的提供方权限及工具调用能力匹配。

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `MODEL_PROVIDER` | `openai` | `openai` 或 `anthropic` |
| `MODEL_NAME` | `gpt-4.1-mini` | 模型名称 |
| `WORKSPACE_ROOT` | `workspace` | 允许操作的宿主机根目录 |
| `DATABASE_PATH` | `storage/checkpoints.sqlite` | checkpoint 数据库 |
| `SANDBOX_IMAGE` | `python:3.12-slim` | 容器镜像 |
| `SANDBOX_CPUS` | `1.0` | 容器 CPU 限额 |
| `SANDBOX_MEMORY` | `512m` | 容器内存上限 |
| `SANDBOX_PIDS_LIMIT` | `256` | 容器进程数上限 |
| `COMMAND_TIMEOUT_MAX` | `300` | 单条命令允许的最大超时，秒 |
| `AGENT_MAX_ITERATIONS` | `20` | 模型迭代阈值 |
| `MAX_TOOL_CALLS` | `40` | 工具调用预算阈值，在路由阶段检查 |
| `MAX_NO_PROGRESS_STEPS` | `3` | 连续重复进展指纹的停止阈值 |
| `MAX_WALL_TIME_SECONDS` | `900` | 流式运行总时长上限，秒 |
| `MODEL_CONTEXT_LIMIT` | `32000` | 上下文估算预算 |
| `OUTPUT_TOKEN_RESERVE_RATIO` | `0.15` | 预留输出比例 |
| `CONTEXT_SAFETY_BUFFER_RATIO` | `0.10` | 上下文安全余量 |
| `RECENT_MESSAGE_LIMIT` | `10` | 近期消息候选条数 |
| `TOOL_INLINE_MAX_BYTES` | `8192` | 工具结果内联大小阈值 |
| `EVENT_HISTORY_LIMIT` | `1000` | 每次运行保留的事件数 |

文件服务还读取进程环境变量 `FILESYSTEM_MAX_READ_BYTES`（默认 2 MiB）和 `FILESYSTEM_MAX_LIST_ENTRIES`（默认 1000）。它们由 MCP 子进程读取，请在启动服务前设置到进程环境。

## MCP 工具

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `list_files` | `path="."`, `recursive=false` | 列出路径、类型和大小，达到上限时标记截断 |
| `read_file` | `path` | 读取大小限制内的 UTF-8 文本 |
| `write_file` | `path`, `content` | 原子写入，创建或覆盖文件，创建父目录 |
| `apply_patch` | `path`, `patch` | 对单个已有文件应用标准 unified diff |
| `execute_command` | `command`, `timeout=60` | 在容器执行命令，返回输出、退出码与超时标记 |

文件工具只接受工作区相对路径，并校验解析后的路径是否越界。终端通过 `docker exec` 在 `/workspace` 执行 `sh -lc`；Windows 宿主机也应使用 Linux shell 命令。

终端结果结构示例：

```json
{
  "stdout": "hello\n",
  "stderr": "",
  "exit_code": 0,
  "timed_out": false
}
```

工作区以可写 bind mount 挂载进容器。文件工具和终端操作同一份文件，修改会直接影响宿主机目录。

## 上下文与会话

### 上下文组装

每轮调用前，`ContextAssembler` 组合系统提示、当前任务、工作区根目录下的 `AGENTS.md`、已有摘要与近期消息。历史过长或预算不足时，生成确定性的历史摘录。

token 使用轻量字符估算，约每 4 个字符 1 token，不是模型专用 tokenizer。`context_manifest` 提供规则、摘要、近期消息和总输入的估算统计；应根据模型设置预算并保留余量。

### 大型工具结果

超过阈值的序列化结果写入：

```text
<工作区>/.code_agent/artifacts/<tool_call_id>.json
```

模型收到头尾预览、大小和 `artifact_ref`，可按需通过文件工具读取原始内容。这减少后续上下文体积，不是敏感信息脱敏机制，也不限制命令执行时的输出内存。

### 持久化范围

| 数据 | 位置 | 服务重启后的情况 |
| --- | --- | --- |
| LangGraph 状态 | SQLite checkpoint | 数据库保留时可用相同用户和会话续接 |
| 运行记录与 SSE 历史 | 进程内存 | 不保留 |
| MCP 会话与待审批状态 | 进程内存 | 不保留 |
| 修改文件与工具结果文件 | 工作区磁盘 | 文件保留 |

同一会话的 Agent 调用通过锁串行执行。不同会话可引用同一工作区，当前没有跨会话文件锁，应避免并发修改相同文件。上下文查询使用进程中的最近状态，重启后不会主动从 checkpoint 恢复该查询缓存。

## 开发与测试

```text
googii/
├── api/main.py                 # HTTP 路由与 SSE
├── agent/
│   ├── config.py               # 环境与运行参数
│   ├── service.py              # 会话、运行、取消、审批与 checkpoint
│   ├── graph.py                # LangGraph 节点与路由
│   ├── state.py                # Agent 状态定义
│   ├── events.py               # 事件协议与内存日志
│   ├── context/                # 上下文预算与消息组装
│   ├── loop/                   # 进展检测与审批策略
│   └── nodes/                  # 模型与工具节点
├── mcp_client/client.py        # stdio 客户端与工具发现
├── servers/filesystem/         # 工作区文件工具
├── servers/terminal/           # 容器终端工具
├── sandbox/                    # Docker 管理与镜像定义
├── tests/                      # 单元与接口测试
├── storage/                    # SQLite 数据
├── workspace/                  # 默认工作区
├── .env.example                # 配置模板
└── pyproject.toml              # 依赖与开发工具配置
```

安装开发依赖后运行：

```bash
python -m pytest
python -m ruff check .
```

现有测试涉及 API 启动及路径校验、上下文组装、事件回放、文件操作、审批策略、工具结果处理和部分 Docker 管理行为，不替代真实模型与 Docker 的端到端验证。

新增工具可在对应 MCP Server 中注册函数，客户端初始化时发现并提供给模型。涉及新副作用时，应同步检查审批策略、事件字段和工作区边界。

## 常见问题

| 现象 | 排查方式 |
| --- | --- |
| `Workspace must be inside configured root` | 检查请求路径是否位于 `WORKSPACE_ROOT` 内 |
| `A session cannot switch workspace` | 使用原工作区，或为新目录创建新会话 |
| 缺少 API Key | 检查服务进程环境中的对应提供方凭据 |
| `Docker Engine is unavailable` | 启动 Docker，确认当前终端能执行 `docker version` |
| 容器缺少 pytest、Git 或 Node.js | 在自定义镜像预装；宿主机依赖不会自动进入容器 |
| 容器无法联网安装依赖 | 容器固定禁网，需提前准备依赖镜像 |
| 状态为 `waiting_user` | 检查审批事件或结束原因，可能待审批、无进展或缺少验证 |
| 代理缓存事件 | 关闭 SSE 缓冲并配置适当的读取超时 |
| 重启后无法查询旧运行 | 运行只存于内存，SQLite 仅保存 LangGraph checkpoint |

## 部署边界与演进方向

当前版本适合本地开发和受控环境中的单进程集成。

- **身份与权限**：`userName` 由调用方提供，部分查询允许省略；当前没有服务端认证与可靠的多租户授权。对外接入前需由网关实施认证和工作区权限控制。
- **执行约束**：容器禁网，限制 CPU、内存与 PID，移除 capabilities，启用 `no-new-privileges`；工作区仍为可写挂载，文件工具直接访问宿主机工作区。
- **审批覆盖**：交互审批仅用于流式接口，命令规则覆盖有限。兼容 `/tasks` 没有交互审批保护。
- **验证含义**：存在文件变更时，基础结束检查要求记录中至少有一条退出码为 0 的命令；它不能保证命令验证了相关变更，也不能证明代码正确。
- **进程模型**：运行、事件和审批状态未持久化或跨进程共享；直接增加 Uvicorn worker 会影响运行查询与审批路由。
- **资源回收**：关闭会话和正常退出会尝试清理容器；异常退出仍需外部巡检及回收策略。

后续可按需求补充持久化事件、后台队列、认证与配额、容器回收、跨进程协调、更完整的审批及任务验证流程。这些尚未在当前版本实现。

## 许可证

当前仓库未附带 `LICENSE` 文件，尚未声明开源许可。公开可访问不等于授予开放使用许可；复用或分发前请向维护者确认授权。
