# MeoClaw

MeoClaw is a workspace-oriented desktop agent whose execution capability is
separate from the approval policy applied to that capability.

## Agent

**Main Agent**:
The single user-facing MeoClaw agent. Conversation, workspace work, and system
tools are tasks handled by this agent rather than separate agent identities.
_Avoid_: chat agent, workspace agent, system agent

**AI Reviewer**:
An isolated reviewer that decides whether a proposed sensitive tool call may
proceed without human approval.
_Avoid_: self-review, main-agent approval

## Permissions

**Permission Policy**:
The rule set that decides whether a tool call executes directly, requires
review, or is denied. It is independent of the Main Agent.
_Avoid_: mode, agent type

**Human Review**:
A permission policy that pauses sensitive tool calls until the user approves or
rejects them.
_Avoid_: manual mode, approval mode

**AI Review**:
A permission policy that sends sensitive tool calls to the AI Reviewer before
execution. Uncertain or failed reviews escalate to human review.
_Avoid_: auto mode, self-approval

**Full Access**:
A permission policy that executes permitted tools without approval while still
enforcing every Hard Safety Boundary.
_Avoid_: root access, unrestricted mode

**Hard Safety Boundary**:
A non-bypassable rule that protects workspace, credentials, sandbox integrity,
and verification truthfulness regardless of Permission Policy.
_Avoid_: approval rule, soft guard

## Sessions

**Archived Session**:
A session removed from active work but whose history remains available for
viewing. It must be restored before new work can continue, and the application
does not provide permanent deletion.
_Avoid_: deleted session, removed session
