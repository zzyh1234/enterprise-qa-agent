# examples_openai —— 真实模型运行记录（补充材料）

本目录只放**真实模型决策器**（`--planner openai`）产出的运行记录，用于和 `examples/`（离线 mock 决策器、
11 个场景全量可复现）区分。`run.planner` 字段可直接核对来源。

## 目录内容

| 目录 | 模型 | 路径 | 状态 |
| --- | --- | --- | --- |
| `01_vpn_normal_real_local/` | Ollama `qwen3-vl:4b`（本地、无外部 Key） | 模型自主选择 `search_docs` → `read_snippet` → `finish` | answered，1 条逐字引用，审计通过 |

生成命令（本机 Ollama，OpenAI 兼容接口）：

```powershell
python run.py ask "VPN 账号多久需要重新认证？" --planner openai `
  --base-url "http://127.0.0.1:11434/v1" --api-key ollama `
  --model "qwen3-vl:4b" --timeout 240 --max-steps 6 --max-tool-calls 4 `
  --save tmp_openai_probe/01 --json
```

该记录随后用 `eqa.runner.write_example` 整理成 `input.json / output.json / trace.json / trace.md`。

## 为什么只有一条路径

本机没有外部 API Key，`api.openai.com:443` 不可达；本地 Ollama 可用，但：

1. 现有三个模型（`qwen3-vl:4b` / `qwen3-vl:latest` / `deepseek-r1:7b`）都是 **thinking 模型**。
   在更长的提示（多步观察）下，Ollama 的 OpenAI 兼容接口只返回 `message.reasoning`，
   `content` 为空且 `finish_reason=length` —— 模型把 token 全用在思考上，没有输出 JSON。
   代码会给出明确报错（`模型只返回了 reasoning/thinking…`），而不是把它当答案。
2. 更长的请求触发了 GPU 故障：`CUDA error` → `GPU is lost`（需重启机器恢复），
   因此无法继续在本地批量跑完 11 个场景。

**结论（如实说明）**：本目录只证明「同一套循环/工具/审计可以用真实模型跑通并产出可核验 Trace」，
不覆盖冲突、追问、工具失败等验收点；这些仍由 `examples/` 的 mock 记录覆盖。
两者都不能替代「用稳定真实模型（外部 Key 或非 thinking 模型）跑完 11 个场景」这一步。
