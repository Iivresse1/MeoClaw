# MeoClaw 1.1 Architecture

## Goals

Version 1.1 moves the agent execution loop and cross-cutting policies to
LangChain 1.x while preserving MeoClaw's differentiation:

- an explicit planner, implementation agent, read-only verifier and final node;
- fail-closed verification based on commands plus model inspection;
- one canonical message timeline shared across nodes and tasks;
- one Main Agent with conversation and action routes;
- Tavily web search and page fetching available to the Main Agent and role agents;
- explicit human, AI-review, and full-access permission policies;
- persistent checkpoints and blocking human approval requests;
- Docker-isolated shell execution.

## Runtime Topology

```text
React/Electron
  -> authenticated REST + WebSocket Bridge
  -> Tavily WebSearch / WebFetch (API key in keyring)
  -> LangGraph Main Agent entry
       classifier
         -> direct responder (read-only web)
         -> planner
         -> implementation create_agent
         -> read-only verifier create_agent
       verifier failed -> planner
       passed or attempts exhausted -> final
  -> SQLite checkpoint/message store
```

The Main Agent graph owns routing and task boundaries. `create_agent`
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
| `ToolPermissionMiddleware` | Applies human, AI-review, or full-access policy before sensitive calls |

Infrastructure failures are not swallowed. The stack replaces only
tool-execution exception handling.

## Web Access

`WebSearchTool` and `WebFetchTool` are read-only tools registered globally for
the Main Agent, verifier, codeAgent and searchAgent loops. The Tavily
key is resolved from `TAVILY_API_KEY` first and from the OS keyring second.
The settings API validates a candidate key before storing it; keys are never
returned by the status endpoint or written to settings JSON.

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

Sensitive calls block only the background task while the Bridge waits for an
approval decision. AI review uses a separate model invocation and escalates
uncertain calls to human review.

## Security Boundaries

- Bridge REST and WebSocket requests require a per-machine capability token.
- CORS is restricted to development and packaged Electron origins; the token is
  still mandatory.
- `session_id` must be a generated 12-character hex ID.
- Sessions can only be archived or restored. Archived sessions keep their
  workspace history and cannot start new tasks until restored.
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

Verifier commands use the same Docker policy as the actor shell, but each
command runs in a one-shot container so a model-generated check never falls
back to a host subprocess.

Set `MEOCLAW_SANDBOX_NETWORK=1` only for an explicitly approved task that needs
network access.

## History

The obsolete 1.0 manual context monitor/compressor has been removed. Version 1.1
uses `SummarizationMiddleware` for in-agent context compression and
`ContextBuilder` for role-specific context projection. When a task reaches the
`final` node, the runtime appends a bounded, deterministic task summary to
`HISTORY_SUMMARY.md`; later tasks and process restarts read it back through
`ContextBuilder`.
