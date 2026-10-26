import { useEffect, useState } from "react";
import type { Step } from "../types";

const STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  success: "完成",
  error: "失败",
  info: "信息",
};

export default function ExecutionDetails({
  steps,
  running,
}: {
  steps: Step[];
  running: boolean;
}) {
  const [open, setOpen] = useState(running);

  useEffect(() => {
    setOpen(running);
  }, [running]);

  if (!steps.length) return null;

  const failed = steps.filter((step) => step.status === "error").length;
  const status = running ? "运行中" : failed ? `${failed} 项失败` : "已完成";

  return (
    <details
      className="execution-details"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className={`execution-marker ${running ? "is-running" : failed ? "is-error" : "is-done"}`}>
          <span />
        </span>
        <span className="execution-summary">
          <strong>执行过程</strong>
          <span>
            {steps.length} 个步骤 · {status}
          </span>
        </span>
        <span className="execution-chevron" aria-hidden="true">
          ⌄
        </span>
      </summary>
      <ol className="execution-list">
        {steps.map((step) => (
          <li key={step.id} className={`execution-step status-${step.status || "info"}`}>
            <span className="step-dot" aria-hidden="true" />
            <div className="step-content">
              <div className="step-title">
                <span className="step-kind">{step.kind}</span>
                <span className="step-name">{step.text}</span>
                <span className="step-status">{STATUS_LABEL[step.status || "info"]}</span>
              </div>
              {step.detail && <pre className="step-detail">{step.detail}</pre>}
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}
