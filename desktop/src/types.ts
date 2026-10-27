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

export const PERMISSION_LABELS: Record<string, string> = {
  human: "人工审核",
  ai: "AI 自动审核",
  full: "完全权限",
};

export const PERMISSION_HINTS: Record<string, string> = {
  human: "文件写入、shell 和敏感系统操作需要逐次人工确认",
  ai: "由独立 AI Reviewer 审核敏感操作，不确定时转人工",
  full: "工具不再请求审批，但仍执行路径、凭据和 Docker 安全边界",
};
