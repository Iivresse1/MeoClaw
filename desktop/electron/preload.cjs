// 预加载脚本：仅暴露桥接端口，渲染层以 http://127.0.0.1:<port> 访问
const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("meoclaw", {
  bridgePort: Number(process.env.MEOCLAW_BRIDGE_PORT || 8931),
});
