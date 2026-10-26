export interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

export type StepStatus = "running" | "success" | "error" | "info";

export interface Step {
  id: string;
  kind: string;
  text: string;
  detail?: string;
  status?: StepStatus;
}

export interface Artifact {
  path: string;
  operation: "created" | "modified";
}

export interface PendingApproval {
  id: string;
  tool: string;
  detail: string;
}

export const MODE_LABELS: Record<string, string> = {
  chat: "仅对话",
  workspace: "工作区任务",
  system: "系统工具",
};

export const MODE_HINTS: Record<string, string> = {
  chat: "只使用模型，不访问工作区或系统工具",
  workspace: "在隔离会话工作区内读取、修改和验证文件",
  system: "允许调用需要审批的桌面系统工具",
};
