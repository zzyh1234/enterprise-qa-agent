"""生成第二版语料：在原语料基础上加入 D03《制度解释第 2 号》。

用途：验证「替换文档内容后，Agent 的答案与动作是否改变」。
执行：python scripts/make_v2_corpus.py
"""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

MEMO = {
    "doc_id": "D03",
    "title": "财务共享中心制度解释第 2 号",
    "doc_type": "memo",
    "version_date": "2024-07-01",
    "owner": "财务共享中心",
    "applies_to": "全体员工",
    "priority": 1,
    "tags": ["制度解释", "报销", "冲突处理"],
    "sections": [
        {
            "section_id": "D03-1.1",
            "heading": "关于报销时限条款的解释",
            "text": "《费用报销操作指引（2024 版）》是《员工费用报销管理规定》的实施细则。两者对同一事项规定不一致的，以《费用报销操作指引（2024 版）》为准，并由财务共享中心负责解释。",
        }
    ],
}


def main() -> None:
    src = ROOT / "data" / "documents.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    if not any(doc["doc_id"] == MEMO["doc_id"] for doc in data["documents"]):
        data["documents"].append(json.loads(json.dumps(MEMO, ensure_ascii=False)))
    data["corpus_version"] = "2025-06-01"
    data["note"] = (
        data["note"]
        + " 本文件为 v2 变体：新增 D03 制度解释，用于验证『替换文档内容后答案与动作是否改变』。"
    )
    out = ROOT / "data" / "documents_v2.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(data['documents'])} documents)")


if __name__ == "__main__":
    main()
