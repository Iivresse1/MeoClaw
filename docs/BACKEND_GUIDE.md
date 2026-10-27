# MeoClaw 后端构思与实现

> 适用快照：MeoClaw `1.1.0`，分支 `MeoAgent1.1`，制品标签 `ver3.5`。
> 范围：后端，包括 Bridge、LangGraph、LangChain Agent、工具、持久化、沙箱、测试与发布。

---

## 一、构思整体方向需要遵循的东西

	最开始构思 MeoClaw，第一件事不是选 LangGraph，也不是写 planner 提示词，而是先定义后端必须长期守住什么。我先是有一个agent的大概描绘，然后再通过grill-me把它转成具体的后端设计准则

MeoClaw 的后端准则可以概括成这些：

1. **能执行**：接收一个任务，调用模型和工具，产生可检查的结果。
2. **能证明**：任务是否完成不能只靠模型自述，必须有验收命令或可复核证据。
3. **能隔离**：文件、命令和系统工具只能访问被允许的资源。
4. **能继续**：会话状态、任务状态和中断点可以在后续任务中重新使用。
5. **能观察**：计划、工具调用、命令结果、失败原因必须以稳定事件输出。
6. **能失败**：模型、网络、Docker、数据库或工具出错时，系统给出明确结果，不假装成功。

因此，后端设计的第一份草稿需要考虑到我可能失败的地方，比如：

```text
模型说“已完成”，但文件没有改
模型生成的命令删除了工作区外文件
Agent 在两个工具之间无限循环
上下文越来越长，最终超出模型窗口
同一会话的新任务忘记旧任务
进程在审批中途重启
路径中的 .. 或符号链接逃出工作区
API Key 被写进普通 JSON
Bridge 被其他网页或本机进程随意调用
```

每一个失败清单项目，最终都要落到一个状态、策略、中间件、工具边界或测试上。

---

## 二、一个 Main Agent，三档权限

Agent 身份与权限策略必须拆开。所有用户请求进入同一个 LangGraph 入口：

- 普通问答进入只读 responder，只允许 WebSearch/WebFetch；
- 需要文件、shell 或系统操作时进入 planner → actor → verifier；
- 用户只选择 `human / ai / full` 三档权限，不再选择“对话 Agent”或“系统 Agent”。

| 权限 | 敏感动作 | 低风险动作 |
| --- | --- | --- |
| `human` | 文件写入、文件编辑、笔记追加、shell、剪贴板读取逐次人工确认 | 截图、打开 URL、写剪贴板、已知文档类型直接执行 |
| `ai` | 独立 Reviewer 判断 shell 安全度和文件修改范围；不确定时转人工 | 与人工审核相同 |
| `full` | 不请求审批 | 不请求审批；路径、凭据、Docker 和 verifier 硬边界仍生效 |

内部 `TODO.md` 更新属于运行状态，不触发文件审批。`NotepadAppendTool` 写入用户可见
的长期笔记，因此属于敏感文件写入。`OpenWorkspaceFile` 只直接打开白名单文档类型；
可执行文件和未知类型转人工。

---

## 三、后端拆成五层

从零设计时，先画出下面这张图：

```text
调用方
  │ REST / WebSocket
  ▼
Bridge
  ├─ 鉴权、会话、任务、事件队列
  └─ 后台线程与审批恢复
  │
  ▼
任务工作流
  ├─ planner
  ├─ actor / specialist
  ├─ verifier
  └─ final
  │
  ▼
单 Agent 执行循环
  ├─ 模型
  ├─ 工具
  ├─ middleware
  └─ 事件翻译
  │
  ▼
工具与 RuntimeState
  ├─ 文件、搜索、命令
  ├─ 路径限制
  └─ Docker 执行策略
  │
  ▼
持久化
  ├─ 会话消息
  ├─ graph checkpoint
  ├─ 工作区文件
  └─ 密钥
```

每一层的唯一职责：

| 层 | 应该负责 | 不应该负责 |
| --- | --- | --- |
| Bridge | API、鉴权、线程、队列、持久化入口 | 判断任务是否完成 |
| 工作流 | 阶段、重试、条件边、验收语义 | 直接实现文件工具 |
| Agent | 单角色内的模型工具循环 | 决定整个任务是否需要重试 |
| 工具与 RuntimeState | 执行能力、预算、路径与命令边界等控制参数 | 生成业务计划 |
| 持久化 | 保存契约化状态 | 猜测哪些消息可见 |

这种分层解决的核心问题是“一个变化只改一处”。例如新增一个工具，不应该改工作流；改变验收入口就改工作流的部分而不是别的地方。到这里为止，我也差不多想好了整体的架构是什么样的。

---

## 四、第一批要定义的契约

### 4.1 三个 ID 的生命周期

写代码前先定义身份：

| 名称 | 含义 | 生命周期 |
| --- | --- | --- |
| `session_id` | 一段持续会话 | 跨多个任务，绑定消息、工作区和 checkpoint |
| `task_id` | 一次执行请求 | 从请求开始到结束 |
| `thread_id` | LangGraph 恢复身份 | 通常由 `session_id` 或 `task_id` 映射而来 |

MeoClaw 当前规则：

- `session_id` 是 12 位十六进制；
- 所有 Main Agent 任务使用 `session_id` 作为 `thread_id`；
- 同一 session 的后续任务进入同一条 checkpoint 时间线；
- `task_id` 只标识一次执行和对应的事件队列。

身份一旦混用，会出现“消息串会话”“审批恢复错任务”“删除工作区删错目录”等问题，所以必须先定下来。

### 4.2 REST 契约

后端最小 API 面可以这样设计：

```text
GET    /api/health
GET    /api/sessions
GET    /api/sessions/{sid}/messages
POST   /api/sessions/{sid}/archive

GET    /api/settings
POST   /api/settings
POST   /api/providers
DELETE /api/providers/{name}
POST   /api/providers/active
POST   /api/test-connection
GET    /api/tavily
POST   /api/tavily
DELETE /api/tavily

POST   /api/task
POST   /api/approval/{aid}
WS     /ws/task/{tid}
```

REST 负责创建资源和提交命令，WebSocket 负责执行过程。

`POST /api/task` 只返回：

```json
{
  "ok": true,
  "task_id": "...",
  "session_id": "..."
}
```

这种设计把“提交任务”和“观察任务”解耦。即使任务执行一分钟，创建请求也能快速返回。

### 4.3 事件契约

事件是后端对调用方的公开协议。先定义语义，再接入具体框架。

```text
node_start
ai_token
ai_message
plan
tool_call
tool_result
verification_command
verification
actor_summary
approval_request
react_done
final_answer
workflow_end
ui_done
error
```

必须明确几个容易混淆的事件：

- `tool_call` 表示即将执行，不代表成功。
- `tool_result` 才代表有工具结果，并且要带 `ok`。
- 内层 Agent 的 `final_answer` 在进入外层事件时改成 `react_done`。
- 真正的任务最终回答来自外层 `final` 节点。
- `ui_done` 是传输结束哨兵，不是业务成功。
- 是否通过任务看 `passed`，进程退出看退出码，两者语义不同。

MeoClaw 当前把 LangChain/LangGraph 的原生流翻译成上述事件。这样未来框架内部事件变化时，Bridge 和调用方仍能依赖稳定协议。不过其实是因为我发现用langchain框架可读性更好，所以从原来的粗糙手写换成了langchain和langgraph，但是伴随着的就是项目变重了，细节会更加难调整。

---

## 五、定义状态再写节点

节点只是状态的转换器。状态不清楚时，直接写节点会把业务事实散落到提示词和局部变量里。关于状态量的思考直接反映对于项目整体的构建思路。

### 5.1 哪些内容属于图状态

`MeoGraphState` 应保存跨节点需要共享的权威事实：

| 类别 | 字段 | 作用 |
| --- | --- | --- |
| 任务 | `task` | 用户原始目标 |
| 运行依赖 | `runtime` | 工作区、预算、ID、模型 |
| 计划 | `plan_summary`、`todos` | 做什么 |
| 成功标准 | `acceptance_criteria` | 怎样算完成 |
| 客观证据 | `verification_commands`、`verification_results` | 如何证明完成 |
| 验证判断 | `verification_checks` | 模型检查与命令检查 |
| 路由状态 | `passed`、`attempts`、`max_attempts` | 是否继续 |
| 失败反馈 | `last_error`、`next_instruction` | 下一轮怎么修 |
| 消息 | `messages` | 规范对话时间线 |

使用 `TypedDict(total=False)` 的理由如下：每个节点只返回自己改变的字段。没有写的字段不应报错，已有字段也不会被无关节点重建。

### 5.2 `messages` 必须使用 reducer

```python
messages: Annotated[list[BaseMessage], add_messages]
```

`add_messages` 让节点新增消息被合并，而不是覆盖整段历史。

如果没有 reducer，就会出现：

```text
planner 看到用户任务
actor 返回局部消息
verifier 看不到原始任务
下一轮 planner 失去上一轮失败上下文
```

消息时间线解决“事实是否保留”，ContextBuilder 再解决“哪个角色看哪些事实”。

### 5.3 `RuntimeState` 保存运行能力

`RuntimeState` 不是业务计划，它描述本次运行允许使用什么：

- 唯一工作区；
- 单文件读取上限；
- 工具输出上限；
- shell 超时；
- `session_id`、`task_id`、`thread_id`；
- 可选模型；
- Todo 临时更新记录。

将所有工具绑定到同一个 `RuntimeState`，可以确保每次任务使用自己的目录和预算，而不是依赖进程全局状态。

### 5.4 消息要带上下文标签

至少需要两个标签：

- `context_task_id`：这条消息属于哪个任务；
- `context_roles`：planner、actor、verifier、final 中哪些角色可见。

标签的作用不是限制数据库保存，而是让 ContextBuilder 能安全地做角色投影。

---

## 六、设计外层工作流

### 6.1 默认图从最小闭环开始

对工作区任务，简单设计一个最小完整闭环像这样：

```text
START → planner → actor → verifier → final
                     ↑        │
                     └────────┘
```

条件边由状态控制：

```text
passed = true
    → final

passed = false 且 attempts < max_attempts
    → planner

passed = false 且 attempts >= max_attempts
    → final
```

这个图有三个重要特性：

- 计划不是一次性文本，而是后续节点可读取的结构化状态。
- 验证失败会带着具体原因回到 planner，不是盲目重试。
- 重试次数有硬上限，不会无限循环。

### 6.2 每个角色使用不同能力

不靠提示词说“你不能修改文件”。真正的最小权限是换工具集。对于不同的节点我只给对应工作需要的工具集，这样就算LLM会发癫去干坏事也不能施展自己的手脚，不会出现执行rm -rf类似的严重失误。

| 角色 | 工具能力 | 目的 |
| --- | --- | --- |
| planner | `TodoWriteTool` | 只发布结构化计划 |
| actor | 文件读写、Grep、WebSearch/WebFetch、Docker shell、Todo 更新 | 实施改动 |
| verifier | 文件读取、Grep、笔记读取、WebSearch/WebFetch | 只查证 |
| final | 无工具 | 汇总结果 |

这种设计使“verifier 不能改文件”成为代码约束，而不是模型自觉。

### 6.3 验收必须 fail-closed

任务通过需要同时满足：

```text
verifier 返回可解析 JSON，且 passed = true
所有验收命令 exit code = 0
```

以下情况一律不通过：

- verifier 输出不是 JSON；
- verifier 声称通过，但命令失败；
- 命令无法启动；
- Agent 用完模型/工具预算仍然没有收尾；
- 达到最大验证轮数但任务没有完成。

这是 MeoClaw 最重要的后端不变量：**模型意见不能否决客观证据。**

### 6.4 复杂拓扑作为可选能力

当任务需要“先搜索资料，再实施”时，这个时候任务已经开始变得复杂起来了。那么单一的agent肯定不能完美执行了，因为搜索资料在实施这种事情会加剧上下文过长等等的问题，降低我们输出的准确性。那么就该构建复杂拓扑，然后采用mutliagent的方案。可以在同一状态模型上构建 supervisor 图：

```text
START → supervisor(planner) → verifier → final/planner
```

supervisor 通过工具委派：

- `searchAgent`：WebSearch/WebFetch；
- `codeAgent`：文件、shell、Todo、笔记，以及只读联网工具。

两个专家 Agent 返回摘要、来源和交接记录，supervisor 再统一交给 verifier。

当前 MeoClaw 已实现这条图，但 Bridge 默认仍调用简单图。因此要区分：

```text
已有代码能力 ≠ 当前默认产品行为
```

---

## 七、设计内层 Agent 循环

### 7.1 不要手写模型工具循环

如果一个角色需要：

```text
模型决定工具调用 → 工具执行 → ToolMessage → 模型继续
```

应优先把循环交给 LangChain `create_agent`，后端只负责：

1. 选择模型；
2. 绑定工具；
3. 组装 middleware；
4. 消费原生 stream；
5. 翻译业务事件；
6. 收集最终消息与状态。

这样可减少自己的状态机错误，也让错误、预算、重试和审批统一由 middleware 管理。当然了我觉得有些时候是不能全靠langchain中间件的，总有中间件不能考虑到的情况，之后的项目改进我会尝试加入更多自己的裁定逻辑，提高整体输出的确定性和准确性。

### 7.2 中间件按横切职责划分

| Middleware | 职责 | 不应该做什么 |
| --- | --- | --- |
| `JsonToolResultMiddleware` | 将工具结果统一成 JSON | 判断业务成功 |
| `ToolErrorMiddleware` | 把工具异常转成模型可读的 `ok=false` | 吞掉进程级故障 |
| `ToolRetryMiddleware` | 只重试 WebSearch 临时失败 | 重试写文件和 shell |
| `ModelCallLimitMiddleware` | 限制模型调用次数 | 计算验证轮数 |
| `ToolCallLimitMiddleware` | 限制工具调用总数 | 决定任务是否完成 |
| `SummarizationMiddleware` | 当前 Agent 上下文过长时压缩 | 保存跨任务历史 |
| `ToolPermissionMiddleware` | 按权限策略放行、拒绝、人工批准或 AI 审核 | 执行工具本身 |
| `ShellToolMiddleware` + Docker policy | 提供隔离 shell | 判断验收是否通过 |

重试策略尤其要克制。WebSearch 可以安全重试；文件写入、剪贴板写入和 shell 命令可能有副作用，不能自动重复。

### 7.3 流式事件的翻译规则

内层 Agent 应同时消费两类原生流：

- `messages`：模型增量文本，映射为 `ai_token`；
- `updates`：模型消息、工具消息和中断，映射为 `ai_message`、`tool_call`、`tool_result`、`approval_request`。

内层 Agent 收尾事件必须改名，避免与外层图最终回答混淆。

一个好事件适配层应保证：

- 事件有稳定 `type`；
- 工具事件有 `name`、`args`、`ok`；
- token 增量可以拼接成最终文本；
- 中断和恢复都可表达；
- 不把框架内部对象直接暴露给调用方。

---

## 八、设计上下文与 Memory

### 8.1 先分清四种 memory

| 类型 | 存在位置 | 生命周期 | 作用 |
| --- | --- | --- | --- |
| 规范消息时间线 | checkpoint | 同会话跨任务 | 保存发生过的事实 |
| 角色上下文 | 每次模型调用临时生成 | 单次调用 | 决定当前角色看到什么 |
| 工作记忆 | `MeoGraphState` | 当前图，随 checkpoint 保存 | 计划、失败、验收事实 |
| 文件记忆 | 工作区文档 | 跨任务 | `TODO.md`、`NOTEPAD.md`、`HISTORY_SUMMARY.md` |

这四者不能合并成一个字符串。否则会出现事实重复、角色污染和压缩后无法追溯。checkpoint的作用就在于保存当前会话的内容，如果中断了还能接续上去再来。

### 8.2 ContextBuilder 是上下文编译器

规范消息时间线不等于每次把所有消息都送给模型。

ContextBuilder 应为每个角色生成最小安全投影：

| 角色 | 上下文内容 |
| --- | --- |
| planner | 少量跨任务对话、计划、失败原因、研究结果 |
| actor | 当前任务完整 AI/tool 消息对、计划、预算 |
| verifier | 任务、成功标准、命令结果、工具证据文本 |
| final | 验收结果、执行摘要、下一步 |

它解决四个问题：

1. 保留跨节点和跨任务连续性；
2. 避免 ToolMessage 与原始 tool call 脱节；
3. 避免 verifier 被 actor 的自我叙述带偏；
4. 控制 token 成本。

### 8.3 三层 Memory 快照

`build_layered_memory()` 生成：

1. **Rules**：固定约束；
2. **Working Memory**：当前任务、计划、失败、来源、轮数；
3. **History Summary Store**：从历史摘要和长期笔记读取的有界文本。

每一层必须有长度限制，不能假设历史永远很小。

### 8.4 自动长期历史摘要的闭环

长期摘要由两层机制分担，职责不能混在一起：

| 机制 | 触发时机 | 作用范围 | 结果写到哪里 |
| --- | --- | --- | --- |
| `SummarizationMiddleware` | 单个 Agent 的上下文超过阈值 | 一次 ReAct 调用 | 只替换本次 Agent 输入中的长消息 |
| `final` 节点 | 一次任务通过，或重试次数耗尽 | 整个任务 | 追加到工作区 `HISTORY_SUMMARY.md` |

任务结算时，`final` 节点调用运行时的确定性函数
`summarize_task_for_history()`，只摘取任务、计划、执行结论、验收命令结果、
最终失败原因和下一步建议。它不会把原始聊天记录、完整工具输出或密钥写入长期记忆，
也不会额外调用一次模型。

因此完整信息流是：

```text
任务执行结束
  → final 节点生成有界结算摘要
  → 运行时按 task_id 去重后追加 HISTORY_SUMMARY.md
  → 下一任务或进程重启
  → ContextBuilder 读取文件并投影给 planner / actor / verifier / final
```

`HISTORY_SUMMARY.md` 超过预算时只保留最新内容，ContextBuilder 再次截断后才进入提示词。
这样 checkpoint、文件记忆和角色上下文各有边界：checkpoint 保留原始时间线，
长期摘要保留跨任务结论，ContextBuilder 决定当前角色最终能看到什么。

---

## 九、设计持久化与恢复

### 9.1 三种持久化各管一类事实

| 存储 | 内容 | 用途 |
| --- | --- | --- |
| `store.db` | 会话、用户消息、助手消息 | 会话列表和历史展示 |
| `checkpoints.sqlite` | LangGraph 状态、消息、待恢复节点 | 图连续性、审批恢复 |
| 会话工作区 | 用户文件、`TODO.md`、笔记 | 实际任务产物 |

非敏感设置存 JSON；API Key 优先使用系统 keyring，失败时才降级到受限本地文件。

### 9.2 `thread_id` 决定连续性

workspace 任务用 `session_id` 作为外层图 `thread_id`。同一 session 的新任务会合并到既有 checkpoint，所以 ContextBuilder 能看到上一任务留下的消息。

这不是“把所有旧消息直接塞进模型”，而是：

```text
checkpoint 保留完整时间线
ContextBuilder 再按actor裁剪
```

### 9.3 恢复能力要写到真实边界

当前实现有两层恢复条件：

- LangGraph 中断状态存在 SQLite checkpoint；
- Bridge 在进程内保存审批 ID、任务队列和恢复参数。

这意味着：

```text
Bridge 不退出：审批可以恢复
Bridge 进程重启：checkpoint 还在，但当前注册表丢失
```

如果产品要求“杀掉应用后继续审批”，需要新增持久化任务上下文、审批映射和恢复入口。不能把“checkpoint 已持久化”直接等同于“业务任务可跨进程恢复”。

### 9.4 并发边界

Bridge 用 `task_id → session_id + queue` 管理正在执行的任务。每个任务有独立队列，WebSocket 消费后发送 `ui_done`。

从零设计时要确定：

- 同一 session 是否允许多个任务同时运行；
- 后发任务是否应等待前一个任务完成；
- 两个任务同时写同一工作区时谁赢；
- Bridge 重启后正在运行的任务如何标记为中断。

当前实现更接近“每个任务独立后台线程”，尚未形成工作区级写入串行化协议。

---

## 十、设计安全边界

### 10.1 第一道门：Bridge 鉴权

Bridge 只监听本机，但仍然需要 capability token：

- REST 使用 `Authorization: Bearer <token>`；
- WebSocket 使用 token 查询参数；
- CORS 使用明确白名单；
- 健康检查可以例外。

原因是“本机服务”仍然可能被浏览器页面或其他低权限进程调用。

### 10.2 第二道门：会话与路径

后端必须保证：

- `session_id` 只接受受控格式；
- 工作区必须是 sessions 根目录的直接子目录；
- 删除会话前检查数据库存在性和路径父级；
- 所有文件工具先经过同一个 `resolve_in_workspace()`。

路径解析要处理：

```text
相对路径
绝对路径
..
~ 展开
符号链接
不存在的目标
```

路径检查必须在工具执行点完成，不能只依赖提示词提醒。

### 10.3 第三道门：actor能力

- planner 只能发布计划；
- verifier 只能只读查证；
- searchAgent 只能联网搜索和读取网页；
- Main Agent 的 actor 可写文件、运行 Docker shell 和使用受限桌面工具；
- codeAgent 才可写文件和运行 shell，联网工具仍为只读；
- 工具是否直接执行由 `ToolPermissionMiddleware` 根据当前权限策略决定。

能力边界由工具集决定，不由模型名字决定。

### 10.3.1 联网工具权限

`WebSearchTool` 与 `WebFetchTool` 是全局只读工具，在 Main Agent、
verifier、codeAgent 和 searchAgent 中可用。两者不访问本地工作区，
不要求 HITL 审批；WebFetch 只接受 `http(s)` URL，并通过 Tavily Extract
返回有字符上限的网页正文。`ToolRetryMiddleware` 仍只重试 WebSearch，
避免重复抓取产生额外副作用。

### 10.3.2 Tavily Key 配置

新用户可在设置页「联网检索」中打开 Tavily 控制台，粘贴 Key 后点击
「一键配置」。Bridge 会先执行一次真实检索验证，再通过 `tavily:api_key`
写入系统 keyring；Key 不会返回给调用方，也不会进入 `settings.json` 或
graph checkpoint。CLI 或自动化环境仍可使用 `TAVILY_API_KEY`，其优先级高于
应用内配置。Bridge 以任务级上下文把 Key 传给工具，任务结束后自动释放。

### 10.4 第四道门：Docker shell

工作区 shell 的设计目标：

- 默认无网络；
- 限制 CPU 和内存；
- 只读 rootfs；
- 只挂载当前会话工作区；
- 容器退出即删除；
- Docker 不可用时拒绝执行。

执行边界由同一个 Docker policy 统一：

```text
actor/codeAgent 的 BashTool → Docker ShellToolMiddleware（持久 shell 会话）
verifier 的验收命令 → execute_command() → Docker 一次性命令
```

两条路径共用镜像、资源限制、网络策略和 workspace 挂载规则。verifier 的每条命令
都通过 `/bin/bash -lc` 在独立容器中执行，命令字符串不会被宿主 shell 解释。
容器使用唯一名称，正常退出时由 `--rm` 删除；超时或中断时运行时会额外执行
`docker rm -f`。Docker 缺失时 `execute_command()` 抛出工具错误，验收结果记为失败，
不会回退到宿主 subprocess。

CLI 与 Bridge 默认启用 `MEOCLAW_SANDBOX=docker`。直接调用 Python API 时，
调用方也应在 `runtime.metadata["sandbox"]` 中设为 `docker`，避免使用测试或开发用的
`host` 模式。

---

## 十一、设计失败模型

后端必须区分不同失败：

| 失败类型 | 示例 | 结果 |
| --- | --- | --- |
| 输入错误 | 空任务、非法 session ID | 立即拒绝 |
| 配置错误 | 没有模型供应商、没有 API Key | 明确配置错误 |
| 工具业务失败 | 文件不存在、测试失败 | `ok=false`，可反馈给模型 |
| 验证失败 | 验收命令非零、verifier 不通过 | `passed=false` |
| 安全拒绝 | 路径越界、Docker 不可用 | 拒绝执行，fail-closed |
| 基础设施失败 | Bridge、数据库、线程异常 | 错误事件并结束任务 |

当前 CLI 退出码可以这样理解：

| 退出码 | 含义 |
| --- | --- |
| `0` | 任务通过验证 |
| `1` | 任务未通过验证 |
| `2` | 参数或任务输入错误 |
| `3` | 缺少 API Key |
| `130` | 用户中断 |

后台 worker 是异常边界。工具异常应转成模型可处理的工具消息，但 worker 边界必须把未恢复异常输出为 `error` 事件，并最终发送 `ui_done`，避免调用方永久等待。

---

## 十二、先设计测试接缝，再实现

### 12.1 测试不依赖真实模型

用脚本化 Fake Model 测试控制和状态：

- 第一轮返回计划；
- 第二轮调用写文件工具；
- verifier 先失败再通过；
- 命令失败时否决模型的乐观结论；
- 达到最大轮数后停止。

这类测试稳定、免费，能准确验证契约。

真实模型测试只用于验证：

- 模型供应商配置；
- 流式 token 兼容性；
- 提示词在真实模型上是否可用；
- 结构化输出是否在实际环境下稳定。

### 12.2 测试应放在最高接缝

| 接缝 | 适合验证 |
| --- | --- |
| `build_workflow().invoke()` | 图状态、重试、最终结果 |
| `stream_agent_events()` | 节点顺序和公开事件 |
| `stream_react_events()` | 单个 Agent 工具循环 |
| `ContextBuilder.build()` | 角色看到什么 |
| Bridge API | 鉴权、会话和输入拒绝 |
| Docker policy | 容器参数和工作区挂载 |

对外事件协议最好在 Bridge 或 `stream_agent_events()` 层做一次端到端测试，而不是只测内部函数。

### 12.3 安全测试必须验证“拒绝”

不要只测试正常返回值。至少要有：

- `../` 和绝对路径越界；
- 符号链接指向工作区外；
- 非法 session ID；
- 无 token 的 REST 和 WS；
- 非白名单 Origin；
- Docker 不存在时拒绝 shell；
- verifier 没有写工具；
- 工具错误不会让 Agent 假装成功。

### 12.4 本地验证入口

```powershell
cd D:\myproject\MeoAgent1.1
.\scripts\build-sandbox.ps1

python -m pytest --basetemp "$env:TEMP\meoclaw-pytest-$([guid]::NewGuid().ToString('N'))"
```

直接启动 Bridge：

```powershell
cd D:\myproject\MeoAgent1.1
$env:PYTHONPATH = "$PWD\src;$PWD\bridge"
pyruntime\Scripts\python.exe -m meoclaw_bridge.server
```

后端健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8931/api/health
```

Bridge 启动后，可以用 REST 和 WebSocket 直接验证后端，不依赖任何产品界面。

---

## 十三、从零实现时应该按什么顺序推进

不要一次把所有功能都接上。按可运行、可验证的纵向里程碑推进。

### M0：Bridge 走通骨架

实现：

- 健康检查；
- 创建 session；
- 提交空壳 task；
- WebSocket 收到 `node_start`、`final_answer`、`ui_done`；
- token 鉴权。

验收：

```text
一次请求可以开始，也可以明确结束
```

### M1：纯对话路径

实现：

- 供应商和模型配置；
- keyring；
- session 历史；
- 模型流式输出；
- WebSearch/WebFetch 与 Tavily Key 配置。

验收：

```text
没有工具、没有图，也能完成连续对话
```

### M2：只读工作区 Agent

实现：

- `RuntimeState`；
- 路径检查；
- FileRead、Grep；
- 最大模型轮数和输出上限。

验收：

```text
Agent 能读取工作区，但无法写文件和执行 shell
```

### M3：可写工具与计划

实现：

- FileWrite、FileEdit；
- TodoWrite、TodoUpdate；
- `TODO.md`；
- planner 的结构化计划。

验收：

```text
任务有明确待办，工具调用可观察
```

### M4：完整验证闭环

实现：

- acceptance criteria；
- verification commands；
- 只读 verifier；
- fail-closed 判定；
- 重试回 planner。

验收：

```text
模型说完成但测试失败时，任务仍然不通过
```

### M5：Context 与跨任务连续性

实现：

- `add_messages`；
- 消息上下文标签；
- ContextBuilder；
- SQLite checkpoint；
- 同 session 第二任务复用时间线。

验收：

```text
新任务能使用旧任务的关键结论
不同角色不会收到不合适的原始工具消息
```

### M6：Docker 执行策略

实现：

- sandbox 镜像；
- 工作区 bind mount；
- 无网络、资源限制、只读 rootfs；
- Docker 不可用时 fail-closed；
- 验收命令也进入同一执行边界。

验收：

```text
任何模型生成的 shell 都不会直接在宿主执行
```

### M7：系统工具与 HITL

实现：

- WebSearch/WebFetch、截图、剪贴板、打开 URL/文件；
- 敏感工具清单；
- 中断事件；
- 审批放行/拒绝；
- 一次性消费审批。

验收：

```text
敏感动作执行前一定暂停，拒绝后不会重试
```

### M8：安全与恢复加固

实现：

- session 路径验证；
- 删除边界；
- CORS；
- 审批恢复；
- 后台任务异常收尾；
- 重启后的中断任务处理。

验收：

```text
恶意路径、错误 token、进程重启都有确定行为
```

### M9：观测与发布

实现：

- 全量事件回归；
- CLI 退出码；
- 日志与错误分类；
- README、架构文档、后端指南同步；
- 分支和 tag 验证。

---

## 十四、用 grill、prototype、spec 和 review 完善后端想法

我喜欢先通过讨论完善想法，再在不确定处做原型。这个顺序适合后端，但每一步应有明确产物。

### 14.1 `/grill-me`：把模糊需求变成后端约束

每次只推进一个后端问题，回答后再追问下一层。

必须覆盖：

1. 用户最终能得到什么？
2. 哪条证据证明完成？
3. 谁会写状态，谁只能读？
4. 哪个动作有副作用？
5. 哪个动作必须批准？
6. 进程重启后哪些状态仍要存在？
7. 资源上限和退出条件是什么？
8. 失败时是重试、降级还是 fail-closed？
9. 兼容旧会话和旧 checkpoint 吗？
10. 怎样用假模型稳定测试？

推荐格式：

```text
/grill-me
我要重新设计 verifier 的命令执行边界。
先不要给实现方案。请逐轮追问：
命令在哪个环境执行、哪些事实必须保留、
Docker 不可用时任务如何结束、如何证明宿主 shell 没有被调用。
```

grill 的产物不是聊天记录，而是一份决策清单：

- 已确认；
- 已否决及原因；
- 未确定；
- 需要原型验证；
- 明确不做。

### 14.2 `/prototype`：只回答讨论不能判断的后端问题

后端原型只做逻辑状态实验，不实现产品界面。

适合原型的问题：

- “每条命令一个新容器”和“一次验证复用一个容器”哪个状态更清楚？
- 审批暂停、拒绝、恢复、重复提交的时序是否可靠？
- 历史压缩发生在任务结束、会话切换还是 token 超限时？
- 验收命令失败与 Docker 启动失败是否需要不同状态？

原型规则：

- 与真实模块放在相近位置；
- 明确命名成 prototype；
- 一条命令可运行；
- 默认不持久化；
- 每一步显示完整状态；
- 不写正式测试、错误恢复和抽象；
- 结束后只把验证过的决定带回正式代码。

对 MeoClaw 后端，一个单文件脚本或最小状态输出程序通常更合适。原型应回答“状态是否正确”，不需要模拟真实模型。

### 14.3 领域建模与 `GLOSSARY.md`

等关键术语稳定后再写术语表。`GLOSSARY.md` 只放定义，不放设计细节和代码。

MeoClaw 后端建议先统一：

| 术语 | 定义 |
| --- | --- |
| Session | 跨多个任务持续存在的会话语境 |
| Task | 一次后端执行请求 |
| Workspace | 一次会话唯一允许读写的工作目录 |
| Plan | 对当前 Task 的结构化执行方案 |
| Todo | Plan 中可独立更新状态的步骤 |
| Acceptance Criterion | 判断 Task 是否完成的可观察条件 |
| Verification Command | 提供客观执行证据的命令 |
| Verification Evidence | 命令结果、输出、退出码或工具结果 |
| WebSearchTool | 通过 Tavily 搜索公开网页并返回摘要与来源的只读工具 |
| WebFetchTool | 通过 Tavily Extract 读取指定公开网页正文的只读工具 |
| Tavily Key | 启用 WebSearch/WebFetch 的联网凭据，存放在环境变量或系统 keyring |
| Context Bundle | ContextBuilder 为某个角色生成的一次输入 |
| Checkpoint | LangGraph 保存的可恢复状态 |
| History Summary | 跨任务保留的压缩历史文本 |
| Fail-closed | 证据不足或安全组件不可用时拒绝放行 |
| HITL | 高危动作暂停等待人工决定 |

遇到模糊词要立即拆分，例如：

```text
你说的 memory 是 checkpoint、ContextBundle，还是 HISTORY_SUMMARY.md？
你说的 done 是 Agent 停止、图结束，还是 verifier 通过？
你说的 sandbox 是路径限制，还是操作系统级容器隔离？
```

### 14.4 `/to-spec`：把已讨论内容综合成规格

这个阶段不再采访，只整理已经确认的决定。

后端规格应包含：

1. Problem Statement；
2. Solution；
3. User Stories；
4. Implementation Decisions；
5. Testing Decisions；
6. Out of Scope；
7. Further Notes。

后端行为优先用不变量表达：

```text
所有验收命令必须在指定 sandbox 中执行。
Docker 不可用时任务不得通过验证。
每次任务只能访问自己的 workspace。
同一 session 的第二任务必须看到上一任务的消息。
```

规格不应依赖容易过期的具体行号，但可以写清模块边界、状态字段、事件语义和迁移方式。

### 14.5 `/to-tickets`：切纵向后端工单

工单应该穿过从状态到事件、执行和测试的完整路径，而不是横向切成“改文件 A、再改文件 B”。

一个后端工单至少包含：

- 用户或调用方可观察的行为；
- 涉及的接口或状态契约；
- 失败语义；
- 验收标准；
- blocker。

当前沿工单都完成后再开始下一层。

### 14.6 `/implement-spec`：先测试外部行为

实现顺序：

1. 写一个会失败的行为测试；
2. 实现最小路径；
3. 重构成清晰模块；
4. 补安全与回归；
5. 更新文档；
6. 合并后重新跑全量测试。

如果多个 Agent 并行实现，主要上下文应通过 spec、ticket、原型结论和提交引用传递，避免复制大段聊天。

### 14.7 `/code-review`：固定点上的双轴审查

先指定 fixed point，例如：

```text
/code-review ver2.5
```

后端重点看两条轴：

- **Standards**：命名、边界、重复、职责和可测试性；
- **Spec**：行为是否完整、是否有遗漏或范围膨胀。

后端还要额外检查安全不变量：

- 是否还有绕过工具边界的路径；
- 是否把所有模型生成内容都当成不可信输入；
- 是否在失败时错误地设置 `passed=true`；
- checkpoint 和内存状态是否被混为一谈；
- 测试是否真的证明了拒绝行为。

---

## 十五、完整案例：从需求到后端设计

需求：

```text
让 verifier 的验收命令也在 Docker 沙箱内运行。
```

### 15.1 先写问题和不变量

问题不是“execute_command 在哪一行”，而是：

```text
在这个案例开始前，actor 的 shell 在 Docker，
但 verifier 的验收命令在宿主 subprocess，
同一任务存在两条不同的命令执行边界。
```

目标不变量：

```text
任务中任何由模型生成的 shell 都不得直接在宿主执行。
Docker 不可用时，验证不得通过。
```

### 15.2 grill 问题

1. 每条验收命令起一个新容器，还是整个验证阶段复用？
2. 容器内临时目录的修改是否需要跨命令保留？
3. 工作区是否继续使用 bind mount？
4. Docker 不可用算任务失败还是执行错误？
5. 超时、取消和容器无法启动分别映射成什么结果？
6. 是否允许验收命令联网？
7. `stdout/stderr` 截断规则是否保持？
8. 调用方如何区分“测试失败”和“沙箱失败”？
9. 如何证明宿主 shell 没有被调用？
10. 如何在没有 Docker 的 CI 里测试策略对象？

### 15.3 是否做原型

如果“每命令一容器”和“整个 verifier 一容器”难以判断，可以做一个逻辑原型：

```text
创建容器
  → 执行命令 A
  → 写入临时文件
  → 执行命令 B
  → 观察临时文件是否仍在
  → 退出并显示最终状态
```

原型只回答生命周期问题，不接真实任务、不持久化、不追求完整 sandbox。

### 15.4 规格中的外部行为

```text
当任务执行验收命令时，命令必须在 Docker 中运行。
当 Docker 不可用时，任务必须 fail-closed。
每个 VerificationResult 仍包含 command、ok、exit_code、stdout、stderr。
默认禁止网络。
调用方能收到可识别的 sandbox failure 事件。
```

### 15.5 纵向工单

1. **建立可替换的命令执行接缝**
   允许现有宿主执行器与未来 Docker 执行器实现同一接口，补行为契约。
   Blocked by: None。

2. **一条验收命令在 Docker 中端到端运行**
   从图状态中的命令到 `verification_command` 事件完整跑通。
   Blocked by: 1。

3. **Docker 不可用时 fail-closed**
   不执行宿主命令，输出明确错误并使任务失败。
   Blocked by: 2。

4. **补安全回归与架构文档**
   证明宿主没有被调用，更新事件语义和安全说明。
   Blocked by: 2、3。

### 15.6 测试重点

- 假 Docker runner 能收到命令；
- 宿主 executor 在这条路径上绝不会被调用；
- Docker 不可用返回安全失败；
- 命令非零仍返回原始退出码；
- verifier 不会因执行器错误得到 `passed=true`；
- 事件顺序保持 `verification_command → verification → final_answer`。

### 15.7 code review 重点

- 是否还存在另一条直接宿主执行路径；
- 是否重复实现现有 Docker policy；
- 基础设施失败是否被误标为测试失败；
- 结果字段是否破坏现有协议；
- 文档是否仍声称两者使用同一沙箱。

---

## 十六、当前后端基线

这一节只描述后端真实状态，不把设计目标写成已实现功能。

### 已实现

- FastAPI REST + WebSocket Bridge；
- Bearer token 和 CORS 白名单；
- session 路径、归档/恢复和工作区路径检查；
- 单一 LangGraph Main Agent：直接回答与行动任务分支；
- `human / ai / full` 三档权限与独立 AI Reviewer；
- 文件写入、笔记追加、shell、剪贴板读取的人工审批；
- 归档会话只读、恢复后继续，API 不再提供永久删除；
- LangChain `create_agent` 内层循环；
- LangGraph planner → actor → verifier → final；
- 条件边、最大验证轮数、失败回填；
- fail-closed verifier；
- ContextBuilder；
- SQLite checkpoint；
- 消息跨任务连续性；
- final 节点自动写入有界历史摘要，并由 ContextBuilder 在后续任务中读取；
- Docker actor shell；
- verifier 命令与 actor 共用 Docker 执行策略；
- LangChain middleware 工具错误、预算、搜索重试、摘要和权限决策；
- WebSearch/WebFetch 全局只读工具与 Tavily 一键配置；
- supervisor、searchAgent、codeAgent 代码路径；
- Fake Model 后端回归测试。

### 仍需明确或补齐

| 优先级 | 问题 | 影响 |
| --- | --- | --- |
| P1 | 人工审批不能跨 Bridge 进程重启恢复 | 恢复语义不完整 |
| P2 | 复杂 supervisor 图没有默认选择规则 | 能力存在但不能稳定触发 |
| P2 | 同 session 并发任务缺少写入串行协议 | 工作区可能竞争 |
| P2 | 事件字段缺少共享 schema | Bridge 与调用方容易漂移 |
| P2 | 空验收命令的语义不明确 | 轻量任务可能过宽通过 |

这些项目应作为下一轮 grill 的入口，而不是直接写成待办清单。

---

## 十七、后端需求模板

下一次开始新后端设计时，可以直接使用下面的模板。

```text
后端目标：
……

用户或调用方可观察的成功：
当……时，应得到……

必须保持的不变量：
1. ……
2. ……

绝不能发生：
1. ……
2. ……

权限与能力：
- human：
- ai：
- full：

状态所有权：
- 谁创建：
- 谁更新：
- 谁只读：
- 哪些进入 checkpoint：
- 哪些只存在内存：

失败语义：
- 输入错误：
- 配置错误：
- 工具失败：
- 验证失败：
- 安全拒绝：
- 基础设施失败：

资源上限：
- 模型轮数：
- 工具轮数：
- 验证轮数：
- 命令超时：
- 输出上限：

测试接缝：
- 工作流：
- Agent：
- 工具：
- Bridge：

明确不做：
……
```

然后按顺序推进：

```text
1. /grill-me
   确定不变量、所有权、失败和边界

2. /prototype
   只验证无法通过讨论判断的后端状态与执行策略

3. domain-modeling
   统一术语并更新 GLOSSARY.md

4. /to-spec
   综合已经确认的内容，不再新增采访

5. /to-tickets
   切成带 blocker 的纵向后端工单

6. /implement-spec
   使用 TDD 按任务图实现

7. /code-review <fixed-point>
   分开审查 Standards 与 Spec

8. 全量测试、文档同步、提交、打 tag
```

---

## 十八、后端完成的判断标准

后端改动是否完成，应由下面这些问题判断：

1. 调用方能否明确知道任务开始、执行、暂停、结束？
2. 成功是否有客观证据？
3. 失败是否 fail-closed？
4. 状态是否有唯一所有权？
5. 模型生成的路径和命令是否按不可信输入处理？
6. 每个循环是否有硬上限？
7. 进程重启后哪些状态还在，是否说得清？
8. 工具失败和基础设施失败是否分开？
9. 测试是否能脱离真实 API Key 稳定复现？
10. spec、术语、实现、事件和文档是否表达同一件事？

所以构思 MeoClaw 后端时，固定推导链应该是：

```text
服务承诺
  → 能力模式
  → API 与事件契约
  → 状态模型
  → 外层工作流
  → 内层 Agent
  → 上下文与记忆
  → 持久化
  → 安全边界
  → 失败模型
  → 测试
  → 发布
```

沿着这条链设计，再框架实现；先确定后端必须遵守什么准则，再决定用哪些 LangChain、LangGraph 或 Docker 能力。接着再去思考我们的runtimestate，以及图状态里到底该有哪些状态量，我要实现某个功能我需要哪些状态量。构思好状态之后再画langgraph节点图，搭建草稿流程，再一步步优化。先从核心的画起，一个基本的循环画好，再把复杂的功能节点一步步往上填，就能得出我们最终的项目构图。接着便是借用matt的skill思想，我们grill-me，再采用最小prototype开发原型并微调，接着to-spec写glossary，spec，readme等等的文档，然后,to-ticket,implement-spec应用文档内容，借助AI辅助开发代码实现，并采用测试集进行测试，最后出锅~
