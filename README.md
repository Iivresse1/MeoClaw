# MeoClaw

工作区受限的编码智能体：`typer` 提供 CLI，`langchain-core` 提供工具协议，`langchain-openai` 提供模型，
`rich` 负责实时输出，`langgraph` 承载共享状态。
所有文件类工具都被钉在 `RuntimeState.workspace` 里 —— 路径越界直接拒绝。

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

## 目录结构

```
src/meoclaw/
├── __init__.py            # 只导出轻量对象，import meoclaw 不拉 langchain
├── __main__.py            # python -m meoclaw
├── core/
│   ├── state.py           # RuntimeState 数据类（workspace + 各种预算）
│   ├── paths.py           # 工作区路径工具（越界检查的唯一执行点）
│   ├── context.py         # 图/Agent 共用：runtime、模型、事件出口解析
│   ├── react.py           # 底层 ReAct 循环（stream_react_events，供节点与专家 Agent 用）
│   └── agent.py           # CLI 侧统一事件流：驱动整张图并翻译图事件
├── agents/
│   ├── code_agent.py      # codeAgent：实现类专家 Agent（run_code_agent）
│   └── search_agent.py    # searchAgent：检索类专家 Agent（run_search_agent）
├── graph/
│   ├── state.py           # MeoGraphState / TodoItem / VerificationResult / LayeredMemory …
│   ├── memory.py          # 三层 Memory：Rules / Working / History Summary
│   ├── nodes.py           # planner / supervisor / actor / verifier / final 节点 + 路由
│   ├── workflow.py        # build_workflow() / build_complex_workflow() + initial_state()
│   └── __init__.py
├── prompts/
│   ├── stage2.py          # PLANNER_PROMPT / ACTOR_PROMPT / FINAL_PROMPT
│   ├── stage3.py          # VERIFIER_PROMPT + SUPERVISOR_PROMPT + SEARCH/CODE_AGENT_PROMPT
│   └── stage4.py          # CONTEXT_COMPRESSION_PROMPT
├── tools/
│   ├── registry.py        # build_tools(state) / build_read_only_tools(state)
│   ├── file_tools.py      # FileReadTool / FileWriteTool / FileEditTool
│   ├── grep_tool.py       # GrepTool
│   ├── bash_tool.py       # BashTool（execute_command 返回结构化结果）
│   ├── todo_tool.py       # TodoWriteTool / TodoUpdateTool + 计划规范化 + TODO.md 持久化
│   ├── notepad_tool.py    # NotepadReadTool / NotepadAppendTool（NOTEPAD.md 长期笔记）
│   └── web_search_tool.py # WebSearchTool（Tavily 检索，懒加载、缺 Key 优雅降级）
├── providers/
│   └── openai_provider.py # create_model() 工厂
└── cli/
    └── app.py             # typer 入口（meoclaw）：按节点实时打印 📋/🔧/✅❌/📝
```

## 工具契约

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `FileReadTool` | `file_path`, `offset=0`, `limit=2000` | 返回带行号的文本；`offset` 是起始行号（0 表示第 1 行） |
| `FileWriteTool` | `file_path`, `content` | 创建或整体覆写，父目录自动创建，按给定内容原样写入（不做换行转换） |
| `FileEditTool` | `file_path`, `old_text`, `new_text` | `old_text` 必须恰好出现一次，0 次或多次都报错；保持文件原有 CRLF/LF；非 UTF-8 文件拒绝编辑；同一片段以 LF 与 CRLF 出现在不同位置时也会报错 |
| `GrepTool` | `pattern`, `path="."`, `glob=None`, `head_limit=None`, `ignore_case=False` | 纯 Python 正则实现，不依赖外部 ripgrep；`head_limit` 留空时取 `RuntimeState.grep_head_limit`（默认 100） |
| `BashTool` | `command`, `timeout_seconds=None` | 固定以 workspace 为 `cwd`，超时强制结束进程树，输出截断（保留头尾） |

`build_tools(state)` 返回的列表顺序固定为
`FileReadTool → FileWriteTool → FileEditTool → GrepTool → BashTool`，
可直接 `model.bind_tools(tools)`。

另外几组工具不属于 `build_tools`：

| 工具 | 构造方式 | 用途 |
| --- | --- | --- |
| `TodoWriteTool` | 现成实例（无状态） | planner 的**结构化输出**通道：`tool_call.args` 就是计划本身 |
| `TodoUpdateTool` | `build_todo_update_tool(runtime)` | actor / codeAgent 更新待办状态；变更记在 `runtime.metadata["todo_updates"]`，由节点合并回图状态并写进 `TODO.md` |
| `NotepadReadTool` / `NotepadAppendTool` | `build_notepad_tools(runtime)` | 工作区 `NOTEPAD.md` 的读写：把结论、决策、重要文件、阻塞点长期留存，抗上下文压缩 |
| `WebSearchTool` | `build_web_search_tool()` | Tavily 检索：返回 `{ok, query, answer, results}`；缺 `TAVILY_API_KEY` / 未装 tavily / 网络失败都返回 `ok=False` 的结构化错误，不抛异常 |
| `CallSearchAgentTool` / `CallCodeAgentTool` | supervisor 内部构造 | 把 searchAgent / codeAgent 包装成 planner 的 toolcall（见 supervisor 节点） |
| `build_read_only_tools(state)` | 函数 | 只读集合 `FileReadTool + GrepTool + NotepadReadTool + WebSearchTool`，verifier 专用（不能写、不能跑 shell、不能追加笔记；WebSearchTool 不触达工作区，允许联网复核事实） |

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

## 统一事件流

CLI 的执行阶段消费 [`core/agent.py`](src/meoclaw/core/agent.py) 的
`stream_agent_events(task, *, workspace, max_attempts=3)`，它**不再自己跑 ReAct 循环**，
而是驱动整张图：

```python
workflow.stream(inputs, stream_mode=["updates", "custom"])
```

- `updates`（节点跑完返回的 state 增量）→ 翻译成 `plan` / `actor_summary` /
  `verification` / `final_answer`；
- `custom`（节点运行中经 `get_stream_writer()` 实时吐出）→ 原样冒泡，并带上 `node` 字段。

```python
from meoclaw.core.agent import stream_agent_events

for event in stream_agent_events("创建 hello.py", workspace="./workspace", max_attempts=3):
    print(event["type"], event.get("node"), event)
```

| 事件 `type` | 关键字段 | CLI 展示 |
| --- | --- | --- |
| `node_start` | `node` | `📋 Planner …` / `🔧 Actor …` / `✅ Verifier …` / `📝 Final …` |
| `plan` | `plan_summary`, `todos`, `acceptance_criteria`, `verification_commands` | 「📋 Planner 计划」面板 |
| `ai_message` | `content`, `node`（只展示 actor 的） | 灰色打印中间说明 |
| `tool_call` | `node`, `name`, `args` | `  → 工具名 {参数 JSON}` |
| `tool_result` | `node`, `name`, `result`, `ok` | 结果面板（失败红框） |
| `verification_command` | `node`, `command`, `ok`, `exit_code` | `  [OK]/[FAIL] exit_code=N 命令` |
| `react_done` | `node` | —（节点内部 ReAct 收尾，**不是**最终回答） |
| `verification` | `passed`, `attempts`, `checks`, `results`, `last_error`, `next_instruction` | 「✅/❌ Verifier（第 N/M 轮）」面板 |
| `final_answer` | `node="final"`, `content`, `passed` | 「📝 Final 最终回答」面板 |
| `workflow_end` | `passed`, `attempts`, `final_answer`, `todos`, `last_error` | 结尾一行结论；决定退出码 |

要点：

- 节点内部的 ReAct 循环在 [`core/react.py`](src/meoclaw/core/react.py)（actor / verifier 用），
  它的收尾事件也叫 `final_answer`，转发进统一流时改名为 `react_done`，避免与 final 节点撞名。
- 工具失败（未知工具、越界、执行抛错）收敛成 `ok=False` 的结果文本，**不会中断循环**；
  只有模型/密钥/网络错误才向上抛，由 CLI 捕获后返回退出码（3=密钥问题，4=其他运行失败）。
- CLI 退出码：`0` 验证通过；`1` 未通过（`workflow_end.passed == False`）；`2` 参数/工作区问题；
  `3` 缺 API Key；`4` 其他运行失败。
- 终端显示按节点分组（📋/🔧/✅❌/📝）。若输出流编码装不下 emoji（例如 GBK 重定向到文件），
  会自动降级成 `[Planner]` / `[OK]` / `[FAIL]`，**不会抛 UnicodeEncodeError**。
- 可注入 `model=` 做测试：`tests/test_workflow.py` 用脚本化假模型跑完整图，不需要 API Key。

## LangGraph 共享状态

[`graph/state.py`](src/meoclaw/graph/state.py) 定义图状态：`MeoGraphState`（`total=False`，
节点只写自己关心的字段）、`TodoItem`、`VerificationResult`。其中

```python
messages: Annotated[list[BaseMessage], add_messages]
```

带 `add_messages` reducer，LangGraph 会把各节点新增的消息**合并追加**（按 id 去重/更新），
而不是整段覆盖。该模块刻意没有用 `from __future__ import annotations`，
以保证 LangGraph 运行时读取到的是真实注解对象。

## LangGraph 图与节点

[`graph/nodes.py`](src/meoclaw/graph/nodes.py) 提供四个节点和路由，
[`graph/workflow.py`](src/meoclaw/graph/workflow.py) 负责组装：

```
START → planner → actor → verifier → ┬─ passed 或 attempts 用尽 → final → END
                 ↑                   │
                 └──── planner ←─────┘   未通过且还有机会
```

| 节点 | 干什么 | 返回的 state 增量 |
| --- | --- | --- |
| `planner_node` | 用 `TodoWriteTool` 生成计划（工具不可用时退化成正文 JSON）；失败过就带 `last_error` + `next_instruction` 修订 | `plan_summary`, `todos`, `acceptance_criteria`, `verification_commands`, `max_attempts`, `passed=False` |
| `actor_node` | `build_tools(runtime) + TodoUpdateTool` 跑 ReAct（≤10 轮），事件转发给 `emit` | `messages`（本轮增量）, `last_actor_summary`, `todos` |
| `verifier_node` | 先**确定性执行** `verification_commands`，再用只读 agent（`FileReadTool`+`GrepTool`+`NotepadReadTool`）出 JSON 结论 | `passed`, `attempts`(+1), `verification_results`, `verification_checks`, `next_instruction`, `messages`，失败时还有 `last_error` |
| `verifier_route` | `passed` → `"final"`；`attempts >= max_attempts` → `"final"`；否则 → `"planner"` | — |
| `final_node` | 把状态整理成 `final_answer`：默认**确定性**格式化（离线也能收尾）；有模型时先用 `FINAL_PROMPT` 写一段总结，失败自动回退 | `final_answer`, `passed` |
| `context_monitor_node` | 估算 `messages + 记忆快照` 的 token 数，判定是否该压缩，并写一份 `memory_snapshot` | `context_token_count`, `context_token_limit`, `context_should_compress`, `context_next_node`, `memory_snapshot` |
| `context_monitor_route` | 已通过 → `"final"`；该压缩 → `"context_compressor"`；否则 → `context_next_node`（默认 `"verifier"`） | — |

判定规则是 **fail-closed**：只有「LLM 说通过 **且** 所有验收命令都成功」才算 `passed`——
验收命令是客观证据，可以否决模型的乐观结论。verifier 的结论解析不出来时同样按未通过处理。

> `VERIFIER_PROMPT`（[`prompts/stage3.py`](src/meoclaw/prompts/stage3.py)）里写到的能力，
> 落地情况：
> - ✅ 读文件 / grep / 读 `NOTEPAD.md` —— 已绑定（`build_read_only_tools`）；
> - ✅ "search the web" —— `WebSearchTool` 已进只读集合（不触达工作区，可联网复核事实/来源）；
> - ✅ "验收命令" —— 由**运行时**确定性地执行后把结果给 verifier（不是它自己跑），
>   这样证据不依赖模型的自觉；提示词已如实改写为"结果已在输入里，按客观证据判断"；
> - ⚠️ shell 检查 —— 只读集合里刻意**没有** `BashTool`（shell 能写文件，会破坏只读约束），
>   需要查证请依赖运行时执行的验收命令结果或 GrepTool。

### 上下文监控与压缩

`context_monitor_node` 估算 `messages + 记忆快照` 的 token 数（优先用
`model.get_num_tokens_from_messages()`，任何异常都退回 `len(text) // 4`），
超过 `context_token_limit`（默认 400000）就置 `context_should_compress`；
`context_monitor_route` 的优先级是 **已通过 → final** > **该压缩 → context_compressor** >
**上游指定的 `context_next_node`**（默认 `"verifier"`）。

`context_next_node` 由上游节点写：`planner_node` → `"actor"`（当前拓扑还有 actor 节点；
换成 stage-3 的 supervisor 拓扑时改成 `"verifier"`）、`actor_node` → `"verifier"`、
`verifier_node` 通过 → `"final"`、失败 → `"planner"`。

`context_compressor_node` 负责真正压缩：让 LLM 按
[`CONTEXT_COMPRESSION_PROMPT`](src/meoclaw/prompts/stage4.py) 产出 9 个字段的 JSON
（`summary` / `active_goal` / `completed_work` / `open_todos` / `important_files` /
`tool_findings` / `sources` / `next_steps` / `risks`，每题都有字符上限），
LLM 不可用或返回的不是 JSON 时用**确定性兜底摘要**（绝不抛异常）。然后：

- `RemoveMessage(REMOVE_ALL_MESSAGES)` 清空消息历史，放一条 `AIMessage(摘要)` 作为新起点
  （`add_messages` reducer 会消费掉 RemoveMessage，通道里只剩摘要那一条）；
- 完整摘要（9 段）写 `context_summary`，落盘 `HISTORY_SUMMARY.md`，并追加一条
  `compression_events`（含 `tokens_before`/`tokens_after`/`dropped_messages`/`used_llm`）；
- `tool_findings` 并进 `research_notes`、`sources` 规范成 `[{title,url}]`、
  `agent_handoffs` 裁到最近 6 条；
- `context_should_compress=False`（刚压过）+ 重算 `context_token_count`。

**接线方式一：`build_workflow()`（有 actor 的执行图，CLI 用的就是它）**

```
verifier ──verifier_route──┬─ passed / 试满 → final → END
                           └─ 要重试 → context_monitor ──┬─ 需要压缩 → context_compressor → planner
                                                          └─ 不需要   → planner
```

即压缩只发生在"要重试"这条分支上：成功路径完全不受影响，也不会反复触发压缩。

**接线方式二：`build_complex_workflow()`（stage-3 的 supervisor 拓扑，没有 actor）**

```
START → planner → context_monitor ─┬─ 需要压缩 → context_compressor ─┐
                                   │                                 │
                                   └────────────→ verifier ←─────────┘
                                                    │
                      verifier → context_monitor ←──┘     （验证后也过 monitor）
```

- 路由完全交给 `context_monitor_route` / `context_compressor_route`，`verifier_route` 不参与；
- ⚠️ **收敛性靠 `verifier_node`**：`context_monitor_route` 里没有 `attempts >= max_attempts`
  判断，所以 verifier 在"试满且未通过"时会把 `context_next_node` 写成 `"final"`，
  否则 `verifier → monitor → planner → monitor → verifier` 会无限打转。
  回归测试：`tests/test_workflow.py::test_complex_workflow_terminates_when_attempts_exhausted`。
- 这条拓扑里的 "planner" 节点跑的是 `supervisor_node`：在一个最多
  `MAX_SUPERVISOR_LOOPS`（8）轮的工具循环里，通过 `TodoWriteTool` 发布/修订计划，
  通过 `CallSearchAgentTool` / `CallCodeAgentTool` 把研究与实现**委派给专家 Agent**，
  自己只做协调（同 MokioAgent 的 supervisor 设计）。
  端到端回归：`tests/test_workflow.py::test_complex_workflow_supervisor_delegates_to_code_agent`。
- 注意 monitor 在 planner 之后也会过一次，所以压缩有可能发生在第一轮验证之前
  （`context_token_limit=1` 时一轮尝试会压缩两次；真实默认 400000 只有上下文真的很大才压）。

CLI 会显示这两步：`[CTX] token ≈ N / 上限 M`（需要压缩时加标注）与
`[ZIP] 上下文 N → M tokens，丢弃 K 条消息（LLM 摘要 / 确定性兜底摘要）`。

```python
from meoclaw.graph.workflow import build_workflow, initial_state

workflow = build_workflow()
result = workflow.invoke(
    initial_state("给 utils.py 加 add(a, b) 并写测试", workspace="./workspace"),
    {"configurable": {"emit": print}},      # 可选：实时事件回调
)
print(result["final_answer"])               # 完成 / 未完成 + 改动 + 验收结果
```

`initial_state(task, workspace=..., max_attempts=3, runtime=..., model=...)` 会建好
`RuntimeState` 并填好初始字段（也可注入模型，方便测试）。
`config["configurable"]` 支持的开关：`emit`（事件回调）、`model`（覆盖模型）、
`summarize_final=False`（final 节点只用确定性文本）。

模型解析顺序：`config["configurable"]["model"]` → `runtime.metadata["model"]` → `create_model()`。

提示词分布在两处：[`prompts/stage2.py`](src/meoclaw/prompts/stage2.py) 放
`PLANNER_PROMPT` / `ACTOR_PROMPT` / `FINAL_PROMPT`，[`prompts/stage3.py`](src/meoclaw/prompts/stage3.py)
放 `VERIFIER_PROMPT`（升级版）与 `CODE_AGENT_PROMPT`；`from meoclaw.prompts import VERIFIER_PROMPT`
永远拿到当前版本（`core/agent.py` 里的 `ACTOR_PROMPT`
只是 re-export，保持老 import 路径可用）。

> ⚠️ `nodes.py` 里**不要**加 `from __future__ import annotations`：langgraph 用
> `inspect.signature(...).annotation` 判断是否注入 `config`，它只认真实的
> `RunnableConfig` / `Optional[RunnableConfig]`（或恰好写成 `"Optional[RunnableConfig]"` 的字符串）。
> 注解一旦变成 `"RunnableConfig | None"` 字符串，langgraph 会**静默跳过** config 注入，
> `emit` 就再也收不到事件。`tests/test_nodes.py::test_nodes_work_inside_a_real_stategraph`
> 专门钉住了这条。

## 专家 Agent（codeAgent）

[`agents/code_agent.py`](src/meoclaw/agents/code_agent.py) 实现实现类专家：由 planner 委派，
在 workspace 内用文件/命令工具把指令落地。

```python
from meoclaw.agents.code_agent import run_code_agent

result = run_code_agent(state, "实现 add(a, b) 并补测试", writer=emit, max_loops=10)
result["ok"], result["summary"], result["todos"], result["tool_events"]
```

| 返回字段 | 含义 |
| --- | --- |
| `ok` | 本轮是否正常收尾（撞到 `max_loops` 时为 False） |
| `summary` | 收尾总结 |
| `todos` | 已合并 `TodoUpdateTool` 变更的待办列表 |
| `messages` | 本轮新增的 AI / ToolMessage（可直接进图状态） |
| `tool_events` | 本轮全部事件（带 `agent="codeAgent"`），同时会转发给 `writer` |
| `stopped_reason` / `loops` / `todo_file` | 停止原因 / 实际轮数 / `TODO.md` 相对路径 |

要点：

- 系统提示词是 [`CODE_AGENT_PROMPT`](src/meoclaw/prompts/stage3.py)；HumanMessage 由
  **任务 + planner 指令 + session 上下文（计划/待办/上轮失败）+ memory 快照** 拼成。
- 工具集 = `build_tools(runtime)` + `TodoUpdateTool` + `NotepadReadTool`/`NotepadAppendTool`
  （提示词里要求用笔记工具，所以笔记工具必须一起绑定）。
- 收尾时把待办写进工作区 `TODO.md`（`- [x] / [ ] / [~] / [!]` 对应四种状态）。
- `build_memory_snapshot()` / `result["memory_snapshot"]` 直接复用三层 Memory 实现
  （见下一节），codeAgent 与图节点看到的是同一份记忆。
- 内部复用 `core.react.stream_react_events()`，所以 tool_call_id 配对、工具失败降级成
  `ok=False`、输出截断等行为与 actor 节点完全一致。
- 本模块**不 import `graph.nodes`**（共用 `core.context`），因此 planner 将来直接调用它也不会成环。

## 专家 Agent（searchAgent）

[`agents/search_agent.py`](src/meoclaw/agents/search_agent.py) 实现检索专家：由 supervisor 的
`CallSearchAgentTool` 委派，用 `WebSearchTool` 把调研指令转成研究摘要 + 结构化来源。

```python
from meoclaw.agents.search_agent import run_search_agent

result = run_search_agent(state, "查一下 add_messages 的合并语义", writer=emit)
result["ok"], result["summary"], result["queries"], result["sources"]
```

| 返回字段 | 含义 |
| --- | --- |
| `ok` | 本轮是否正常收尾（撞到 `max_loops` 时为 False，默认上限 4 轮） |
| `summary` | 研究摘要（没有摘要时回落到各次检索的 `answer` 拼接） |
| `queries` | 实际发出的检索词 |
| `sources` | 按 URL 去重后的 `[{title, url}]`（截断到 12 条） |
| `messages` / `tool_events` | 本轮新增消息 / 全部事件（带 `agent="searchAgent"` 标记） |

要点：

- 系统提示词是 [`SEARCH_AGENT_PROMPT`](src/meoclaw/prompts/stage3.py)：唯一外部能力是
  `WebSearchTool`，偏好官方/百科来源，不写文件、不产代码；
- 检索失败（缺 Key / 未装 tavily / 网络错误）不进 `sources` 也不打断循环——
  结果文本会回到模型，让它自行调整；
- 与 codeAgent 同构：复用 `core.react.stream_react_events()` + 三层记忆快照，
  测试见 [`tests/test_search_agent.py`](tests/test_search_agent.py)。

## supervisor 节点（委派形态的 planner）

[`graph/nodes.py`](src/meoclaw/graph/nodes.py) 的 `supervisor_node` 是 planner 的
supervisor 形态，`build_complex_workflow()` 的 "planner" 节点用的就是它：

1. 校验任务后组装三层记忆快照，绑定三个工具：
   `TodoWriteTool` / `CallSearchAgentTool` / `CallCodeAgentTool`；
2. 进入最多 `MAX_SUPERVISOR_LOOPS`（8）轮的工具循环：
   - `TodoWriteTool` → 规范化计划、写 `TODO.md`、实时发 `plan_snapshot` 事件；
   - `CallSearchAgentTool` / `CallCodeAgentTool` → 发 `handoff` 事件、运行专家 Agent、
     合并 `research_notes` / `sources` / `todos` / `agent_handoffs`，发 `handoff_result`；
   - 未知工具 / 专家执行异常 → `ok=False` 的结果文本回到模型，循环不中断；
3. 模型没有产出任何计划时，把任务本身兜底成一条待办，保证 verifier 有东西可验收；
4. 返回 planner 的全部计划字段 + 委派产出，并把 `context_next_node` 写成 `"verifier"`。

## 三层 Memory

[`graph/memory.py`](src/meoclaw/graph/memory.py) 把"该让模型记住什么"收在一处，
**由运行时组装，不给 Agent 暴露写记忆的工具**（正是 `RULES_LAYER` 的最后一条）。

```python
from meoclaw.graph.memory import build_layered_memory, format_layered_memory_for_prompt

memory = build_layered_memory(state, node="planner")     # dict，三层
prompt_part = format_layered_memory_for_prompt(memory)   # JSON 文本，直接拼进提示词
```

| 层 | 内容 | 特点 |
| --- | --- | --- |
| `rules` | `RULES_LAYER`：工作区范围、相对路径写法、`TODO.md`/`NOTEPAD.md`/`HISTORY_SUMMARY.md` 分工、不给 Agent 记忆写工具 | 固定不变 |
| `working_memory` | `node`、任务、`session_id`/`session_turn`（取自 `runtime.metadata`）、计划、待办、验收标准、验收命令、`research_notes`(1600)、`sources`（只留 title/url）、`agent_handoffs`（最近 6 条）、`code_agent_summary`(1000)、`verifier_summary`(1000)、`last_error`(1400)、`attempts`/`max_attempts` | 每轮刷新、每个字段有上限 |
| `history_summary_store` | `HISTORY_SUMMARY.md`（2200）、`NOTEPAD.md`（1800）、`context_summary`(1600)、最近 3 条 `compression_events` | 跨上下文压缩保留 |

配套：

- `read_notepad(runtime)` / `read_history_summary(runtime)` → `{path, exists, content}`（读不到只是 `exists=False`，绝不让记忆组装失败）；
- `append_history_summary(runtime, content, heading=...)` → 压缩步骤写 `HISTORY_SUMMARY.md`；
  超出 `max_read_bytes` 时**最新一段完整保留**、旧历史按剩余预算保留尾部；
- `_short_text(text, limit)` 截断补 `"..."`；`_trim_handoffs()` 只留最近 6 条并压短字段；
- `format_layered_memory_for_prompt(memory)` 用 `json.dumps(..., ensure_ascii=False, indent=2)` 输出。

`MeoGraphState` 里对应的字段：`memory_snapshot`（最近一次快照）、`history_summary`、
`context_summary`、`context_token_count`、`context_token_limit`、`context_should_compress`、
`context_next_node`、`compression_events`，以及 `research_notes`/`sources`/`agent_handoffs`/
`code_agent_summary`/`verifier_summary`。

> 记忆组装已经接进主链路：codeAgent / searchAgent / supervisor 的提示词都会带上
> `Memory snapshot`，`context_monitor_node` 每次都会写一份 `memory_snapshot` 进图状态，
> 压缩节点（`context_compressor_node`）也已实现 `RemoveMessage(REMOVE_ALL_MESSAGES)`
> 的真压缩并把摘要落盘 `HISTORY_SUMMARY.md`。

## 安全模型

1. **路径安检在 `core/paths.py`**：`resolve_in_workspace()` 先展开 `~`、跟随符号链接、折叠 `..`，
   再确认结果仍在 workspace 内；`..` 上跳、绝对路径越界、指向外部的符号链接都会被拦下。
   这一层不认识 langchain，越界直接抛 `WorkspaceError`。
2. **工具层**：文件工具和 GrepTool 都通过 `resolve_tool_path()` 走同一条检查，并把
   `WorkspaceError` 翻译成 `ToolException`（"路径不存在"和"路径越界"都是如此）。
   工具失败以 `ToolException` 表达，由调用方转成模型可见的文本——
   `core.agent.execute_tool` 会收敛成 `ok=False` 的结果文本，LangGraph 的 `ToolNode`
   也会捕获 `ToolException` 并返回其 message，所以一次写错路径不会打断 Agent 循环，
   同时"失败"不会被误当成"成功返回"（这也是这里**不设** `handle_tool_error=True` 的原因）。
   可预期的失败（匹配多处、正则非法、文件过大、非 UTF-8 编辑等）同理。
   GrepTool 遍历时会逐个候选文件再安检一次，因此 `glob="../*.py"` 这类写法会被直接拒绝，
   指向外部的符号链接也不会被读取。
3. **BashTool 的边界**：`cwd` 固定在 workspace，但 shell 命令本身无法被 Python 真正沙箱化
   （`cd ..`、写绝对路径仍然可能发生）。需要强隔离时，请把工作区放到容器或低权限用户下运行。
   Windows 下 `shell=True` 走 `%COMSPEC%`（通常是 cmd.exe），POSIX 专用命令会失败。

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
`build_workflow()` 的真实图回路与**统一事件流**（`node_start`/`plan`/`tool_call`/`verification`/
`react_done`/`final_answer`/`workflow_end`，以及 config 注入不被
`from __future__ import annotations` 破坏）、
专家 Agent（codeAgent：todo 回写与 `TODO.md` 持久化、`NOTEPAD.md` 读写、max_loops 收尾；
searchAgent：检索事件、来源去重、摘要回落、缺 Key 优雅降级；
`tests/test_search_agent.py`）、WebSearchTool（缺 Key、Tavily 结果解析、schema、只读集合）、
三层 Memory（字段上限与截断、`NOTEPAD.md`/`HISTORY_SUMMARY.md` 读取、压缩历史裁剪、
交接记录只留最近 6 条、来源只留 title/url、会话字段兜底）、
上下文监控（token 估算与 `len//4` 兜底、压缩阈值、路由优先级与默认去向、真实 StateGraph 里跑通）、
上下文压缩（LLM 9 字段摘要与确定性兜底、`RemoveMessage` 清空历史、字段截断、事件上限、
`HISTORY_SUMMARY.md` 落盘，以及**真图里"验证失败 → 压缩 → 重新规划 → 通过"的完整回路**）、
复杂拓扑（`build_complex_workflow` 无 actor、`context_compressor_route`、**试满后收敛不死循环**、
压缩在验证前后各触发一次的真实行为、**supervisor 委派 codeAgent 改文件并验收通过的端到端**），
CLI 的 `--list-tools`/参数校验/缺密钥退出码，以及经 `StructuredTool.invoke()` 的端到端调用。
