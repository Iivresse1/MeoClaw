// Electron 主进程：启动 pyruntime 桥接子进程 + 渲染窗口（deepseek-harness 式壳层）
const { app, BrowserWindow, Menu } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");

const ROOT = path.resolve(__dirname, "..", ".."); // D:\MeoClaw-Desktop
const PY = path.join(ROOT, "pyruntime", "Scripts", "python.exe");
const PORT = process.env.MEOCLAW_BRIDGE_PORT || "8931";
let pyProc = null;

function startBridge() {
  pyProc = spawn(PY, ["-m", "meoclaw_bridge.server"], {
    cwd: path.join(ROOT, "bridge"),
    env: { ...process.env, MEOCLAW_BRIDGE_PORT: PORT, PYTHONPATH: path.join(ROOT, "bridge") },
    stdio: "ignore",
    windowsHide: true,
  });
  pyProc.on("exit", () => { pyProc = null; });
}

function healthOk() {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${PORT}/api/health`, (res) => resolve(res.statusCode === 200));
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => { req.destroy(); resolve(false); });
  });
}

async function ensureBridge() {
  if (await healthOk()) return; // 已有健康服务则直接复用
  startBridge();
  for (let i = 0; i < 60; i++) {
    if (await healthOk()) return;
    await new Promise((r) => setTimeout(r, 500));
  }
  console.error("桥接服务启动超时");
}

async function createWindow() {
  const win = new BrowserWindow({
    width: 1180,
    height: 760,
    minWidth: 860,
    minHeight: 560,
    backgroundColor: "#ffffff",
    title: "MeoClaw",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  Menu.setApplicationMenu(null); // 无「应用/编辑」菜单

  if (!app.isPackaged) {
    // 开发模式：加载 vite dev server，未就绪时自动重试
    const tryLoad = (left) => {
      win.loadURL("http://localhost:5173").catch(() => {
        if (left > 0) setTimeout(() => tryLoad(left - 1), 800);
      });
    };
    tryLoad(30);
  } else {
    await win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

app.whenReady().then(async () => {
  await ensureBridge();
  await createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  if (pyProc) { try { pyProc.kill(); } catch (e) { /* 忽略 */ } }
});
