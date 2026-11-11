# 03 评测：预期、实际结果、失败与限制

## 1. 测试总览（实际运行）

命令：`python scripts/run_tests.py`
结果文件：`examples/test-results.txt`

```
ran=69 failures=0 errors=0 skipped=0 -> OK
```

| 测试模块 | 数量 | 覆盖内容 |
| --- | --- | --- |
| `tests/test_retrieval.py` | 5 | 切词/归一化、改写提分、越界低分、文档聚合与元数据 |
| `tests/test_tools.py` | 9 | 检索摘要、改写参数、逐字原文、非法片段/文档、未知工具、故障注入（error/empty）、空查询 |
| `tests/test_analysis.py` | 9 | 8 类意图识别、越界无意图、VPN 密码 vs 重新认证、引用≠证据、冲突判定、分档不算冲突、memo 优先级、用户显式选择、年假分档 |
| `tests/test_agent_flows.py` | 15 | 11 个场景的端到端断言 + 预算不越界 + 引用逐字 + 两处「改内容→改答案」 |
| `tests/test_audit.py` | 9 | 引用逐字/篡改/不存在文档、冲突说明、Mock 与「模型输出」的审计降级、answered 无引用、已读冲突但只引用一方 |
| `tests/test_planners.py` | 14 | JSON 解析与非法动作、真实模型协议循环（假 transport）、纠正、失败、回放 |
| `tests/test_cli.py` | 8 | `ask --json`、`--max-tool-calls`、`batch`、`verify`、`--context` 追问、`corpus`、默认 openai 无 key 回退、`--no-fallback` 快速失败、batch 回退标注 |

引用/冲突审计的独立复核：`python scripts/verify_examples.py` → `examples/verify-results.txt`，
11 个输出全部 `[PASS]`。

## 2. 场景运行结果（`python run.py batch` 实际输出）

| # | 场景 | 问题 | status | 引用 | 工具调用 | 步数 | 审计 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | `01_vpn_normal` | VPN 账号多久需要重新认证？ | answered | 1（D05-3.1） | 2/8 | 3/8 | PASS |
| 02 | `02_reimbursement_conflict` | 报销期限是多久？ | needs_clarification | 2（D01、D02） | 5/8 | 6/8 | PASS |
| 03 | `03_conflict_followup` | 那如果按最新发布的版本执行，期限是多久？ | answered | 2（D02、D01） | 2/8 | 3/10 | PASS |
| 04 | `04_receipt_lost_paraphrase` | 报销的发票不小心弄没了还能报吗？ | answered | 1（D09-1.1） | 5/8 | 6/10 | PASS |
| 05 | `05_annual_leave_paraphrase` | 我工作六年了，一年能休几天带薪假？ | answered | 1（D04-2.1） | 3/8 | 4/10 | PASS |
| 06 | `06_tool_fault_recovered` | VPN 密码有什么要求？ | answered | 1（D05-3.2） | 4/8 | 5/10 | PASS |
| 07 | `07_tool_fault_blocked` | 发票丢失了怎么办？ | blocked | 0 | 8/8 | 9/10 | PASS |
| 08 | `08_budget_exceeded` | 云盘免费版单文件上传上限是多少？ | incomplete_budget_exceeded | 0 | 1/1 | 1/5 | PASS |
| 09 | `09_corpus_swap_resolved` | 报销期限是多久？（v2 语料） | answered | 3（D03、D02、D01） | 6/8 | 7/10 | PASS |
| 10 | `10_out_of_scope` | 公司年会抽奖的奖品是什么？ | insufficient_evidence | 0 | 1/6 | 2/8 | PASS |
| 11 | `11_replay_offline` | VPN 账号多久需要重新认证？（回放） | answered | 1（D05-3.1） | 2/8 | 3/8 | PASS |

每个目录含：`input.json`（问题、历史、语料、预算、故障注入）、`output.json`（完整结果）、
`trace.json`（仅步骤）、`trace.md`（人类可读表格）。

### 2.1 真实模型补充记录（`examples_openai/`）

- 目录：`examples_openai/01_vpn_normal_real_local/`，模型为本地 Ollama `qwen3-vl:4b`
  （不用外部 Key），`run.planner=openai`、`planner_fallback=false`。
- 模型自主决策：`search_docs`（原问题）→ `read_snippet D05-3.1` → `finish(answered)`；
  答案引用原文逐字，`audit.passed=true`，总耗时约 15s。
- 边界：只有 1 条路径。批量复跑时该 thinking 模型在长提示下只返回 `reasoning`（`content` 为空），
  随后 GPU 报 `CUDA error` 并掉卡；代码对此给出明确报错而不是误当答案（见第 4 节）。
- 因此它只证明「真实模型能驱动同一套循环并产出可核验 Trace」，**不覆盖全部验收点**；
  11 个验收场景仍由 `examples/` 的 mock 记录覆盖。稳定真实模型（外部 Key 或非 thinking 模型）
  跑完全量后，才应替换 `examples/` 并更新本节。

## 3. 关键用例逐条核对

### 3.1 明确答案 + 引用（01）

- 答案：`VPN 重新认证周期为 180 天。`
- 引用：`D05 / D05-3.1`，原文逐字包含「VPN 账号每 180 天需要进行一次重新认证…」。
- 动作：`search_docs` → `read_snippet` → `finish(answered)`；没有追问、没有多余工具调用。

### 3.2 文档冲突（02）

- 答案列出 `D02：15 天` 与 `D01：30 个自然日`，说明「均适用于全体员工、未给出优先级」，
  查证结论「语料中没有制度解释类文档」，并请求用户确认。
- `status=needs_clarification`；`clarification.options` 含「按 D02…」「按 D01…」「按最新发布的版本」「由财务共享中心解释」。
- 没有出现「报销期限为 15 天」这种擅自结论；引用 2 条均逐字。

### 3.3 上下文追问（03）

- 输入带上一轮 `output.json`；Planner 走上下文分支：直接 `read_snippet D02-3.1` → `read_snippet D01-4.2` → finish。
- 答案：`按您确认的适用制度《费用报销操作指引（2024 版）》（D02）…结论：报销提交时限为 15 天。`
  并保留「D01（30）与本答案存在冲突，本次按您指定的 D02 执行」。
- `conflicts[0].resolution=user_selected`。

### 3.4 改写后的相似问题（04、05）

- 04「发票弄没了」：改写检索后先读 `D01-4.3`，发现它只是引用《票据遗失与替代凭证处理说明》，
  判定「与问题不直接相关」；继续读 `D09-1.1` 并据此回答（记账联复印件 + 发票专用章 + 备注「票据遗失」）。
  Trace 与 `run.notes` 完整记录了这次「读错 → 调整」。
- 05「工作六年、带薪假」：原查询 25.45 分 → 改写后 67.17 分；答案按「满 1 年不满 10 年」档给出 5 天，
  并把原文三档全部引用出来。

### 3.5 资料外问题（10）

- 检索有低分噪声（D04 11.24），但无意图匹配且远低于 60 的保守阈值；返回 `insufficient_evidence`，
  引用为空，`evidence_gaps` 列出检索词与低置信候选，明确「不作答」。

### 3.6 工具失败（06、07）

- 06：第一次 `search_docs` 报「检索服务超时（模拟故障）」，原样重试成功；随后改写检索 + 读 `D05-3.2`，
  返回 12 位/90 天要求。Trace 第 1 步 `ok=false`，第 2 步恢复。
- 07：`read_snippet` 连续失败（注入 20 次），Agent 对 D09、D01、D02 各尝试两次后停止，
  `status=blocked`，引用为空，`evidence_gaps` 写明工具与错误，不输出任何「记账联」之类未经核验的内容。

### 3.7 执行上限（08）

- `--max-tool-calls 1`：检索 1 次后，Planner 想读 `D06-1.1`，Agent 在调用前拦截。
- `status=incomplete_budget_exceeded`，`budget.exceeded=true`，答案写明「本应执行 read_snippet…但预算已用尽」，
  引用为空（没有读取原文就不引用）。
- 测试 `test_budget_never_exceeded_in_any_flow` 断言所有场景的用量都不超过上限。

### 3.8 替换文档内容（09 + 两个测试）

- `data/documents_v2.json` = 原语料 + D03《财务共享中心制度解释第 2 号》（冲突时以操作指引为准）。
- 同一问题下：`02` 追问；`09` 读 D03 后给出 15 天并引用 D03/D02/D01。
- 另外两个测试直接在临时语料里改文本：
  - `180 天 → 240 天`：答案变为 240，且不再出现 180；
  - `30 个自然日 → 45 个自然日`：冲突答案变为 45，且不再出现 30。
  说明答案来自文档内容，而不是模板或硬编码。

## 4. 失败、限制与未验证项（诚实清单）

| 项目 | 现状 | 影响 / 后续 |
| --- | --- | --- |
| 真实模型效果 | **未调用外部 API**（环境无 Key，api.openai.com 不可达）。只验证了协议循环、JSON 校验、审计兜底 | README/文档均标注；有 Key 时 `python run.py batch --planner openai --out examples_openai` 可直接生成真实 Trace，但效果需另行评测 |
| 本地真实模型尝试 | 本机 Ollama 有 `qwen3-vl:4b`、`deepseek-r1:7b`，但都是 thinking 模型：OpenAI 兼容接口把内容放在 `reasoning`、`content` 为空且 `finish_reason=length`；随后 GPU 报 `CUDA error` 并掉卡（需重启）。单轮短提示可正常返回 JSON，长提示未产出可用 Trace | 代码已对「只有 reasoning」给出可操作报错；重启后可用非 thinking 模型（如 qwen2.5）或外部 Key 重跑，步骤见 README |
| examples/ Trace 来源 | 当前 11 个示例由 **mock** 生成（本机无 key），`run.planner=mock`、`planner_fallback=true` | 不能当作真实模型决策证据；配置 key 后重跑、复核审计，再替换 `examples/` 并更新本文档 |
| Mock 决策器 | 规则化，只能证明「流程与护栏」成立，不能证明「模型会这样决策」 | 不作为真实模型能力证据 |
| 检索阈值 | 15 / 30 / 60 是基于 8 篇文档、11 个问题校准的 | 扩充语料后需用 `scripts/inspect_retrieval.py` 重新校准；生产应改向量 + rerank |
| 同义词表 | 约 20 条口语映射 | 未覆盖全部口语/方言；改写失败会退回「资料不足」而不是乱答 |
| 冲突检测 | 仅覆盖「可抽数值/关键词」的属性（天数、金额、GB、位数） | 语义型冲突（例如两种表述互相否定）检测不到，需模型 + 人工规则 |
| 引用审计 | 要求逐字；模型若改写引用会被判 `audit_failed` | 这是有意的严格策略，换来可机器核验；生产中可在提示词里强调原文复制 |
| 追问理解 | 只识别显式选择（文档 ID / 「最新版本」/ 制度类型词） | 更自由的说法会再次追问，不会猜 |
| 多轮 | 仅支持 1 轮上下文（`--context` 上一轮 output.json），无会话存储 | 需要 Session 时再加 |
| 工具故障 | 通过 `tool_faults` 注入模拟，不是真实网络故障 | 真实超时/限流需重试策略与熔断 |
| 规模 | BM25 每次查询 O(片段数)，内存索引 | 万级文档以上需倒排/向量库 |
| 语料 | 全部为合成数据，机构名、电话、金额均为虚构 | 不涉及真实隐私或密钥 |

## 5. 审核要点自查

| 审核要点 | 证据 |
| --- | --- |
| 引用原文是否支持回答 | 11 个输出的 `citations[].quote` 全部逐字命中原文；`examples/verify-results.txt` 全 PASS；`test_all_example_citations_are_verbatim` |
| 是否存在无依据回答 | 资料外 → `insufficient_evidence`；工具失败 → `blocked`；引用编造 → 审计降级 `audit_failed`（`tests/test_audit.py`） |
| 冲突是否擅自选择 | 02 明确列出双方取值并追问；09 只有读到 D03 明确「以 D02 为准」才给 15 天；测试 `test_conflict_lists_both_values_and_asks_user` |
| 替换文档内容后答案/动作是否改变 | 02 vs 09；`test_answer_follows_changed_document_content`；`test_conflict_values_follow_changed_document_content` |
| 是否有检索改进证据 | 05：改写前后分数 25.45 → 67.17，Trace 记录 `use_expansion` 与新增词；02：7.30 → 59.07 |
| 是否有执行上限与未完成状态 | 08；`test_budget_limit_returns_explicit_incomplete_status` |
| 是否有至少两条不同执行路径 | mock：01（直接答）/02（冲突追问）/06（失败重试）/09（查证后答）等 11 条 Trace；真实模型：`examples_openai/` 1 条（search→read→finish）。真实模型的多路径记录仍需稳定模型复跑 |

## 6. 复现命令

```powershell
# 1) 看语料
python run.py corpus

# 2) 单问题（默认 openai；无 key 自动回退 mock，并在 run 中标明）
python run.py ask "报销期限是多久？"
python run.py ask "报销期限是多久？" --planner mock --json
python run.py ask "报销期限是多久？" --json

# 3) 上下文追问（先跑 02 或直接引用仓库内示例）
python run.py ask "那按最新发布的版本执行呢？" --context examples/02_reimbursement_conflict/output.json --planner mock

# 4) 全量场景与审计
python run.py batch --planner mock              # 离线可复现
python run.py batch --planner openai --out examples_openai   # 真实模型（需 key）
python scripts/verify_examples.py

# 5) 测试
python scripts/run_tests.py
```

如果命令输出中文乱码，是 Windows 控制台编码问题，可先执行：

```powershell
$env:PYTHONIOENCODING = "utf-8"
```
