# Paper Reader · 论文阅读器

左论文、右 AI 的双栏阅读器：本地 PDF / arXiv 检索 → 中英对照阅读 → 选中即问。

**启动只要一条命令**，环境检测、依赖安装、大模型探测、开浏览器全部由内置 agent 完成：

```bash
python3 agent/boot.py
```

不需要手动建虚拟环境、不需要 `pip install`、不需要改配置、不需要第二条命令。
`agent` 会按顺序做四件事：

| 步骤 | 做什么 | 失败时 |
| --- | --- | --- |
| 1 | 找到/创建 `.venv` 并切换解释器 | 打印手动创建命令 |
| 2 | 检查 `requirements.txt`，缺什么装什么（依次尝试阿里云 → 清华 → 官方源） | 报错并指出缺哪个包 |
| 3 | **真连一次**大模型接口来探测可用配置，写入 `data/settings.json` | 不阻断启动，阅读/检索仍可用，界面里再配 |
| 4 | 选空闲端口启动服务并打开浏览器 | — |

常用参数：`--port 8765`、`--no-browser`、`--check`（只检测不启动）、`--recheck-api`。

---

## 功能

**阅读**
- 打开本地 PDF（选择文件 / 拖拽 / 粘贴绝对路径）
- 按标题、关键词、作者检索 **arXiv** 与 **Semantic Scholar**，一键下载入库
- 服务端渲染页面为图片，叠加逐词对齐的透明文本层 —— 可以像原生 PDF 一样**拖动选中**文字
- 翻页（按钮 / 页码输入 / ← → / PageUp PageDown）、缩放、适应宽度
- 页面尺寸在打开时一次性取回，整篇布局稳定，翻页不跳动
- 扫描件（无文本层）会被识别并提示，不会假装能翻译

**翻译**
- 「原文 / 中文翻译」一键切换
- 按**段落**为单位翻译，页内并发，结果流式回填，已读页面自动翻译（可关）
- 译文以白底块覆盖在原位置，原页淡化为底图，图表位置仍可见
- 译文本身也可以选中、复制、提问
- 翻译结果按内容哈希缓存到 `data/papers/<id>/translation.json`，重复阅读零成本
- 「翻译全文」会串行翻完整篇，可随时中断，已完成的部分保留

**AI 对话**
- 普通问答：自动带上论文标题、摘要、当前页文本
- **选中即问**：选中左侧文字 → 浮动菜单「解释这段 / 翻译这段 / 问 AI…」→ 选中内容作为引用进入对话并高亮显示
- 复制选区时会把逐词文本重组成干净句子（不会得到一堆碎词）
- 折叠思考过程、流式输出、Markdown / 表格 / 公式原文渲染
- 可选「全文上下文」开关；对话历史按论文持久化

**界面**
- 左右双栏，中间可拖动分隔（宽度记在 localStorage）
- 任一侧可**全屏折叠另一侧**（论文区的 `⛶` / 对话区的 `⛶`，或 `F` / `f`）
- `Esc` 退出全屏或关闭弹窗

**历史记录**
- 书库首页展示全部读过的论文：封面缩略图、标题、作者、出处、已读进度
- 点击继续阅读，自动回到上次的页码
- 支持按标题/作者/摘要过滤、从书库移除（连带清掉 PDF 与翻译缓存）

---

## 大模型配置

默认走 **Anthropic 兼容协议**，与本机 Claude Code CLI 用的是同一套配置。探测顺序：

1. `data/settings.json`（界面上保存过的）
2. 环境变量 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`
3. `~/.claude/settings.json` 的 `env` 段（Claude Code CLI 的配置）
4. 本地运行时：ollama `:11434`、LM Studio `:1234`、vLLM `:8000` 等

每个候选都会**发一次真实请求验证**，能通的才采用。

也可以在界面右上角「设置」里改 Base URL、密钥、模型、协议，并点「测试连接」。
本地 ollama / vLLM 请把协议切到 **OpenAI 兼容**。

翻译请求会带上 `thinking: {"type": "disabled"}` 以提速；公共 Anthropic API 不接受该字段时，
在设置里取消勾选「使用 thinking 开关」即可。

---

## 目录结构

```
agent/      内置 agent：boot.py（入口）· detect.py（API 探测）· deps.py（依赖安装）
server/     FastAPI 后端
  ├ main.py       HTTP 路由
  ├ pdf.py        PyMuPDF：渲染页面、逐词文本层、段落切分、标题识别
  ├ llm.py        Anthropic / OpenAI 双协议的流式客户端
  ├ translate.py  段落级并发翻译 + 内容哈希缓存
  ├ search.py     arXiv + Semantic Scholar 检索与下载
  └ library.py    SQLite 书库与对话历史
web/        前端（原生 JS，无构建步骤）
data/       运行时数据（已 gitignore）
```

## 接口速查

```
GET  /api/health                              服务与大模型状态
GET  /api/papers                              书库列表（?q= 过滤）
POST /api/papers/upload                       上传本地 PDF
POST /api/papers/open-local                   按绝对路径打开
GET  /api/search?q=&source=                   arXiv / Semantic Scholar 检索
POST /api/papers/open-result/stream           下载并入库（SSE 进度）
GET  /api/papers/{id}/pages                   全部页面尺寸
GET  /api/papers/{id}/pages/{n}.png           页面图片
GET  /api/papers/{id}/pages/{n}/layout        逐词文本层 + 段落块
GET  /api/papers/{id}/pages/{n}/translate     翻译该页（SSE）
POST /api/chat/stream                         对话（SSE）
POST /api/papers/{id}/progress                记录阅读位置
```
