# MeoClaw 1.1 Architecture

## Goals

Version 1.1 moves the agent execution loop and cross-cutting policies to
LangChain 1.x while preserving MeoClaw's differentiation:

- an explicit planner, implementation agent, read-only verifier and final node;
- fail-closed verification based on commands plus model inspection;
- one canonical message timeline shared across nodes and tasks;
- persistent checkpoints and resumable human approvals through SQLite;
- Docker-isolated shell execution.

## Runtime Topology

```text
React/Electron
  -> authenticated REST + WebSocket Bridge
  -> LangGraph role graph
       planner
         -> implementation create_agent
         -> read-only verifier create_agent
       verifier failed -> planner
       passed or attempts exhausted -> final
  -> SQLite checkpoint/message store
```

The role graph owns task boundaries and fail-closed semantics. `create_agent`
owns each model-tool loop. The Bridge maps native LangChain events to the
existing WebSocket event contract.

## Middleware Stack

| Middleware | Responsibility |
| --- | --- |
| `JsonToolResultMiddleware` | Serializes successful tool results before returning them to the model |
| `ToolErrorMiddleware` | Returns selected tool execution errors as error `ToolMessage`s |
| `ToolRetryMiddleware` | Retries only transient `WebSearchTool` failures |
| `ModelCallLimitMiddleware` | Enforces the ReAct model-call budget |
| `ToolCallLimitMiddleware` | Enforces the tool-call budget |
| `SummarizationMiddleware` | Compresses long in-agent histories at a token threshold |
| `HumanInTheLoopMiddleware` | Suspends sensitive system-tool calls until approval |

Infrastructure failures are not swallowed. The stack replaces only
tool-execution exception handling.

## ContextBuilder

`ContextBuilder` compiles one canonical conversation timeline into role-specific
model input:

- planner: previous conversation text, task state, plan, failure and research;
- actor: complete current-task AI/tool message pairs plus typed state;
- verifier: task state and command evidence without raw actor exploration;
- final: verification evidence and actor output summary.

The canonical `messages` channel uses LangGraph's `add_messages` reducer.
Messages are tagged with `context_task_id` and `context_roles`.

## Persistence and Resume

`langgraph-checkpoint-sqlite` stores graph state in
`%APPDATA%\MeoClawDesktop\checkpoints.sqlite`. A session uses a stable
`thread_id`, so later tasks continue the same timeline.

`HumanInTheLoopMiddleware` interrupts system-tool calls. UI decisions resume the
graph with `Command(resume=...)`; approvals no longer block on a custom
`threading.Event`.

## Security Boundaries

- Bridge REST and WebSocket requests require a per-machine capability token.
- CORS is restricted to development and packaged Electron origins; the token is
  still mandatory.
- `session_id` must be a generated 12-character hex ID.
- Session deletion validates paths below the sessions root and only deletes
  sessions existing in SQLite.
- Workspace shell execution requires Docker. Without Docker it fails closed
  instead of silently executing on the host.

## Docker Sandbox

Build the image:

```powershell
.\scripts\build-sandbox.ps1
```

Runtime defaults:

- network disabled;
- two CPUs and 2 GiB memory;
- read-only container root filesystem;
- only the session workspace is a persistent writable mount;
- container removed after the shell session exits.

Set `MEOCLAW_SANDBOX_NETWORK=1` only for an explicitly approved task that needs
network access.

## History

The obsolete 1.0 manual context monitor/compressor has been removed. Version 1.1
uses `SummarizationMiddleware` for in-agent context compression and
`ContextBuilder` for role-specific context projection.
