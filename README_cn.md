<div align="center">
  <h1>awecompress：上下文压缩代理</h1>
  <p><strong>把旧对话冻结成一份摘要，再送到你的 provider。</strong></p>
  <p>本地上下文压缩代理，支持 Anthropic Messages、OpenAI Chat Completions 和 OpenAI Responses 三种协议。会话历史超过 token 门限时，最旧的整轮对话被替换成一份冻结的 LLM 摘要 —— 结果缓存，之后每个请求复用同样的字节，provider 的 prompt cache 不失效。</p>
  <p>
    <a href="./README.md">English</a> ·
    <strong>简体中文</strong>
  </p>
  <p>
    <img src="https://img.shields.io/badge/version-0.2.0-7C3AED?style=flat-square" alt="Version">
    <img src="https://img.shields.io/badge/python-%E2%89%A53.9-0EA5E9?style=flat-square" alt="Python">
    <img src="https://img.shields.io/badge/license-MPL--2.0-22C55E?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/status-alpha-c96a3d?style=flat-square" alt="Status">
    <img src="https://img.shields.io/badge/install-pip-22C55E?style=flat-square" alt="pip">
    <img src="https://img.shields.io/badge/platform-terminal-334155?style=flat-square" alt="Platform">
    <img src="https://img.shields.io/github/stars/wehuman01/awecompress?style=flat-square" alt="Stars">
  </p>
</div>

> 压缩 coding agent 的长上下文：旧轮次变成一份冻结摘要，请求变小，会话跑几天不用 `/clear`。可独立代理运行，也可作为 awerouter 的一个开关进程内运行。

## 工作原理

Claude Code 每轮都重发整个对话。几小时后，大部分是死重——旧的文件读取、做完的探索、失败的尝试。

awecompress 卡在 agent 和讲以下任一协议的上游之间：Anthropic Messages、OpenAI Chat Completions、OpenAI Responses：

Responses API 合法的字符串形式 `input` 会透明转发；只有列表形式才启用压缩，这样轮次边界保持明确。

```
Claude Code → awecompress (:8808) → awerouter → 各家 provider
```

对每个请求估算上下文大小。超过门限时，在轮次边界上选一个切点（不带 tool result 的 user 消息——保证工具调用永远不会和它的结果拆开），把切点之前的所有内容用一次 LLM 调用（走同一个上游）总结成一份摘要，替换成单条摘要消息，并把结果冻结进本地 SQLite。

三个关键性质：

- **冻结，不重算。** 摘要只生成一次，之后每个请求复用同样的字节，provider 的 prompt cache 看到稳定前缀。覆盖范围扩大时摘要重写一次——一次性的 cache miss。
- **fail-open。** 压缩路径任何失败都原样转发原始请求。压缩出问题绝不会弄断会话。
- **不碰认证，不碰路由。** 认证头原样透传；路由和故障转移留在 [awerouter](https://github.com/wehuman01/awerouter)（或你的任何上游）。摘要调用本身也是走该上游的普通请求——awerouter 的 flash 路由照常生效。

某个请求想完全绕过压缩：加请求头 `X-Awecompress: off`。

## 安装

```bash
pip install awecompress
```

源码安装：

```bash
git clone https://github.com/wehuman01/awecompress
cd awecompress && pip install -e .
```

## 快速开始

和 awerouter 叠加（推荐形态）：

```bash
awerouter serve run          # 路由守护进程，照旧
awecompress serve            # 压缩代理，前台运行

# Claude Code 从指向 awerouter 改为指向 awecompress
export ANTHROPIC_BASE_URL=http://127.0.0.1:8808
claude

# openai-chat / openai-responses 客户端同样可用
export OPENAI_BASE_URL=http://127.0.0.1:8808/v1
```

独立使用，对任何 Anthropic 协议端点：

```bash
awecompress serve --upstream https://api.anthropic.com
```

看它工作——每个被压缩的请求一行日志，统计随查随有：

```
[awecompress] 3f9a2c1b: init — summarized messages 0..61 (est 41200 tok) into 1100
              via claude-sonnet in 2.8s; body est 48900 -> 8800 tokens
[awecompress] 3f9a2c1b: applied frozen summary (messages 0..61) — est 48900 -> 8800 tokens
```

```bash
awecompress status
```

## 配合 awerouter（进程内，无需代理）

`awerouter` 接受 `awecompress` profile 开关，用法和 `rtk`/`odcp` 完全一致。压缩核心在路由管线内运行——排在 odcp 去重和 rtk 压缩之前——客户端照旧指向路由端口，开关随 `routing.json` 热更新：

```json
"cc-router-1": {
  "protocol": "anthropic",
  "destinations": { "flash": "stepfun,step-3.7-flash", "pro": "glm,glm-5.3" },
  "odcp": true,
  "awecompress": true
}
```

对象形式可细调——`summaryModel` 决定谁服务摘要调用：`"flash"`（默认，flash 目的地——直接路由，绝不会因长上下文规则被改判 pro）、`"pro"`、或 `providers.json` 里任何 provider 声明过的模型（serve 启动时校验）。其余键与独立配置一致：

```json
"awecompress": {
  "summaryModel": "flash",
  "thresholdTokens": 60000,
  "keepRecentTurns": 4,
  "protectedTools": ["task", "skill", "todowrite", "todoread", "updateplan"],
  "protectedFilePatterns": ["**/*.schema.json"]
}
```

需要路由器一侧装上包：`pip install awerouter[compress]`（缺失时开关会让 serve 启动失败并给出该提示）。节省量与 rtk/odcp 并排记入用量日志（`awecompress_saved`，`awerouter usage` 可见）；`X-Awerouter-Token-Saver: off` 一个头关掉所有有损层；冻结存储与独立代理共享（`awecompress status` / `clear` 通用）。

## 配置

`~/.config/awecompress/config.json`（或 `$AWECOMPRESS_CONFIG_DIR`），首次运行自动写入默认值：

```json
{
  "port": 8808,
  "upstream": "http://127.0.0.1:20128",
  "thresholdTokens": 60000,
  "keepRecentTurns": 4,
  "minSpanTokens": 8000,
  "summaryModel": "",
  "summaryMaxTokens": 2048
}
```

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `port` | `8808` | 监听端口。 |
| `upstream` | `http://127.0.0.1:20128` | 请求发往哪里——默认是 awerouter。 |
| `thresholdTokens` | `60000` | 估算上下文超过该值触发压缩。 |
| `keepRecentTurns` | `4` | 始终原样保留的最近人类轮次数。 |
| `minSpanTokens` | `8000` | 小于该值的跨度不值得总结，跳过。 |
| `summaryModel` | `""` | 摘要调用的模型。留空 = 用请求自己的模型，由上游路由（通常走 flash）。 |
| `summaryMaxTokens` | `2048` | 摘要最大输出 token。 |
| `summaryTimeoutSeconds` | `60` | 摘要调用超时即放弃，请求原样转发。 |
| `transcriptResultCap` | `4000` | 展平历史给摘要器时，单个工具结果的字符上限。 |
| `dbPath` | 配置目录 | 冻结摘要的 SQLite 存储。 |

## 命令

```bash
awecompress serve                  # 前台运行代理
awecompress serve --port 8809 --upstream http://127.0.0.1:20128
awecompress status                 # 运行状态 + 压缩统计
awecompress config path            # 配置文件在哪
awecompress config show            # 打印配置
awecompress clear --yes            # 清空全部冻结摘要
```

## 说明与边界

- **三种协议** —— Anthropic Messages、OpenAI Chat Completions、OpenAI Responses；其他路径原样中继。
- **压缩天然有损。** 摘要提示词要求穷尽技术细节、短用户消息逐字保留，但摘要终究是摘要。`keepRecentTurns` 保证工作集原样保留；想要更多原始历史就调大。
- **会话回退到检查点**（已存摘要之下的历史变了）会通过哈希识别，从头重新压缩。
- **`/v1/messages/count_tokens`** 只应用已有摘要，绝不触发新的摘要调用。
- 灵感来自 [DCP](https://github.com/Opencode-DCP/opencode-dynamic-context-pruning) 的 Compress 策略（AGPL）和闭源的 Sleev——两者都是 harness 内集成。awecompress 是独立的、代理形态的实现；未使用任何 DCP 代码。

## 开发

```bash
pip install -e ".[dev]"
pytest
```

架构与设计契约见 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)。

## 许可

MPL-2.0。压缩行为参考 DCP 的公开文档；实现从零编写。
