# 企业知识问答 Agent（Enterprise Knowledge QA Agent）

> 题目：企业知识问答 Agent —— 帮员工找到制度/产品问题的可靠答案，给出**文档 ID + 原文片段**，
> 遇到**文档冲突**先查证、查证不了就**追问**，绝不凭常识编造；全流程有可复核的 Trace 与执行上限。

实现方式：**Python 标准库原生 ReAct 循环**（不依赖任何 Agent 框架）。
CLI 默认 `--planner openai`（由真实模型输出动作）；未配置 `EQA_API_KEY` 时自动回退到离线 `mock`，
并在 `run.planner_requested / run.planner_fallback`、stderr 与 Trace 中明确标注。
另提供 `replay` 回放入口。所有业务工具、语料、故障注入、Trace、测试均在仓库内。

---

## 1. 核心行为（与验收点一一对应）

1. **先检索、再读原文、最后回答**：`search_docs` 只返回命中摘要，必须 `read_snippet` 取到逐字原文后才能引用。
2. **引用可核验**：每条引用都带 `doc_id / section_id / quote`，审计器逐字比对原文；对不上即判定 `audit_failed`。
3. **文档冲突不擅自选**：同一属性在多个文档中取值不同且无优先级/适用范围说明时，先检索是否存在制度解释
   （如「以《X》为准」）；查证不到就 `ask_user` 列出冲突取值与引用。
4. **资料外问题不回答**：检索置信度不足时返回 `insufficient_evidence` 与资料缺口，而不是硬答。
5. **工具失败有明确状态**：瞬时失败重试一次；持续失败返回 `blocked`，且不输出没有原文支撑的结论。
6. **执行上限**：步数 / 工具调用次数双重上限，超限返回 `incomplete_budget_exceeded`。
7. **上下文追问**：可把上一轮 `output.json` 作为上下文传入，按用户显式指定的制度给出答案并保留冲突说明。

---

## 2. 快速开始（无需安装第三方依赖）

要求：Python >= 3.9（开发验证使用 Python 3.13），Windows / macOS / Linux 均可。

默认 `--planner openai`（真实模型）；**没有 Key 也能跑**：会自动回退到离线 `mock`，
并在 stderr、`run.planner_requested`、`run.planner_fallback`、`run.notes` 中明确标注。
需要完全可复现的离线 Trace 时，显式加 `--planner mock`。

```powershell
# 在仓库根目录执行
python run.py corpus                     # 查看语料统计
python run.py ask "VPN 账号多久需要重新认证？"        # 默认 openai；无 key 自动回退 mock 并提示
python run.py ask "VPN 账号多久需要重新认证？" --planner mock   # 纯离线
python run.py ask "报销期限是多久？" --planner mock             # 冲突 → 追问
python run.py ask "那按最新发布的版本执行呢？" --context examples/02_reimbursement_conflict/output.json --planner mock
python run.py batch --planner mock       # 离线重跑全部 11 个场景，写入 examples/<id>/
python run.py verify "examples/*/output.json"   # 逐字引用 + 冲突说明审计
python scripts/run_tests.py              # 67 个测试，结果写入 examples/test-results.txt
```

可选（生成 `eqa` 命令）：`pip install -e .`

### 用真实模型跑

```powershell
$env:EQA_API_KEY = "sk-..."                       # 不要提交到仓库
$env:EQA_BASE_URL = "https://api.openai.com/v1"   # 任意 OpenAI 兼容网关（可选）
$env:EQA_MODEL = "gpt-4o-mini"                    # 可选

python run.py ask "报销期限是多久？" --max-tool-calls 8
python run.py ask "VPN 账号..." --no-fallback      # 无 key 时直接报错，避免误以为在跑真实模型

# 用真实模型重新生成整套 Trace（先输出到 examples_openai/，复核后再替换 examples/）
python run.py batch --planner openai --out examples_openai
python run.py verify "examples_openai/*/output.json"
```

模型只负责输出 JSON 动作（`search_docs` / `read_snippet` / `ask_user` / `finish`），
工具执行、预算、引用审计、冲突审计全部在代码里强制执行。
**离线 Mock 只验证流程与校验逻辑，不证明真实模型的决策质量。**
`--planner replay --replay data/replay/vpn_replay.json` 是回放入口：用预记录的 Mock 决策走同一套协议闭环。

---

## 3. 评审入口（建议顺序）

| 想确认什么 | 直接看 |
| --- | --- |
| 冲突如何处理、是否有依据 | `examples/02_reimbursement_conflict/output.json`、`trace.md` |
| 用户确认后答案如何变化 | `examples/03_conflict_followup/` |
| 相同问题、语料新增制度解释后动作如何变化 | `examples/09_corpus_swap_resolved/` 与 `examples/02_.../` 对比 |
| 检索改写与相似问法 | `examples/05_annual_leave_paraphrase/`、`examples/04_receipt_lost_paraphrase/` |
| 工具失败与拒绝作答 | `examples/06_tool_fault_recovered/`、`examples/07_tool_fault_blocked/` |
| 执行上限 | `examples/08_budget_exceeded/` |
| 资料外问题 | `examples/10_out_of_scope/` |
| 引用是否真实 | `examples/verify-results.txt`（11 个输出全部 PASS） |
| 测试是否真跑过 | `examples/test-results.txt`（`ran=67 failures=0 errors=0`） |
| Trace 是真实模型还是规则 | 看 `output.json` 的 `run.planner`、`run.planner_requested`、`run.planner_fallback`；当前 examples 为 mock（本机无 key），真实模型生成命令见第 2 节 |

---

## 4. 验收点 → 文件 / 测试映射

| 验收点 | 运行记录 | 自动化断言 |
| --- | --- | --- |
| 明确答案 + 引用原文 | `examples/01_vpn_normal/` | `tests/test_agent_flows.py::test_normal_answer_is_cited_and_audited` |
| 文档冲突（30 天 vs 15 天） | `examples/02_reimbursement_conflict/` | `test_conflict_lists_both_values_and_asks_user` |
| 上下文追问（用户指定制度） | `examples/03_conflict_followup/` | `test_followup_uses_user_selected_policy` |
| 改写后的相似问题 | `examples/04_receipt_lost_paraphrase/` | `test_paraphrased_question_reads_original_section` |
| 检索改进（低置信 → 改写） | `examples/05_annual_leave_paraphrase/` | `test_retrieval_rewrite_improves_low_confidence_query` |
| 工具失败（瞬时/持续） | `examples/06_.../`、`examples/07_.../` | `test_transient_tool_failure_is_retried`、`test_persistent_tool_failure_blocks_answer` |
| 执行上限 + 未完成状态 | `examples/08_budget_exceeded/` | `test_budget_limit_returns_explicit_incomplete_status`、`test_budget_never_exceeded_in_any_flow` |
| 资料外问题 | `examples/10_out_of_scope/` | `test_out_of_scope_question_is_not_answered` |
| 替换文档内容后答案/动作改变 | `examples/02` vs `examples/09` | `test_corpus_change_changes_answer_and_actions`、`test_answer_follows_changed_document_content`、`test_conflict_values_follow_changed_document_content` |
| 引用逐字支持回答 | 全部输出 | `test_all_example_citations_are_verbatim`、`test_tampered_quote_is_rejected` |
| 真实模型协议闭环 | `examples/11_replay_offline/` | `tests/test_planners.py`（假 transport 模拟三次模型响应） |

---

## 5. 实际输出长什么样

冲突场景（`examples/02_reimbursement_conflict/output.json`，节选）：

```
状态：needs_clarification（需要澄清（存在未解决冲突或缺少上下文））
资料存在冲突，暂无法给出唯一答案（主题：报销期限）：
- D02《费用报销操作指引（2024 版）》（版本 2024-06-15）第 D02-3.1 节：为配合月度结账，员工应在费用发生之日起 15 天内提交报销单据。
- D01《员工费用报销管理规定》（版本 2024-03-01）第 D01-4.2 节：员工应当在费用发生之日起 30 个自然日内提交报销申请。
以上文件均标注适用于「全体员工」，语料中未给出优先级或适用范围差异。
查证情况：语料中没有制度解释类文档。
请确认以哪份制度为准，或由制度归口部门解释后再答复。
```

同一个问题，语料新增 D03《财务共享中心制度解释第 2 号》后（`examples/09_corpus_swap_resolved/`）：

```
状态：answered（已回答）
根据《财务共享中心制度解释第 2 号》（D03，版本 2024-07-01）第 D03-1.1 节：
「……以《费用报销操作指引（2024 版）》为准，并由财务共享中心负责解释。」
结合被指定为准的《费用报销操作指引（2024 版）》（D02，版本 2024-06-15）第 D02-3.1 节：……
结论：报销提交时限为 15 天。
说明：D01（30）的取值与本答案不同，已依据 D03 的优先级规则采用 D02。
```

Trace 里能看到动作差异（`examples/*/trace.md`）：

- `02`：检索 → 改写检索 → 读 D02 → 读 D01 → 查证检索 → `ask_user`
- `09`：检索 → 改写检索 → 读 D02 → 读 D01 → 查证检索 → 读 D03 → `finish(answered)`

---

## 6. 任务状态（结果里的 `status`）

| status | 含义 | 典型场景 |
| --- | --- | --- |
| `answered` | 有可核验原文、无未解决冲突 | 01 / 03 / 04 / 05 / 06 / 09 / 11 |
| `needs_clarification` | 冲突无法在语料内消解，需用户确认 | 02 |
| `insufficient_evidence` | 知识库没有能回答的资料，不硬答 | 10 |
| `blocked` | 工具持续失败且无原文，拒绝作答 | 07 |
| `incomplete_budget_exceeded` | 达到步数 / 工具调用上限，明确未完成 | 08 |
| `partial` | 部分证据可用、部分缺失（预留给真实模型/多模态资料） | — |
| `audit_failed` | 引用编造或冲突未说明，答案被审计降级 | `tests/test_audit.py` |
| `failed` | 决策协议不可用、回放耗尽等硬失败 | `tests/test_planners.py` |

---

## 7. 目录结构

```
enterprise-qa-agent/
├─ run.py                     # 免安装入口：python run.py ask "…"
├─ pyproject.toml             # 可选 pip install -e .（无第三方依赖）
├─ requirements.txt           # 仅说明「无需安装」
├─ .env.example               # 仅 openai planner 需要；默认 mock 无需变量
├─ src/eqa/
│  ├─ text.py                 # 归一化 / 中文单字+二元组 / 句子切分
│  ├─ corpus.py               # 语料加载与查询
│  ├─ retrieval.py            # BM25 片段检索 + 文档聚合 + 同义词改写
│  ├─ tools.py                # search_docs / read_snippet（含故障注入）
│  ├─ analysis.py             # 意图、证据判定、冲突检测、优先级/用户选择解析
│  ├─ planners.py             # Mock / OpenAI 兼容 / 回放 三种决策器 + JSON 协议校验
│  ├─ agent.py                # ReAct 循环、预算、结果组装、审计降级
│  ├─ audit.py                # 逐字引用审计 + 冲突说明审计
│  ├─ runner.py               # 场景执行/示例落盘/Trace Markdown
│  └─ cli.py                  # ask / batch / verify / corpus
├─ data/
│  ├─ documents.json          # 合成制度/FAQ 语料（D01 30 天 vs D02 15 天冲突）
│  ├─ documents_v2.json       # 替换语料：新增 D03 制度解释（冲突可解）
│  ├─ intents.json            # 意图与证据规则（Mock 用，真实模型只用于审计）
│  ├─ query_expansions.json   # 同义词/口语改写词典
│  ├─ scenarios.json          # 11 个评测场景
│  └─ replay/vpn_replay.json  # 离线回放决策（Mock 决策，非真实模型输出）
├─ tests/                     # 67 个 unittest（无第三方依赖）
├─ scripts/                   # 生成 v2 语料 / 检索分数 / 跑测试 / 审计示例
├─ examples/                  # 11 组 input / output / trace.json / trace.md + 测试与审计结果
└─ docs/                      # 需求、方案、评测、AI 使用、复盘
```

---

## 8. 已投入时间与未完成部分

| 项目 | 情况 |
| --- | --- |
| AI 辅助实现 + 自测（本次会话） | 约 3 小时（代码、语料、测试、示例、文档） |
| 提交人实际用时 | 待提交人核对后填写（未记录则不填，勿虚报） |
| examples/ Trace 来源 | 当前为 **mock**（本机无 API Key；`run.planner=mock`、`planner_fallback=true`）。真实模型需按第 2 节命令重跑并复核 |
| 未完成 | 真实模型 API 未在本次环境调用（无 key）；审批流/权限/多租户/线上部署未做；评测集规模小（11 场景 / 8 篇文档，v2 为 9 篇） |
| 刻意不做 | Web UI、多 Agent、向量数据库、生产级 Runtime（题目明确非必需） |

已知限制详见 `docs/03-evaluation.md` 第 4 节；设计取舍见 `docs/02-solution.md`。
