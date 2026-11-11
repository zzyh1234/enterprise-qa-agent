# 04 AI 辅助说明

> 本仓库在 AI（Cline）辅助下完成初版。提交前请提交人复核本文第 4 节的清单，
> 并按实际情况填写用时与 GitHub 信息。AI 生成的代码已通过 69 个自动化测试和 11 个场景的引用审计，
> 但「测试通过」不等于「业务正确」，仍需人工抽查。

## 1. AI 参与的部分

| 部分 | AI 做了什么 | 人工需要确认什么 |
| --- | --- | --- |
| 需求拆解 | 把题目中的验收点拆成 11 个可运行场景与 74 个断言 | 验收点是否有遗漏、场景是否符合真实业务 |
| 语料 | 编写合成的制度/FAQ（8 篇文档、冲突对 D01/D02、v2 的 D03） | 制度措辞、版本号、适用范围是否合理 |
| 代码 | 原生 ReAct 循环、BM25+改写、两个工具、三种 Planner、审计、CLI、runner | 是否有安全/合规问题；异常路径是否可接受 |
| 测试 | unittest 全量覆盖（含用假 transport 驱动的真实模型协议循环） | 断言是否过拟合当前语料 |
| 文档 | 需求/方案/评测/AI 使用/复盘与 README | 数字与命令是否与实际运行一致（已逐条运行） |
| 调试记录 | 见第 3 节 | — |

## 2. AI 使用边界（明确声明）

- **没有**把模型的「常识」当作答案来源：所有回答必须经过 `read_snippet` 的原文引用与审计。
- **没有**调用真实大模型 API（本环境无 Key）；`mock` 只验证流程与护栏，**不证明真实模型决策效果**。
- **没有**使用真实企业资料或个人隐私：语料全部合成，电话/金额/机构名均为虚构。
- **没有**提交任何密钥：`.env.example` 只有占位符，`.env` 已加入 `.gitignore`。

## 3. 实现过程中的关键修改（都有测试兜底）

这些不是「一次写对」，而是运行 Trace 后发现并修掉的问题，保留了过程以便复核：

1. **假阳性证据**：`D01-4.3` 以《票据遗失与替代凭证处理说明》形式引用了另一份文件，
   朴素关键词匹配把「遗失」当成证据，导致回答引用了错误的条款。
   修复：判定证据前先剔除 `《…》` 内容。测试：`test_cross_reference_alone_is_not_evidence`；
   示例：`examples/04_receipt_lost_paraphrase`。
2. **冲突可解但答案没提对方取值**：09 场景首次运行 `status=audit_failed`，
   因为答案只写「D01 与本答案不同」而没写出 30。修复：答案中显式写出冲突文档的取值。
   测试：`test_corpus_change_changes_answer_and_actions`。
3. **追问选项丢失**：`ask_user` 的嵌套 `clarification` 没有被正确读取，导致 `options=[]`。
   修复：优先读取嵌套字段，再回退到顶层字段。示例：`examples/02_reimbursement_conflict`。
4. **年假分档匹配错误**：正则 `满 (\d+) 年` 会匹配到「不满 10 年」里的「满 10 年」，
   25 年工龄被算成 5 天。修复：加负向前瞻 `(?<!不)满`。测试：`test_annual_leave_tier_and_year_parsing`。
5. **预算状态文案重复**：`工具调用上限：工具调用上限 1 已用尽`。修复：统一由 `exceeded_reason` 生成。
6. **真实模型提示词缺少观察摘要**：模型只能看到 JSON payload，看不到工具摘要。
   修复：prompt 同时给出 summary 与 payload；测试断言第二次请求包含上一步观察。

## 4. 提交前请人工确认（必须）

- [ ] README 第 8 节的「提交人实际用时」按真实情况填写，不要照抄 AI 估计。
- [ ] 运行一遍：`python scripts/run_tests.py`、`python run.py batch --planner mock`、`python scripts/verify_examples.py`，
      确认与文档中的数字一致（应为 74 / 11 / 11 PASS）。
- [ ] 如你有模型 API Key：执行 `python run.py batch --planner openai --out examples_openai`，
      复核 `run.planner=openai`、11 个场景状态与 `verify` 全 PASS 后，再替换 `examples/`，
      并把 `SUBMISSION.md`、`docs/03-evaluation.md` 中的「当前 Trace 来源」改为真实模型。
      **没有真实运行就不要把 mock Trace 写成 openai Trace。**
- [ ] 抽查 2 条引用：打开 `data/documents.json` 对应片段，确认 `quote` 逐字一致。
- [ ] 如你有模型 API Key，可补一条真实模型运行记录（`--planner openai`），并在 `docs/03` 中注明；
      若无，请保留「未验证真实模型」的声明，不要删除。
- [ ] 填写题号、GitHub URL、commit SHA；私有仓库给评审账号授权。
- [ ] 确认没有把 `.env`、真实密钥、公司内部资料提交进仓库。

## 5. 使用的 AI 交互方式（可复用）

- 用「验收点 → 场景 → 断言」三层拆解任务，先写失败用例，再补实现。
- 要求 AI 每条结论都落到可执行命令或文件路径，不接受「应该可以」。
- 让 AI 用真实运行结果回填文档（本仓库的分数、状态、工具调用次数均来自实际运行）。
- 对 AI 生成的规则保持怀疑：用「替换语料」的方式验证它是否真的在依据文档，而不是记住答案。
