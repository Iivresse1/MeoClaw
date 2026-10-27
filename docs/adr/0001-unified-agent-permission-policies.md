# 单一主 Agent 与三档权限策略

**Status**: accepted

MeoClaw 不再用 `chat / workspace / system` 表示三种 Agent。所有请求进入同一个
LangGraph 入口和同一个 Main Agent；内部仍可保留直接回答、planner、actor、
verifier 等阶段。工具能力与审批策略分离，用户只选择人工审核、AI 自动审核或
完全权限。

人工审核逐次确认工作区文件写入、`NotepadAppendTool`、shell 和剪贴板读取；
截图、打开 URL、写入剪贴板直接执行。`OpenWorkspaceFile` 对已知文档类型直接
打开，对可执行文件和未知类型要求人工确认。内部 `TODO.md` 状态持久化不属于
用户文件写入，不触发审批。

AI 自动审核先经过确定性硬规则，再由与主 Agent 上下文隔离的 Reviewer 判断
shell 安全性和文件修改程度。普通构建、测试、单文件编辑可通过；破坏性命令、
凭据访问、沙箱逃逸、大范围修改或任何不确定结果升级人工。所有策略继续执行
路径、sandbox、凭据和 verifier 的硬安全边界。

## Considered Options

- 保留三种 Agent 再叠加三种权限，会产生九种行为组合，难以解释和测试。
- 让主 Agent 自审工具调用，会混淆动作产生者和审批者。
- 完全权限允许宿主 root 操作，会推翻现有工作区和 Docker 安全模型。
