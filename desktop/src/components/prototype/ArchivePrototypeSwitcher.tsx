import { useEffect } from "react";
import type { ArchivePrototypeVariant } from "./ArchiveSessionPrototype";

const VARIANTS: ArchivePrototypeVariant[] = ["A", "B", "C"];
const LABELS: Record<ArchivePrototypeVariant, string> = {
  A: "内联归档",
  B: "浮层归档",
  C: "整栏切换",
};

export function getArchivePrototypeVariant(): ArchivePrototypeVariant | null {
  if (!import.meta.env.DEV) return null;
  const params = new URLSearchParams(window.location.search);
  if (params.get("prototype") !== "archive") return null;
  const requested = params.get("variant")?.toUpperCase();
  return VARIANTS.includes(requested as ArchivePrototypeVariant)
    ? (requested as ArchivePrototypeVariant)
    : "A";
}

export default function ArchivePrototypeSwitcher({
  current,
}: {
  current: ArchivePrototypeVariant;
}) {
  function show(next: ArchivePrototypeVariant) {
    const url = new URL(window.location.href);
    url.searchParams.set("prototype", "archive");
    url.searchParams.set("variant", next);
    window.location.assign(url);
  }

  function cycle(direction: number) {
    const index = VARIANTS.indexOf(current);
    const next = VARIANTS[(index + direction + VARIANTS.length) % VARIANTS.length];
    show(next);
  }

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (
        target?.matches("input, textarea, select, [contenteditable='true']")
      ) {
        return;
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        cycle(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        cycle(1);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  return (
    <div className="prototype-switcher" aria-label="归档原型变体切换">
      <button onClick={() => cycle(-1)} aria-label="上一个变体">
        ←
      </button>
      <span>
        {current} · {LABELS[current]}
      </span>
      <button onClick={() => cycle(1)} aria-label="下一个变体">
        →
      </button>
    </div>
  );
}
