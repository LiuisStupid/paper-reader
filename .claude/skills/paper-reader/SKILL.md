---
name: paper-reader
description: 启动 Paper Reader 论文阅读器：体检解释器与依赖、探测大模型接口、常驻后台起服务并验证。任何一步失败都要就地诊断修复，不要把报错原样丢给用户。用户说「启动阅读器 / 起服务 / 重启阅读器」时使用。
disable-model-invocation: true
---

# 启动 Paper Reader

按「体检 → 装依赖 → 探大模型 → 起服务 → 验证」走一遍。**这个 skill 存在的理由就是
出错时能当场修**：改 `agent/` `server/` 里的代码、换镜像、清缓存，改完重启再验，
而不是把一段 traceback 甩给用户让他自己看。

## 两条硬前提

1. 所有命令都在**仓库根目录**下跑（`server` / `agent` 是包，换了目录 import 就断）。
2. **不建虚拟环境**，依赖直接装进解释器（非 venv 时走 `pip install --user`）。
   别自作主张加 venv 来「隔离一下」——`agent/deps.py` 的注释里写了为什么。

**先定解释器，别信 `python3`**：这台机器上 `which python3` 会命中另一个项目的 venv。
优先用 `/usr/bin/python3`（3.9.6）；不确定就跑第 1 步看它打印的路径。

```bash
PY=/usr/bin/python3
$PY -V        # 期望 3.9.x
```

## 1. 体检 + 装依赖

```bash
$PY -m agent.deps            # 缺什么装什么（阿里云 → 清华 → 官方源）
$PY -m agent.deps --check    # 只看不装
```

退出码 0 = 依赖齐了。非 0 就读它打印的 pip 输出，常见四种：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 三个源全失败、超时 | 网络 / 镜像不通 | `PAPER_READER_PIP_INDEX=https://pypi.org/simple/ $PY -m agent.deps` |
| `externally-managed-environment` | PEP 668 保护 | 模块自己会退到 `--user --break-system-packages`；还失败说明 pip 太老，先 `$PY -m pip install -U pip --user` |
| 装完仍缺同一个包 | 装到了别的解释器 | 回到上面「先定解释器」，用 `$PY -m deps` 重来 |
| `No module named pip` | 裸解释器 | 模块会自动 `ensurepip --user`；失败就手动跑一次 |

允许直接改 `agent/deps.py`（加镜像、放宽版本约束），改完重跑。

## 2. 探测大模型接口

```bash
$PY -m agent.detect          # 依次真连每个候选，打印哪个通
$PY -m agent.detect --save   # 把通的那个写进 data/settings.json
```

候选顺序：已存设置 → 环境变量 `ANTHROPIC_*` → Claude Code CLI 的配置 → 本地 ollama / vLLM。

**探不到不要硬编一个 base_url 进去。** 正确做法是列出每个候选各自的失败原因
（`check()` 返回的那个 err 往往就够定位：401 是密钥、404 是路径、连接超时是网关不通），
然后问用户，二选一：

- **有 Anthropic 兼容网关**：要 base_url / key / model，追加进 `data/settings.json`
  —— 字段 `base_url`、`auth_token`、`model`、`protocol`（`anthropic` 或 `openai`）；
  公共 Anthropic API 再加 `"supports_thinking_toggle": false`（它不接受 `thinking` 字段）。
  写完重跑 `$PY -m agent.detect --save` 验证一次。**密钥只落这个文件**（已 gitignore），
  不要出现在命令回显、提交或对话之外的任何地方。
- **都没有**：告诉用户阅读和检索照常可用，只是翻译和对话不可用，可以在界面「设置」里补配。

## 3. 起服务（常驻）

```bash
PORT=8765
lsof -nP -iTCP:$PORT -sTCP:LISTEN          # 先看端口有没有人占
nohup $PY -m uvicorn server.main:app \
      --host 127.0.0.1 --port $PORT \
      --log-level warning --no-access-log \
      > data/server.log 2>&1 &
echo $! > data/server.pid
```

- `nohup` + 重定向是为了**脱离本会话**：用户关掉 Claude 窗口之后服务继续跑，
  他可以直接去浏览器里读。所以别用 Claude 的后台任务机制起它。
- **只绑 `127.0.0.1`**。这是单人本机工具，绑 `0.0.0.0` 等于把书库和 API key 交给局域网。
- 端口被占：先看下一步的 `/api/health` 通不通——通的说明是本项目已经在跑，直接用；
  不通就换个端口重起（`8766`、`8767`…）。
- `--log-level warning` 是为了让 `data/server.log` 只留真正的问题，方便回读。

## 4. 验证（别省这步）

```bash
curl -sf http://127.0.0.1:$PORT/api/health && echo
```

- 通了 → 把地址交给用户，`open http://127.0.0.1:$PORT`（macOS）帮他打开。
- 没通 → `tail -40 data/server.log` 读 traceback，**改代码 → 重启 → 重新验证**，
  循环到通过为止。常见的：依赖装错解释器、端口冲突、`data/` 权限。

## 已经开着一个了

`curl -sf /api/health` 通就别重复起（会撞端口）。要重启：

```bash
kill "$(cat data/server.pid)" 2>/dev/null; sleep 1   # 然后回到第 3 步
```

健康检查里带当前模型和 `secret_hint`，回答用户时可以直接引用它确认配置生效了。

## 不要做的事

- 不要建 `.venv`、不要 `pip install` 全局包（非 `--user`）。
- 不要把服务绑到 `0.0.0.0`。
- 不要在探测失败时猜一个模型名硬塞进配置——猜错的话用户看到的是翻译请求 404。
- 不要把 `data/settings.json` 的内容（含明文密钥）粘进对话或提交。
