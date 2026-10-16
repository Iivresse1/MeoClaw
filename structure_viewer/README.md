# 架构可视化（structure_viewer）

自包含交互式架构图（archify 生成，双击或浏览器打开即可，无需构建/依赖）。

## 阅读路径

从 [p1-module.html](p1-module.html) 进入：点击任一模块 → 语义护照面板 →「进入函数与代码层级 →」下钻到该模块的函数级全量图。每一层节点点击后护照内均带源码锚点，可直接跳转到本仓库对应提交（`cca916f`）的文件行。

| 文件 | 内容 |
| --- | --- |
| p1-module.html | 模块级架构总览（下钻入口） |
| p2-functions.html | 函数级总览 |
| p3-code-nodes-full.html | graph/nodes.py 全量 42 符号调用图 |
| p3-code-agents.html | agents 专家模块内部结构 |
| p4-cli.html | cli/app.py |
| p4-runtime.html | core（事件流/ReAct/上下文/路径） |
| p4-tools-workspace.html | tools：注册表/待办/便签 |
| p4-tools-exec.html | tools：命令/文件/检索/网络 |
| p4-memory.html | graph/memory.py 三层记忆 |
| p4-workflow.html | graph/workflow.py + state.py 图装配 |
| p4-providers.html | providers 模型接入 |

## 在线预览

- **GitHub Pages**：仓库 Settings → Pages → Deploy from branch → `main` /(root)，保存后访问
  `https://<你的用户名>.github.io/MeoClaw/structure_viewer/p1-module.html`
- **免配置**：`https://htmlpreview.github.io/?https://github.com/Iivresse1/MeoClaw/blob/main/structure_viewer/p1-module.html`
