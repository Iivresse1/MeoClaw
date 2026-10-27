# MeoClaw

MeoClaw 是一个本地桌面的 AI 助理：支持纯对话、工作区文件任务和系统工具（截图/剪贴板/打开链接）三类模式。所有可执行工具的 Agent 模式都支持 Tavily 网络搜索与网页正文读取；1.1 分支使用 LangChain `create_agent` 与 middleware 管理 ReAct 循环、工具错误、调用预算、上下文摘要和人工审批，并通过 SQLite checkpoint 持久化会话。

## 项目整体架构

| 层 | 位置 | 技术 | 职责 |
| --- | --- | --- | --- |
| 桌面壳 | `desktop/electron/` | Electron 33 | 拉起/复用桥接进程、创建窗口、无系统菜单 |
| 渲染层 | `desktop/src/` | React 18 + TypeScript + Vite | 会话侧边栏、消息流、执行过程卡、审批卡、输入卡片、设置弹窗 |
| 桥接层 | `bridge/meoclaw_bridge/` | FastAPI + uvicorn + websockets | 仅监听 `127.0.0.1:8931`，REST + WS 封装后端事件流，会话持久化与密钥管理 |
| 后端核心 | `src/meoclaw/` | LangChain 1.x + LangGraph | `create_agent` 循环、middleware、ContextBuilder、SQLite checkpoint、工作区路径安检 |

一次任务的数据流：

```
输入消息 → POST /api/task（懒建会话、用户消息落库、后台线程启动）
        → WS /ws/task/{id} 推事件（node_start / ai_token / plan / tool_call / tool_result /
          verification / final_answer / approval_request / ui_done）
        → final_answer 自动落库为助手消息 → 侧边栏刷新会话标题
```

## 目录结构与模块职责

```
.
├─ bridge/meoclaw_bridge/     # Python 桥接服务
│  ├─ server.py               # FastAPI 入口：REST 端点 + WS 事件泵
│  ├─ config.py               # settings.json 读写、keyring 密钥、供应商解析
│  ├─ store.py                # SQLite 会话/消息持久化（懒创建）
│  └─ adapters/
│     ├─ runner.py            # 三模式后台执行 + HITL 审批注册表
│     ├─ model_factory.py     # 由激活供应商构造 ChatOpenAI、测试连接
│     └─ system_tools.py      # 系统工具集（5 个，高危需批准）
├─ desktop/                   # Electron + React 前端
│  ├─ electron/main.cjs       # 主进程：桥接子进程管理、窗口、去菜单
│  ├─ electron/preload.cjs    # 通过 IPC 暴露 bridgePort 与能力令牌
│  └─ src/                    # App.tsx + api.ts + components/ + styles.css
├─ src/meoclaw/               # 后端核心（agents / cli / core / graph / prompts / providers / tools）
├─ tests/                     # 后端全链路测试（假模型，无需 API Key）
├─ pyproject.toml             # 后端打包与依赖声明
├─ ARCHITECTURE_V1.1.md       # 当前运行架构与安全边界
└─ requirements.txt           # 桥接层 Python 依赖
```

## 关键类与函数

### 桥接层（bridge）

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | 健康检查（Electron 启动前探测/复用） |
| `GET /api/sessions`、`GET /api/sessions/{sid}/messages`、`DELETE /api/sessions/{sid}` | 会话与消息 |
| `GET/POST /api/settings` | 读写 `temperature / max_attempts / permission / sidebar_open` |
| `POST /api/providers`、`DELETE /api/providers/{name}`、`POST /api/providers/active` | CC Switch 式供应商管理 |
| `POST /api/test-connection` | 用表单参数（或已存密钥）试调模型 |
| `GET/POST/DELETE /api/tavily` | 查询、验证并保存 Tavily API Key（只保存在系统凭据中） |
| `POST /api/task` | 启动任务，返回 `{task_id, session_id}` |
| `POST /api/approval/{aid}` | 审批放行/拒绝 |
| `WS /ws/task/{tid}` | 推送 runner 事件流，`ui_done` 收尾 |

- `runner.start(...)`：启动后台任务并把 LangChain/LangGraph 原生事件写入队列；`workspace` 驱动角色图，`system` 驱动带 HITL middleware 的 Agent
- `runner.resume_approval(...)`：审批后使用 `Command(resume=...)` 恢复 SQLite checkpoint 中的任务
- `system_tools`：`WebSearchTool / WebFetchTool` + `TakeScreenshot / ClipboardRead / ClipboardWrite / OpenUrl / OpenWorkspaceFile`，路径经 `resolve_in_workspace` 安检，越界直接拒绝
- `store`：会话仅在首条消息时落库，标题取首条消息前 60 字

### 前端（desktop/src）

- `App.tsx`：全局状态中枢；`handleEvent()` 将 `ai_token` 增量映射为流式回答，并把计划、工具、验证和审批显示为实时进度；Ctrl+B 切换侧边栏
- `api.ts`：REST 封装 + `taskSocket()`；端口取 `window.meoclaw.bridgePort`（纯浏览器调试回落 8931）
- 组件：`Sidebar`（品牌+新会话+历史）、`ChatView`（欢迎屏/消息流）、`Composer`（输入卡片+权限下拉，无模型选择器）、`ProgressCard`（可折叠执行过程）、`ApprovalCard`（允许/拒绝）、`SettingsModal`（供应商与运行参数）
- `styles.css`：黑白浅色主题（主色 `#171717`，无彩色）

## 技术栈

| 技术 | 版本要求 | 项目中用途 | 选用理由 |
| --- | --- | --- | --- |
| Electron | ≥ 33 | 桌面壳、桥接进程管理 | Web UI 快速落地，`contextIsolation` 安全模型 |
| React + TypeScript | 18 / 5 | 渲染层 | 事件流驱动的组件化 UI |
| Vite | ≥ 5 | 开发服务器与构建 | 秒级热更新，`base:"./"` 支持 file:// 加载 |
| FastAPI + uvicorn | ≥ 0.115 / 0.30 | 桥接 REST/WS | 与后端同语言零转换，自动文档 |
| websockets | ≥ 12 | 事件流传输 | 原生 LangChain 事件经 Bridge 映射为稳定 UI 协议 |
| LangChain | ≥ 1.4 | `create_agent`、middleware、工具协议 | 统一循环预算、错误、摘要和 HITL |
| LangGraph + SQLite checkpoint | ≥ 1.2 / ≥ 3.1 | 状态图与持久化恢复 | 会话消息跨任务连续，审批可恢复 |
| keyring | ≥ 25 | API Key 存 Windows 凭据管理器 | 失败自动降级本地 `.secrets.json` |
| pyperclip / Pillow | — | 剪贴板与截图系统工具 | 桌面场景刚需 |
| meoclaw（`src/`） | — | 后端工作流编排 | 外层角色图 + 内层 `create_agent` |

## 依赖关系

```
desktop(渲染层) ──HTTP/WS──▶ bridge(8931) ──pip -e──▶ meoclaw 核心(src/)
                                │                        │
                                ▼                        ▼
                    %APPDATA%\MeoClawDesktop        OpenAI 兼容 API
                    settings.json / store.db /      (.env 或设置页供应商)
                    checkpoints.sqlite / sessions\<sid>\
```

- 前端不接触任何后端逻辑与密钥，仅访问本机 8931
- 每个会话有独立工作区目录，文件操作被限制在其中

## 快速开始

### 环境要求

- Windows 10/11
- Python 3.10+
- Node.js 20+
- Docker Desktop（WSL2 后端）
- 一个 OpenAI 兼容模型供应商

-如果你没有在本地下载Docker，可以尝试清华 Docker CE 镜像，根地址为：

https://mirrors.tuna.tsinghua.edu.cn/docker-ce/

-准备Docker完成后，可以自己创建一个根目录(比如C:\Users:\Desktop\meoagent)。

-然后右键开始打开终端(管理员)或Win+R输入powershell，按住Ctrl+Shift+Enter以管理员方式打开powershell。先输入cd (你的根目录地址)，再依次输入下列命令:

-(以下命令需在同一个 PowerShell 会话中依次执行。每一步成功后再进入下一步；如果某一步失败，不要继续执行后面的启动命令。Python 环境目录固定为仓库根目录下的 `pyruntime`。)

```powershell
# 1. 获取代码
git clone https://github.com/Iivresse1/MeoClaw.git meoclaw
cd meoclaw

# 2. 创建 Python 环境并安装后端、桥接层依赖
python -m venv pyruntime
pyruntime\Scripts\python.exe -m pip install --upgrade pip
pyruntime\Scripts\python.exe -m pip install -r requirements.txt `
  -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 启动 Docker Desktop，然后构建隔离 shell 镜像
.\scripts\build-sandbox.ps1 `
  -PythonImage docker.m.daocloud.io/library/python:3.12-slim `
  -DebianMirror mirrors.tuna.tsinghua.edu.cn `
  -PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple

# 4. 安装桌面端依赖
cd desktop
$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
npm install
```

检查 Python 依赖和沙箱镜像都已安装成功：

```powershell
cd ..
pyruntime\Scripts\python.exe -c "import fastapi, langchain, langgraph; print('python deps ok')"
docker image inspect meoclaw-sandbox:latest --format "{{.Id}}"
```

开发模式启动 Vite、Electron 和本地 Bridge：

```powershell
cd desktop
npm run dev
```

生产模式构建并启动：

```powershell
cd desktop
npm run build
npm start
```

启动后打开应用「设置」，填写供应商名称、模型名、Base URL 和 API Key。新用户可在「设置 → 联网检索」中打开 Tavily 控制台创建 Key，粘贴后点击「一键配置」；应用会先验证连接，再存入系统凭据。Agent 的文本、计划、工具调用、验证命令和审批请求会通过 WebSocket 实时更新。

如果可正常访问 Docker Hub、Debian 和 PyPI 官方源，也可以改用默认构建：

```powershell
Set-Location (git rev-parse --show-toplevel)
.\scripts\build-sandbox.ps1
```

桥接健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8931/api/health
```

### Windows 首次安装排查

**pip 报 `UnicodeDecodeError` 或 `'gbk' codec can't decode`**

旧版本 `requirements.txt` 包含中文注释，部分中文 Windows 环境会让 pip 使用 CP936/GBK 解码。当前仓库中的依赖文件已改为纯 ASCII。请先更新代码后重新安装；如果使用旧克隆，也可以绕过该文件：

```powershell
Set-Location (git rev-parse --show-toplevel)
pyruntime\Scripts\python.exe -m pip install `
  -i https://pypi.tuna.tsinghua.edu.cn/simple `
  -e ".[dev]" `
  "fastapi>=0.115" `
  "uvicorn>=0.30" `
  "websockets>=12.0" `
  "keyring>=25.0" `
  "pyperclip>=1.8" `
  "pillow>=10.0"
```

**Docker 报 `failed to resolve source metadata for python:3.12-slim`**

先确认 Docker Desktop 已启动：

```powershell
docker version
```

该错误通常是无法连接 `registry-1.docker.io`，不是项目代码错误。使用上面的国内镜像构建命令；如果 `docker.m.daocloud.io` 不可用，可以尝试：

```powershell
Set-Location (git rev-parse --show-toplevel)
.\scripts\build-sandbox.ps1 `
  -PythonImage docker.1ms.run/library/python:3.12-slim `
  -DebianMirror mirrors.tuna.tsinghua.edu.cn `
  -PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple
```

**npm 报 `unable to verify the first certificate`**

失败发生在 Electron 二进制下载阶段，通常是网络代理或本地证书链拦截了 GitHub 下载地址。设置 Electron 镜像后重新执行 `npm install`：

```powershell
Set-Location (Join-Path (git rev-parse --show-toplevel) "desktop")
$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
npm install
```

不要通过关闭 `strict-ssl` 或设置 `NODE_TLS_REJECT_UNAUTHORIZED=0` 绕过证书校验。

**`npm install` 同时出现 `EBUSY` 或 `EPERM`**

这些通常是 Electron 下载失败后的清理错误。先关闭所有 Electron、Vite、Node 和终端进程，再设置镜像并重新执行 `npm install`。如果目录仍被占用，重启终端后再试。

## 配置

**模型供应商（设置页，CC Switch 式）**：名称 / 模型名 / Base URL / API Key 多条记录，
`active_provider` 一键切换当前供应商；密钥存 keyring（服务名 `MeoClawDesktop`，
键名 `provider:{名称}`），不可用时降级 `%APPDATA%\MeoClawDesktop\.secrets.json`。

**Tavily 联网检索**：在设置页粘贴 Tavily API Key 并点击「一键配置」。Key 以
`tavily:api_key` 存入系统凭据，不会写入 `settings.json`。环境变量
`TAVILY_API_KEY` 优先级最高，适合 CLI 或自动化环境。配置完成后，
`WebSearchTool` 与 `WebFetchTool` 在 chat、workspace、system 和专家 Agent 中可用。

**`%APPDATA%\MeoClawDesktop\settings.json`**：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `temperature` | `0.0` | 采样温度 |
| `max_attempts` | `3` | workspace 模式最大验证轮数 |
| `permission` | `workspace` | 输入框权限档位记忆 |
| `providers` / `active_provider` | `[]` / `""` | 供应商列表与当前项 |
| `sidebar_open` | `true` | 侧边栏开合状态 |

运行环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MEOCLAW_SANDBOX` | `docker` | workspace shell 隔离模式；生产不建议使用 `host` |
| `MEOCLAW_SANDBOX_IMAGE` | `meoclaw-sandbox:latest` | 沙箱镜像 |
| `MEOCLAW_SANDBOX_NETWORK` | `0` | 设为 `1` 才允许容器联网 |
| `MEOCLAW_ALLOWED_ORIGINS` | 本地 Vite/Electron | REST CORS 白名单 |

## 权限模式

| 下拉文案 | 内部值 | 行为 |
| --- | --- | --- |
| 仅对话 | `chat` | `create_agent` + WebSearch/WebFetch；携带会话历史，不访问工作区 |
| 在工作区内修改 | `workspace`（默认） | planner→implementation→verifier→final；含 WebSearch/WebFetch，shell 在 Docker 中执行，Docker 不可用时 fail-closed |
| 系统工具（需批准） | `system` | `create_agent` + WebSearch/WebFetch + 5 个系统工具；敏感调用经 `HumanInTheLoopMiddleware` 中断，用户决定后恢复 |

## 测试

- 后端核心全链路回归（假模型、无需 API Key）：仓库根目录运行 `python -m pytest`
- 桥接层安全测试：鉴权、Origin、session 路径和删除边界
- 桌面端构建：`cd desktop; npm install; npm run build`

---

新的运行架构、ContextBuilder、middleware、SQLite 恢复和 Docker 安全边界见
[ARCHITECTURE_V1.1.md](ARCHITECTURE_V1.1.md)。

> 仓库分支说明：**`main`** 为完整项目（后端核心 `src/` + 桥接层 `bridge/` + 桌面端 `desktop/`）；
> **`branch`** 为后端核心 `meoclaw` 的备份分支。

本项目前端参考 dsh 与 openclaw 重新设计，并由 AI 辅助实现。
