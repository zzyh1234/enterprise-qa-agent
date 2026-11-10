# 提交信息

| 项目 | 内容 |
| --- | --- |
| 题号 | 企业知识问答 Agent（企业制度/产品问答，要求引用、冲突处理、动态决策） |
| 仓库 URL | `<提交人填写 GitHub URL>` |
| 最终 commit SHA | `<push 后填写；本地 commit 见 git log -1>` |
| 运行入口 | `python run.py ask "报销期限是多久？"`（无需安装依赖；默认请求真实模型，无 key 自动回退 mock 并标注） |
| 评审入口 | `README.md` 第 3 节；`examples/README.md`；`docs/03-evaluation.md` |
| 离线可用 | 是：`--planner mock` 无 key 跑通全部场景与测试。CLI 默认 `--planner openai`；未配置 `EQA_API_KEY` 时**自动回退 mock**，并在 `run.planner_requested=openai`、`run.planner_fallback=true`、stderr 警告与 `run.notes` 中如实标注 |
| examples/ 当前来源 | **mock 决策器**（本机无 API Key；`run.planner=mock`、`planner_fallback=true`）。真实模型 Trace 需自备 key，按下方命令生成并复核后再替换 |
| 私有仓库 | 若为私有仓库，请给评审账号授予 Read 权限 |

## 生成真实模型 Trace（替换当前 mock 示例）

```powershell
$env:EQA_API_KEY = "sk-..."        # 不要提交到仓库
$env:EQA_MODEL = "gpt-4o-mini"
python run.py batch --planner openai --out examples_openai
python run.py verify "examples_openai/*/output.json"
```

复核要点：

1. `examples_openai/*/output.json` 的 `run.planner` 必须是 `openai`，`planner_fallback` 必须是 `false`；
2. 11 个场景状态符合预期，`verify` 全部 PASS；
3. 真实模型可能不按预期选择动作（例如冲突时直接作答）——审计会降级为 `audit_failed`，
   这属于**需要修正提示词后重跑**的失败，不能直接替换示例。

通过后可用 `examples_openai/` 覆盖 `examples/`，并把上表「examples/ 当前来源」改为真实模型，
同时更新 `docs/03-evaluation.md` 的场景表（工具调用次数与答案措辞可能不同）。
**不要在没有真实运行的情况下把 mock Trace 写成 openai Trace**：`run.planner` 字段与回答风格会直接暴露来源。

## 提交前自检

```powershell
python scripts/run_tests.py        # 期望 ran=67 failures=0 errors=0
python run.py batch                # 期望 11 个场景，状态与 examples/README.md 一致
python scripts/verify_examples.py  # 期望 11 个 [PASS]
```

不要提交：`.env`、真实 API Key、公司内部资料、个人信息。
