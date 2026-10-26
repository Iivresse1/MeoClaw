import { useEffect, useState } from "react";
import ArtifactList from "../components/ArtifactList";
import ExecutionDetails from "../components/ExecutionDetails";
import MarkdownMessage from "../components/MarkdownMessage";
import type { Artifact, Step } from "../types";
import "./prototype.css";

const VARIANTS = [
  { key: "A", label: "Reader" },
  { key: "B", label: "Inspector" },
  { key: "C", label: "Command center" },
] as const;

const MOCK_ANSWER = `## 已完成

已浏览并核对桌面的 \`chatpet\` 文件夹。当前目录为空，没有发现代码、图片或配置文件。

### 结果要点

- 目标路径：\`C:\\Users\\i\\Desktop\\chatpet\`
- 目录状态：存在、可访问、当前为空
- 检查方式：只读遍历，没有创建、修改或删除任何内容
- 验收结论：路径检查、内容检查和只读边界检查全部通过

### 建议

如果准备在这个目录中开始项目，可以先确定技术栈，再初始化 README、依赖清单和第一版目录结构。`;

const MOCK_STEPS: Step[] = [
  { id: "p1", kind: "计划", text: "确认路径、检查内容并验证只读边界", status: "success" },
  {
    id: "p2",
    kind: "工具",
    text: "Get-ChildItem",
    status: "success",
    detail: "Path: C:\\Users\\i\\Desktop\\chatpet\nMode: ReadOnly\nFiles: 0\nDirectories: 0",
  },
  {
    id: "p3",
    kind: "验收命令",
    text: "verify_chatpet.ps1",
    status: "success",
    detail: "PATH=EXISTS\nFILES=0\nDIRS=0\nRESULT=PASS",
  },
  { id: "p4", kind: "验收结论", text: "全部检查通过", status: "success" },
  { id: "p5", kind: "任务结束", text: "任务已完成", status: "success" },
];

const MOCK_ARTIFACTS: Artifact[] = [
  { path: "chatpet_overview.md", operation: "created" },
  { path: "verify_chatpet.ps1", operation: "created" },
];

const SESSIONS = ["浏览桌面的 chatpet 文件夹", "帮我查询伦敦时间", "你好"];

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`pv-brand${compact ? " compact" : ""}`}>
      <span>M</span>
      {!compact && <strong>MeoClaw</strong>}
    </div>
  );
}

function UserPrompt({ dark = false }: { dark?: boolean }) {
  return (
    <div className={`pv-user${dark ? " dark" : ""}`}>
      浏览一下桌面的 chatpet 文件夹，确认当前内容并留下检查记录。
    </div>
  );
}

function Answer({ compact = false }: { compact?: boolean }) {
  return (
    <article className={`pv-answer${compact ? " compact" : ""}`}>
      <header>
        <Brand compact />
        <div>
          <strong>MeoClaw</strong>
          <span>已完成 · 8.4 秒</span>
        </div>
        <button type="button">复制</button>
      </header>
      <MarkdownMessage content={MOCK_ANSWER} />
    </article>
  );
}

function VariantA() {
  return (
    <div className="pv pv-a">
      <aside className="pv-a-sidebar">
        <header>
          <Brand />
          <span>LOCAL AGENT</span>
        </header>
        <button className="pv-new">＋ 新任务</button>
        <p>最近会话</p>
        <nav>
          {SESSIONS.map((session, index) => (
            <button className={index === 0 ? "active" : ""} key={session}>
              <span />
              {session}
            </button>
          ))}
        </nav>
        <button className="pv-settings">⚙ 设置</button>
      </aside>
      <main className="pv-a-main">
        <header className="pv-a-topbar">
          <span><i /> 已完成 <small>8.4 s</small></span>
          <b>工作区任务</b>
        </header>
        <section className="pv-a-content">
          <UserPrompt />
          <Answer />
          <ExecutionDetails steps={MOCK_STEPS} running={false} />
          <ArtifactList artifacts={MOCK_ARTIFACTS} />
        </section>
        <div className="pv-composer">
          <span>继续追问，或让 MeoClaw 接着处理…</span>
          <button>↑</button>
        </div>
      </main>
    </div>
  );
}

function VariantB() {
  return (
    <div className="pv pv-b">
      <aside className="pv-b-rail">
        <Brand compact />
        <button className="active">◫</button>
        <button>＋</button>
        <span />
        <button>⚙</button>
      </aside>
      <main className="pv-b-chat">
        <header>
          <span>chatpet / 目录检查</span>
          <span className="pv-chip">工作区任务</span>
        </header>
        <section>
          <UserPrompt />
          <Answer compact />
        </section>
        <div className="pv-b-composer">
          <span>继续输入…</span>
          <button>↑</button>
        </div>
      </main>
      <aside className="pv-inspector">
        <header>
          <strong>运行检查器</strong>
          <span>COMPLETE</span>
        </header>
        <div className="pv-run-summary">
          <div><strong>5</strong><span>步骤</span></div>
          <div><strong>3</strong><span>命令</span></div>
          <div><strong>0</strong><span>错误</span></div>
        </div>
        <section>
          <h3>活动时间线</h3>
          <ol className="pv-timeline">
            {MOCK_STEPS.map((step) => (
              <li key={step.id}>
                <span />
                <div>
                  <small>{step.kind}</small>
                  <strong>{step.text}</strong>
                </div>
              </li>
            ))}
          </ol>
        </section>
        <details className="pv-command-details">
          <summary>命令详情 <span>⌄</span></summary>
          <pre>{`PowerShell -NoProfile -File verify_chatpet.ps1
→ PATH=EXISTS
→ FILES=0
→ DIRS=0
→ RESULT=PASS`}</pre>
        </details>
        <section className="pv-files">
          <h3>产出文件</h3>
          {MOCK_ARTIFACTS.map((file) => (
            <button key={file.path}>
              <span>{file.path.endsWith(".md") ? "MD" : "PS"}</span>
              <strong>{file.path}</strong>
              <small>{file.operation === "created" ? "已创建" : "已修改"}</small>
            </button>
          ))}
        </section>
      </aside>
    </div>
  );
}

function VariantC() {
  return (
    <div className="pv pv-c">
      <header className="pv-c-header">
        <Brand />
        <div>
          <span className="active">任务</span>
          <span>历史</span>
          <span>设置</span>
        </div>
        <button>新任务＋</button>
      </header>
      <main className="pv-c-grid">
        <section className="pv-c-activity">
          <div className="pv-c-status">
            <span>RUN 07F2</span>
            <strong>任务完成</strong>
            <p>路径、内容和只读边界均已验证。</p>
          </div>
          <div className="pv-c-timeline">
            {MOCK_STEPS.map((step, index) => (
              <article key={step.id}>
                <span>{index + 1}</span>
                <div>
                  <small>{step.kind}</small>
                  <strong>{step.text}</strong>
                </div>
              </article>
            ))}
          </div>
          <details className="pv-c-log">
            <summary>查看执行命令与原始输出</summary>
            <pre>{`> Get-ChildItem -LiteralPath C:\\Users\\i\\Desktop\\chatpet -Force
PATH=EXISTS
FILES=0
DIRS=0

> verify_chatpet.ps1
RESULT=PASS`}</pre>
          </details>
        </section>
        <section className="pv-c-result">
          <div className="pv-c-task">
            <span>你</span>
            <p>浏览一下桌面的 chatpet 文件夹，确认当前内容并留下检查记录。</p>
          </div>
          <Answer />
          <footer>
            <h3>交付物</h3>
            <div>
              {MOCK_ARTIFACTS.map((file) => (
                <button key={file.path}>
                  <span>{file.path.endsWith(".md") ? "MD" : "PS"}</span>
                  <div>
                    <strong>{file.path}</strong>
                    <small>工作区根目录</small>
                  </div>
                  <b>↗</b>
                </button>
              ))}
            </div>
          </footer>
        </section>
      </main>
      <div className="pv-c-compose">
        <span>继续输入，或选择一个产出文件查看…</span>
        <button>发送 ↑</button>
      </div>
    </div>
  );
}

function PrototypeSwitcher({
  current,
  onChange,
}: {
  current: string;
  onChange: (key: string) => void;
}) {
  const index = VARIANTS.findIndex((item) => item.key === current);

  function move(delta: number) {
    const next = VARIANTS[(index + delta + VARIANTS.length) % VARIANTS.length];
    onChange(next.key);
  }

  return (
    <div className="proto-switcher" role="navigation" aria-label="UI prototype variants">
      <button onClick={() => move(-1)} aria-label="上一个方案">←</button>
      <span>
        <b>{current}</b> · {VARIANTS[index]?.label}
      </span>
      <button onClick={() => move(1)} aria-label="下一个方案">→</button>
    </div>
  );
}

export default function UIPrototype() {
  const initial = new URLSearchParams(window.location.search).get("variant")?.toUpperCase();
  const [variant, setVariant] = useState(
    VARIANTS.some((item) => item.key === initial) ? initial! : "A"
  );

  function selectVariant(key: string) {
    setVariant(key);
    const url = new URL(window.location.href);
    url.searchParams.set("variant", key);
    window.history.replaceState(null, "", url);
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (
        target?.matches("input, textarea, select, [contenteditable='true']")
      ) {
        return;
      }
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const index = VARIANTS.findIndex((item) => item.key === variant);
      const delta = event.key === "ArrowRight" ? 1 : -1;
      selectVariant(VARIANTS[(index + delta + VARIANTS.length) % VARIANTS.length].key);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [variant]);

  return (
    <div className="prototype-root">
      <div className="proto-flag">UI PROTOTYPE · READ ONLY · MOCK DATA</div>
      {variant === "A" && <VariantA />}
      {variant === "B" && <VariantB />}
      {variant === "C" && <VariantC />}
      <PrototypeSwitcher current={variant} onChange={selectVariant} />
    </div>
  );
}
