import type { PendingApproval } from "../App";

interface Props {
  approval: PendingApproval;
  onResolve: (decision: boolean) => void;
}

export default function ApprovalCard({ approval, onResolve }: Props) {
  return (
    <div className="approval-card">
      <div className="approval-title">需要你的批准</div>
      <div className="approval-detail">
        <b>{approval.tool}</b>
        <span>{approval.detail}</span>
      </div>
      <div className="approval-actions">
        <button className="btn-dark" onClick={() => onResolve(true)}>
          允许
        </button>
        <button className="btn-ghost" onClick={() => onResolve(false)}>
          拒绝
        </button>
      </div>
    </div>
  );
}
