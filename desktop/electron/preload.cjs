// 沙箱化预加载只能使用 Electron API；文件访问由主进程代为完成。
const { contextBridge, ipcRenderer } = require("electron");

const runtime = ipcRenderer.sendSync("meoclaw:get-runtime");
contextBridge.exposeInMainWorld("meoclaw", runtime);
