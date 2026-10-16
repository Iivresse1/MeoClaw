# MeoClaw

一个Code Agent,可以通过CLI框架执行你想要的指令；

## 快速开始

```powershell
# 1) 安装（可编辑模式）
python -m pip install -e ".[dev]"

# 2) 配置密钥
Copy-Item .env.example .env      # 然后填入 OPENAI_API_KEY

# 3) 运行
meoclaw "在 utils.py 里加一个 add(a, b) 函数，并写个测试" --workspace ./workspace

# 等价写法：模块方式调用（无需 console script）
python -m meoclaw "统计 src 下有多少个 Python 文件" -w ./workspace
meoclaw --list-tools          # 只列工具，不调用模型（不需要 API Key）
```

`--workspace` 默认是 `./workspace`，不存在时自动创建。

## 技术栈

MeoClaw 使用 Python ≥ 3.10，核心依赖 typer + langchain（core / openai）+ langgraph + rich，检索可选 tavily-python。

| 技术 | 版本要求 | 项目中用途 | 选用理由 |
| --- | --- | --- | --- |
| Python | ≥ 3.10 | 运行环境 | 现代类型语法（`X \| None`）、跨平台、生态齐全 |
| typer | ≥ 0.12 | CLI 框架（`meoclaw` 命令入口） | 类型注解即参数定义，样板代码最少 |
| rich | ≥ 13.0 | 终端实时渲染（面板 / 颜色 / 表格） | 按节点分组输出体验好，GBK 等编码降级可控、不抛 UnicodeEncodeError |
| langchain-core | ≥ 0.3 | 工具协议与消息类型（`StructuredTool`） | 行业标准工具接口，与具体模型解耦 |
| langchain-openai | ≥ 0.2 | 模型接入（OpenAI 兼容） | 兼容各家网关，换供应商零改动 |
| langgraph | ≥ 0.2 | 状态图编排（节点 / 条件路由 / 流式） | 共享状态 + `add_messages` reducer + `updates`/`custom` 双流输出 |
| tavily-python | ≥ 0.7 | 网络检索（WebSearchTool / searchAgent） | 面向 LLM 的结构化结果，缺 Key 优雅降级不抛异常 |
| python-dotenv | ≥ 1.0 | `.env` 密钥加载 | 密钥与代码分离，真实环境变量始终优先 |
| pytest | ≥ 8.0（dev） | 测试框架 | 脚本化假模型，无需 API Key 即可全链路回归 |

## CLI 选项

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `TASK`（位置参数） | 无 | 任务描述；配合 `--list-tools` 时可省略 |
| `--workspace`, `-w` | `./workspace` | 工作区目录，不存在时自动创建；传空字符串会直接报错退出（不会退化成当前目录） |
| `--model`, `-m` | `OPENAI_MODEL` | 覆盖模型名 |
| `--temperature`, `-t` | `0.0` | 采样温度 |
| `--max-attempts` | `3` | 最多尝试轮数（1-20）：验证不通过就回到 planner 重来，用尽后收尾 |
| `--list-tools` | 关 | 只打印工作区状态、工具及参数后退出，不调用模型 |
| `--show-state` | 关 | 执行前额外打印工作区与各类预算 |

## 配置

`.env`（真实环境变量优先，不会被 `.env` 覆盖）：

| 变量 | 必填 | 默认 | 兼容旧名 |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | 是 | — | `API_KEY` |
| `OPENAI_MODEL` | 否 | `gpt-4o-mini` | `MODEL` |
| `OPENAI_BASE_URL` | 否 | 官方地址 | `BASE_URL` / `OPENAI_API_BASE` |
| `TAVILY_API_KEY` | 否（启用检索需要） | — | —；不配时 `WebSearchTool` 返回缺 Key 说明，不影响其他功能 |

`.env` 的查找顺序：当前目录 → 逐级向上直到第一个含 `pyproject.toml` / `setup.py` / `.git`
的目录（即项目根，不会继续上溯到用户主目录）→ 源码树根目录。真实环境变量始终优先。

## 测试

```powershell
python -m pytest
```

覆盖：路径越界拦截（含符号链接、`..` glob）、读写/替换语义（CRLF 保持、非 UTF-8 拒绝）、
Grep 的 glob 与 head_limit、Bash 的退出码/超时、providers 的环境变量读取、
底层 ReAct 循环（`tests/test_agent.py`：多工具调用、max_loops 收尾、工具失败降级为 `ok=False`）、
`MeoGraphState` 的 `add_messages` 合并语义、
四个图节点（计划生成/修订、TodoUpdateTool 回写、verifier fail-closed、路由边界、final 两种收尾）、
supervisor 节点（工具绑定、计划应用与 `TODO.md` 持久化、`CallCodeAgentTool`/`CallSearchAgentTool`
真实委派、来源按 URL 去重、未知工具降级、brief 组装）、
`build_workflow()` 的真实图回路与**统一事件流**、
专家 Agent（codeAgent / searchAgent）、WebSearchTool（缺 Key、Tavily 结果解析、schema、只读集合）、
三层 Memory、上下文监控与压缩（含**真图里"验证失败 → 压缩 → 重新规划 → 通过"的完整回路**）、
复杂拓扑（`build_complex_workflow` 试满后收敛不死循环、supervisor 委派端到端），
CLI 的 `--list-tools`/参数校验/缺密钥退出码，以及经 `StructuredTool.invoke()` 的端到端调用。

---

> 完整架构、模块职责、关键类与函数、事件系统、安全模型与扩展指南见 [项目架构指南.md](项目架构指南.md)。
