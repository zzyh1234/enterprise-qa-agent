# 01 需求说明

## 1. 背景与用户

企业制度、流程、产品 FAQ 分散在多个文档里，员工想问的是很具体的一句话：
「报销期限多久」「VPN 多久要重新认证」「发票丢了还能不能报」。
通用大模型能回答得像模像样，但**无法保证答案来自公司现行制度**，也无法指出冲突。

目标用户：

- 一线员工：需要快速得到可执行的答案，并知道依据哪份制度、哪个版本。
- 制度归口部门（财务/HR/IT）：需要看到答案的引用原文与冲突处理过程，便于纠错。
- 知识库维护者：希望系统在资料不足或工具故障时明确说「不知道」，而不是编造。

## 2. 任务与输入输出

输入：

- `question`：用户问题（中文，允许口语化）。
- `history`：可选对话历史（上一轮问题与回答）。
- `prior_result`：可选上一轮完整结果（用于冲突确认、追问）。
- 语料：`data/documents.json`，每篇文档含 `doc_id / title / doc_type / version_date / owner /
  applies_to / priority / sections[]`；每个片段含 `section_id / heading / text`。

输出（`examples/<id>/output.json` 的字段）：

| 字段 | 说明 |
| --- | --- |
| `status` | 任务状态，见 README 第 6 节 |
| `answer` | 面向用户的回答；资料不足/冲突时说明原因，而不是硬答 |
| `citations[]` | `doc_id / title / section_id / heading / version_date / quote / supports` |
| `evidence_gaps[]` | 资料缺口、限制、无法核验的原因 |
| `conflicts[]` | 冲突主题、各文档取值、`resolution`（unresolved / resolved_by_memo / user_selected） |
| `clarification` | 需要用户确认的问题与可选项 |
| `trace[]` | 每步：决策依据、动作、参数、返回摘要、后续动作 |
| `run` | planner、模型名、语料路径、预算用量、耗时、失败与备注 |
| `audit` | 引用逐字校验、冲突说明校验、整体是否通过 |

## 3. 范围内

- 文档检索（`search_docs`）与片段读取（`read_snippet`）两个业务工具。
- 至少一个完整 ReAct 循环：决策 → 工具 → 观察 → 调整/结束。
- 文档冲突：先查证制度解释/优先级，查不到则追问，不擅自选一个。
- 资料外问题：明确拒绝并列出缺口。
- 工具失败：瞬时重试、持续失败返回未完成状态。
- 执行上限：步数与工具调用次数，超限有明确未完成状态。
- 可复现的 Trace、实际运行样例、自动化测试、评测文档。

## 4. 范围外（本版不做）

- Web UI / 聊天前端、线上部署、鉴权与多租户。
- 多 Agent 协作、向量数据库、生产级 Runtime、流式输出。
- 真实模型的线上效果评测（本环境无 API Key；真实模型接入代码与离线协议测试已完成）。
- 文档解析（PDF/Word/HTML）、增量索引与权限过滤。

## 5. 成功标准（可验证）

| 编号 | 标准 | 验证方式 |
| --- | --- | --- |
| S1 | 对有明确依据的问题给出答案，且每条引用逐字来自原文 | `examples/01`、`04`、`05`、`06`、`11`；`audit.citations_supported=true` |
| S2 | 发现 30 天 vs 15 天冲突时列出双方取值与引用，并请求确认 | `examples/02`；`status=needs_clarification` |
| S3 | 用户指定制度后给出答案，同时保留冲突说明 | `examples/03`；`conflicts[0].resolution=user_selected` |
| S4 | 口语化/相似问法仍能召回正确文档 | `examples/04`、`05`；测试 `test_retrieval_rewrite_...` |
| S5 | 资料外问题不编造 | `examples/10`；`citations=[]`、`status=insufficient_evidence` |
| S6 | 工具失败可恢复，或明确未完成且不无依据作答 | `examples/06`、`07` |
| S7 | 超过步数/工具调用上限时返回明确未完成状态 | `examples/08`；`budget.exceeded=true` |
| S8 | 替换文档内容后，答案与动作随之改变 | `examples/02` vs `09`；`test_answer_follows_changed_document_content` |
| S9 | 无第三方依赖、离线可运行、命令可复制 | `python run.py ask "…"`、`python scripts/run_tests.py` |

## 6. 验收点 ↔ 场景

| 验收点 | 场景 |
| --- | --- |
| 明确答案 | 01、06、11 |
| 文档冲突 | 02、09 |
| 资料外问题 | 10 |
| 改写后的相似问题 | 04、05 |
| 上下文追问 | 03 |
| 工具失败 | 06（瞬时）、07（持续） |
| 执行上限 | 08 |

同一场景可覆盖多个验收点；详细结果见 `docs/03-evaluation.md`。
