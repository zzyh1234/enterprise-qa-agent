# examples_openai —— 真实模型运行记录（补充材料）

本目录只放**真实模型决策器**（`--planner openai`）产出的记录，与 `examples/`（离线 mock，11 个场景全量可复现）区分。
`output.json` 里的 `run.planner` 字段可直接核对来源。

## 1. 可用的真实模型路径

| 目录 | 模型 | 动作序列 | 结果 |
| --- | --- | --- | --- |
| `01_vpn_normal_real_local/` | 本地 Ollama `qwen3-vl:4b` | `search_docs` → `read_snippet D05-3.1` → `finish(answered)` | 180 天，引用逐字，审计通过 |
| `05_annual_leave_paraphrase/` | 本地 Ollama `qwen2.5:3b` | `search_docs("工作六年 带薪假 天数")` → `read_snippet D04-2.1` → `finish(answered)` | 引用年休假三档原文；**未按 6 年直接给出 5 天**，属于“政策正确但不个性化”的部分成功 |

两条记录的共同点：动作由模型输出（`decision.reason` 是模型自己的话），工具返回后才决定下一步，
最终引用必须通过逐字审计。复现命令：

```powershell
# 01（qwen3-vl:4b，单轮短路径可正常产出 content）
python run.py ask "VPN 账号多久需要重新认证？" --planner openai `
  --base-url "http://127.0.0.1:11434/v1" --api-key ollama `
  --model "qwen3-vl:4b" --timeout 240 --max-steps 6 --max-tool-calls 4

# 05（qwen2.5:3b，非 thinking 模型）
python run.py batch --planner openai --only "05_annual_leave_paraphrase" `
  --out examples_openai --base-url "http://127.0.0.1:11434/v1" --api-key ollama `
  --model "qwen2.5:3b" --timeout 180
```

## 2. 真实模型失败样本（`failures/`）

这些不是“合格路径”，保留是为了说明真实小模型的失败模式与系统护栏，避免只展示成功样例。

| 目录 | 模型行为 | 系统结果 / 说明 |
| --- | --- | --- |
| `02_conflict_asked_user/` | 只读了 D02，发现还有 D01 后让用户“提供原文进行比较” | `needs_clarification`；应自己读 D01 再判断，说明 3B 指令遵循不足 |
| `04_receipt_lost_paraphrase/` | 读了一段后反问“是否可按《票据遗失…》执行” | `needs_clarification`，0 引用；没有读 D09-1.1 |
| `06_tool_fault_recovered/` | 多次检索后输出非法 JSON，纠正后又把提示词当成答案 | `needs_clarification`；若低分作答会被“检索置信度”护栏降级 |
| `10_out_of_scope/` | 用低分文档（最高 10.35）硬答“年会奖品是 300% 日工资” | **被护栏拦下**：`audit_failed`，`evidence_gaps` 写明“检索置信度不足，不接受 answered” |

## 3. 为什么没有 11 个真实场景

- 本机无外部 API Key，`api.openai.com:443` 不可达；只能用本地 Ollama。
- 现有 `qwen3-vl:*` / `deepseek-r1:7b` 都是 thinking 模型，长提示下 OpenAI 兼容接口只返回 `reasoning`，
  `content` 为空且 `finish_reason=length`；期间还出现 GPU `CUDA error` 掉卡（已恢复）。
- `qwen2.5:3b` 是非 thinking 模型，可以稳定走完循环，但 3B 的指令遵循不足以稳定处理
  “读两篇再判断冲突”“优先读最高分片段”这类策略，于是出现上表的失败样本。
- `qwen2.5:7b` 正在拉取（约 4.7GB，速度 1.5–3.6MB/s，未在本轮完成）。

**结论（如实说明）**：本目录证明“真实模型可以驱动同一套循环并产出可核验 Trace”，
其中 2 条可用路径、4 条失败样本；它**不覆盖**全部 11 个验收点，冲突/追问/工具失败的正确行为
仍由 `examples/` 的 mock 记录与自动化测试覆盖。要满足“真实模型多路径”的完整要求，需要外部 Key
或更强的非 thinking 模型复跑 `python run.py batch --planner openai`，复核后替换 `examples/`。
