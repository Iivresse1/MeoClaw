import { useState } from "react";
import type { Artifact } from "../types";

function fileExtension(path: string): string {
  const name = path.split(/[\\/]/).pop() || path;
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toUpperCase() : "FILE";
}

function fileName(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

export default function ArtifactList({ artifacts }: { artifacts: Artifact[] }) {
  const [copied, setCopied] = useState<string | null>(null);
  if (!artifacts.length) return null;

  async function copyPath(path: string) {
    try {
      await navigator.clipboard.writeText(path);
      setCopied(path);
      window.setTimeout(() => setCopied(null), 1400);
    } catch {
      setCopied(null);
    }
  }

  return (
    <section className="artifact-panel">
      <div className="artifact-heading">
        <span>产出文件</span>
        <span>{artifacts.length}</span>
      </div>
      <div className="artifact-list">
        {artifacts.map((artifact) => (
          <button
            key={`${artifact.operation}:${artifact.path}`}
            className="artifact-row"
            onClick={() => copyPath(artifact.path)}
            title="复制相对路径"
          >
            <span className="file-mark">{fileExtension(artifact.path)}</span>
            <span className="artifact-meta">
              <strong>{fileName(artifact.path)}</strong>
              <span>
                {artifact.operation === "created" ? "已创建" : "已修改"} · {artifact.path}
              </span>
            </span>
            <span className="artifact-action">{copied === artifact.path ? "已复制" : "复制路径"}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
