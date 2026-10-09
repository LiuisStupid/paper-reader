# CLAUDE.md

论文阅读器：左侧服务端渲染的 PDF（带逐词可选文本层），右侧 AI 对话。启动入口是
`agent/boot.py`，它把环境检测、依赖安装、大模型探测、服务启动全包了。

## 启动 / 验证

```bash
python3 agent/boot.py                 # 唯一入口；自动建 venv、装依赖、探 API、开浏览器
python3 agent/boot.py --check         # 只检测不启动
./.venv/bin/python agent/boot.py --no-browser --port 8765   # 已在 venv 里时可直接跑
```

改完后端重启即可；前端是原生 JS，刷新浏览器就行（无构建步骤）。

## 约定与坑（改代码前先读）

- **Python 3.9 兼容是硬约束**（本机只有 `/usr/bin/python3` = 3.9.6）。所有模块都带
  `from __future__ import annotations`，运行时不要用 `X | Y`、`match`、`asyncio.TaskGroup`。
  用 `Optional[X]` / `List[X]` / `Dict[K, V]`。
- **PyPI 在国内极慢**（实测比阿里云镜像慢 25 倍）。`agent/deps.py` 会依次尝试
  阿里云 → 清华 → 官方源，可用 `PAPER_READER_PIP_INDEX` 覆盖。
- **大模型网关是 Anthropic 兼容透传**（`ANTHROPIC_BASE_URL`）。`/v1/models` 返回空，
  所以模型名是配置项而非探测结果。翻译请求会带 `thinking: {"type": "disabled"}` 提速；
  公共 Anthropic API 不接受该字段，靠 `supports_thinking_toggle` 开关兜住。
- **翻译的缓存键是段落文本的 sha1**（`translate.py:cache_key`），存在
  `data/papers/<id>/translation.json` 的 `_entries` 里。改翻译提示词不会让缓存失效，
  需要 `?force=true` 或删缓存文件。
- **arXiv 检索是三轮降级**（`search.py:_queries`）：短语 → 去停用词 AND → OR。
  单纯 AND 会因为标题里的停用词（"attention **is** all you need"）返回 0 条。
- **arXiv 下载很慢**（几十 KB/s），所以下载走 SSE 报进度
  （`/api/papers/open-result/stream`），不要改回一次性请求。
- **文本层是一词一个绝对定位的透明 span**，坐标 = PDF 点 × `scale`。改动页面尺寸 /
  缩放时必须同时重建文本层和译文层（`app.js:drawAllPages`）。
- **选区取文本用 `range.intersectsNode()`，不要用 `cloneContents()`**：克隆出的
  fragment 不含 `.text-layer` 祖先，选择器会全部落空，退化成没有空格的字符串。
- **原文模式下 `.trans-layer` 必须 `display: none`**。它 z-index 高于文本层，
  只关 `pointer-events` 会让白底译文块把英文正文整个盖住。
- **`#chatEmpty` 是 `#chatLog` 的子元素**，清空对话要用 `clearChatLog()` 删 `.msg`，
  不能 `innerHTML = ''`（会连带删掉它，后续 `?.classList` 读到 null）。
- **`#home section` 的 max-width 限定在首页**：`#viewerPane` / `#chatPane` 也是
  `<section>`，全局规则会让两侧全屏时只占 1180px。
- 页面翻译有**两级并发**：页内 6 路（`translate.py:CONCURRENCY`），页间 2 页
  （`app.js:TRANSLATE_CONCURRENCY` + 队列）。自动翻译随滚动触发，必须受页间闸门约束。

## 结构

```
agent/boot.py     入口：venv → deps → detect LLM → uvicorn
agent/detect.py   候选端点（已存设置 → 环境变量 → Claude CLI 配置 → 本地运行时）逐个真连验证
agent/deps.py     venv 创建 / requirements 检测安装 / 切换解释器
server/main.py    HTTP 路由（29 条）
server/pdf.py     PyMuPDF：渲染、逐词文本层、段落合并、标题识别（按字号而非首行）
server/llm.py     Anthropic + OpenAI 双协议流式客户端，事件归一化为 thinking/text/done/error
server/translate.py  段落级并发翻译 + 内容哈希缓存
server/search.py  arXiv（多轮降级）+ Semantic Scholar，带进度下载
server/library.py SQLite：papers + messages
web/app.js        前端全部逻辑（无框架）
```

`data/` 已 gitignore，含 `library.db`、`settings.json`（可能存密钥）、论文与译文缓存。
