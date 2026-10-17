import { useState } from "react";
import type { Step } from "../App";

export default function ProgressCard({ steps }: { steps: Step[] }) {
  const [open, setOpen] = useState(true);
  if (!steps.length) return null;
  return (
    <div className="progress-card">
      <button className="progress-head" onClick={() => setOpen((v) => !v)}>
        <span>执行过程</span>
        <span className="chev">{open ? "⌄" : "›"}</span>
      </button>
      {open && (
        <ul className="progress-list">
          {steps.map((s, i) => (
            <li key={i}>
              <span className="kind">{s.kind}</span>
              <span className="text">{s.text}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
