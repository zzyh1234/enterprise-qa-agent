# examples 说明与索引

本目录是**实际运行结果**（不是手写样例）：每个场景一个目录，包含

| 文件 | 内容 |
| --- | --- |
| `input.json` | 场景 ID、问题、历史、语料、planner（含 `planner_requested / planner_fallback`）、预算、故障注入、覆盖的验收点 |
| `output.json` | 完整结果：status / answer / citations / gaps / conflicts / clarification / audit / run / trace |
| `trace.json` | 仅执行步骤（决策依据、动作、参数、观察、后续动作） |
| `trace.md` | 人类可读的 Trace 表格 + 答案 + 引用 + 缺口 + 决策器来源 |
| `test-results.txt`（根目录） | 69 个 unittest 的实际输出：`ran=69 failures=0 errors=0` |
| `verify-results.txt`（根目录） | 对全部 `output.json` 的逐字引用/冲突审计：11 个 `[PASS]` |

> **当前 Trace 来源**：本机没有 API Key，11 个示例均由 `--planner mock` 生成，
> 因此 `output.json` 中 `run.planner=mock`、`run.planner_requested=mock`、`run.planner_fallback=false`。
> CLI 的默认值是 `--planner openai`；未配置 key 时会自动回退并标注。
> 生成真实模型 Trace：`python run.py batch --planner openai --out examples_openai`，复核后再替换本目录。

生成/刷新命令：

```powershell
python run.py batch --planner mock   # 重新生成 11 个目录（离线可复现）
python scripts/run_tests.py         # 重新生成 test-results.txt
python scripts/verify_examples.py   # 重新生成 verify-results.txt
```

## 1. 场景索引

| # | 目录 | 问题 | status | 关键动作序列 | 验收点 |
| --- | --- | --- | --- | --- | --- |
| 01 | `01_vpn_normal` | VPN 账号多久需要重新认证？ | answered | search → read D05-3.1 → finish | 明确答案、引用原文 |
| 02 | `02_reimbursement_conflict` | 报销期限是多久？ | needs_clarification | search → 改写 search → read D02 → read D01 → 查证 search → ask_user | 文档冲突、请求确认 |
| 03 | `03_conflict_followup` | 那如果按最新发布的版本执行，期限是多久？ | answered | read D02 → read D01 → finish | 上下文追问、冲突说明 |
| 04 | `04_receipt_lost_paraphrase` | 报销的发票不小心弄没了还能报吗？ | answered | search ×2 → read D01-4.3（判为非证据）→ read D01-4.2（非证据）→ read D09-1.1 → finish | 相似问法、读原文后调整 |
| 05 | `05_annual_leave_paraphrase` | 我工作六年了，一年能休几天带薪假？ | answered | search → 改写 search → read D04-2.1 → finish | 检索改进、相似问法 |
| 06 | `06_tool_fault_recovered` | VPN 密码有什么要求？ | answered | search（失败）→ 重试成功 → 改写 search → read D05-3.2 → finish | 工具失败可恢复 |
| 07 | `07_tool_fault_blocked` | 发票丢失了怎么办？ | blocked | search ×2 → read 失败 ×6 → finish(blocked) | 工具失败、拒绝无依据作答 |
| 08 | `08_budget_exceeded` | 云盘免费版单文件上传上限是多少？ | incomplete_budget_exceeded | search → finish（预算拦截 read） | 执行上限、未完成状态 |
| 09 | `09_corpus_swap_resolved` | 报销期限是多久？（v2 语料） | answered | search → 改写 search → read D02 → read D01 → 查证 search → read D03 → finish | 冲突查证、替换文档后答案/动作改变 |
| 10 | `10_out_of_scope` | 公司年会抽奖的奖品是什么？ | insufficient_evidence | search → finish | 资料外问题、不编造 |
| 11 | `11_replay_offline` | VPN 账号多久需要重新认证？（回放） | answered | search → read D05-3.1 → finish | 离线回放、模型协议闭环 |

## 2. 覆盖矩阵（验收点 → 场景）

| 验收点 | 场景 |
| --- | --- |
| 明确答案 | 01、06、11 |
| 文档冲突 | 02、09 |
| 资料外问题 | 10 |
| 改写后的相似问题 | 04、05 |
| 上下文追问 | 03 |
| 工具失败 | 06（瞬时恢复）、07（持续失败） |
| 执行上限 + 未完成状态 | 08、07 |
| 引用逐字可核验 | 全部（见 `verify-results.txt`） |
| 替换文档内容后答案与动作改变 | 02 vs 09 + 两个改文本测试 |

## 3. 两条路径的对照（模型根据工具结果调整动作）

**02（默认语料）**

1. `search_docs` 原问题 → 最高分 7.30，低于改写阈值
2. `search_docs` + 同义词 → D02 57.97、D01 50.17
3. `read_snippet D02-3.1` → 15 天
4. `read_snippet D01-4.2` → 30 个自然日 → **发现冲突**
5. `search_docs`「制度解释…以…为准」→ 无制度解释文档
6. `ask_user` → 列出冲突并请求确认（未擅自选 15 天）

**09（新增 D03 后，同一问题）**

1-4 同上；第 5 步命中 D03（memo，32.78 分）
6. `read_snippet D03-1.1` → 「以《费用报销操作指引（2024 版）》为准」
7. `finish(answered)` → 15 天，引用 D03 + D02 + D01，`resolution=resolved_by_memo`

**06（工具故障恢复）**

1. `search_docs` → 失败（注入「检索服务超时」）
2. `search_docs` 原样重试 → 成功
3. `search_docs` + 同义词 → 34.07
4. `read_snippet D05-3.2` → 12 位 / 90 天
5. `finish(answered)`

## 4. 回放与离线 Mock 的边界

- `11_replay_offline` 使用 `data/replay/vpn_replay.json` 中**手写的 Mock 决策**，
  目的是验证 OpenAI 兼容决策器的解析、执行、引用审计闭环（真实调用需要 Key）。
- 回放**不会**根据工具结果动态调整，除非回放序列已经写了下一步；它不能作为「模型决策能力」的证据。
- CLI 默认请求 `openai`；未配置 key 时自动回退到 `mock`（`run.planner_fallback=true`，stderr 与 Trace 都会提示）。
  `mock` 是规则决策器，能体现「根据结果调整动作」，但同样**不代表真实模型**。

## 5. 单条结果怎么看

```powershell
# 人类可读（等价于打开 trace.md）
python run.py ask "报销期限是多久？"

# 完整 JSON（含 trace）
python run.py ask "报销期限是多久？" --json

# 重新审计某个输出
python run.py verify examples/02_reimbursement_conflict/output.json
```
