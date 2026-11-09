# 提交信息

| 项目 | 内容 |
| --- | --- |
| 题号 | 企业知识问答 Agent（企业制度/产品问答，要求引用、冲突处理、动态决策） |
| 仓库 URL | `<提交人填写 GitHub URL>` |
| 最终 commit SHA | `<push 后填写；本地 commit 见 git log -1>` |
| 运行入口 | `python run.py ask "报销期限是多久？"`（无需安装依赖） |
| 评审入口 | `README.md` 第 3 节；`examples/README.md`；`docs/03-evaluation.md` |
| 离线可用 | 是（默认 `mock` 决策器；真实模型需自备 key，命令见 README 第 2 节） |
| 私有仓库 | 若为私有仓库，请给评审账号授予 Read 权限 |

## 提交前自检

```powershell
python scripts/run_tests.py        # 期望 ran=64 failures=0 errors=0
python run.py batch                # 期望 11 个场景，状态与 examples/README.md 一致
python scripts/verify_examples.py  # 期望 11 个 [PASS]
```

不要提交：`.env`、真实 API Key、公司内部资料、个人信息。
