"""Validation Agent — TA-friendly CLI (paste LinkedIn school info).

最常用:
  python scripts/validate_agent.py list
  python scripts/validate_agent.py resume 1
      → 按提示粘贴 LinkedIn「教育经历」文字，空一行结束

也可:
  python scripts/validate_agent.py resume github:123 --education "Stanford BS CS 2019"
  python scripts/validate_agent.py resume 1 --file school.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.validation_agent import list_pending, load_queue, resume_validation


def _pending_sorted() -> list[dict]:
    return sorted(
        list_pending(),
        key=lambda x: (-int(x.get("score") or 0), str(x.get("dedup_key") or "")),
    )


def _resolve_key(key_or_num: str) -> str:
    """Allow '1' from list numbering, or full 'github:123' key."""

    key_or_num = (key_or_num or "").strip()
    pending = _pending_sorted()
    if key_or_num.isdigit():
        idx = int(key_or_num)
        if idx < 1 or idx > len(pending):
            raise KeyError(f"序号 {idx} 不存在。请先运行 list，看清 1、2、3…")
        return str(pending[idx - 1].get("dedup_key"))
    if any(p.get("dedup_key") == key_or_num for p in pending):
        return key_or_num
    # still allow resume of exact key even if missing? resume_validation will KeyError
    return key_or_num


def _read_education_interactive(candidate_name: str) -> str:
    print()
    print("=" * 60)
    print(f"  为 【{candidate_name}】 粘贴学校 / 教育信息")
    print("=" * 60)
    print()
    print("从哪里复制？")
    print("  1. 打开对方 LinkedIn")
    print("  2. 找到「教育经历 Education」区块（也可带一点 About）")
    print("  3. 用鼠标选中文字 → Ctrl+C 复制")
    print()
    print("粘贴到哪里？")
    print("  → 就粘贴在下面这个黑窗口里（Ctrl+V）")
    print("  → 可以贴多行")
    print("  → 贴完后：空一行，再按一次回车；或单独一行打 END 再回车")
    print()
    print("示例（直接粘贴类似内容即可）:")
    print("  Stanford University")
    print("  Bachelor of Science, Computer Science")
    print("  2015 – 2019")
    print()
    print("-" * 60)
    print("开始粘贴 ↓")
    print("-" * 60)

    lines: list[str] = []
    empty_streak = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "END":
            break
        if line.strip() == "":
            empty_streak += 1
            if empty_streak >= 1 and lines:
                break
            continue
        empty_streak = 0
        lines.append(line)

    text = "\n".join(lines).strip()
    if not text:
        print("\n没有读到内容。请重新运行 resume，并粘贴教育经历。")
    return text


def cmd_list(_: argparse.Namespace) -> int:
    pending = _pending_sorted()
    if not pending:
        print()
        print("目前没有「待验证学校」的候选人。")
        print("等 nightly / Actions 跑完后，有 school unverified 的人会出现在这里。")
        print()
        return 0

    print()
    print(f"待你验证学校的候选人：共 {len(pending)} 人")
    print("（Agent 已暂停，等你从 LinkedIn 贴教育经历）")
    print()
    for i, item in enumerate(pending, start=1):
        cand = item.get("candidate") or {}
        name = cand.get("name") or "?"
        score = item.get("score", "?")
        role = item.get("role_title") or cand.get("role") or ""
        url = cand.get("profile_url") or ""
        key = item.get("dedup_key") or ""
        print(f"  [{i}] {name}")
        print(f"      分数: {score}  |  职位: {role}")
        print(f"      主页: {url}")
        print(f"      编号: {key}")
        print(f"      下一步: 打开 LinkedIn 查教育经历，然后运行:")
        print(f"             python scripts/validate_agent.py resume {i}")
        print()

    print("提示: resume 后面写列表里的序号即可，例如 resume 1")
    print()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        key = _resolve_key(args.key)
    except KeyError as exc:
        print(exc)
        return 1
    q = load_queue()
    item = q.get("pending", {}).get(key)
    if not item:
        print(f"队列里找不到: {key}")
        return 1
    cand = item.get("candidate") or {}
    print()
    print(f"姓名: {cand.get('name')}")
    print(f"分数: {item.get('score')}")
    print(f"职位: {item.get('role_title')}")
    print(f"主页: {cand.get('profile_url')}")
    print(f"编号: {key}")
    print(f"Agent 想法: {item.get('thought', '')}")
    print(f"请你提供: {item.get('question', '')}")
    print()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    try:
        key = _resolve_key(args.key)
    except KeyError as exc:
        print(exc)
        return 1

    q = load_queue()
    item = q.get("pending", {}).get(key)
    if not item:
        print(f"队列里找不到: {key}")
        print("先运行: python scripts/validate_agent.py list")
        return 1

    name = (item.get("candidate") or {}).get("name") or key
    education = (args.education or "").strip()

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"找不到文件: {path}")
            print("可以新建一个 school.txt，把 LinkedIn 教育经历粘贴进文件再保存。")
            return 1
        education = path.read_text(encoding="utf-8").strip()

    if not education:
        education = _read_education_interactive(name)

    if not education:
        return 2

    print()
    print(f"正在交给 Agent 验证【{name}】的学校信息…")
    try:
        final = resume_validation(key, education)
    except KeyError as exc:
        print(exc)
        return 1

    status = final.get("status")
    score = final.get("score")
    print()
    print("-" * 40)
    if status == "ready":
        print(f"结果: 通过验证 ✅  新分数 {score}")
        print("可以按 Ready 候选人跟进 / outreach。")
    elif status == "rejected":
        print(f"结果: 仍未达标 ❌  新分数 {score}")
        print("学校信息补上后仍不够 bar，先不要优先推。")
    elif status == "needs_validation":
        print("结果: 还需要更多信息（例如更完整的教育经历）。")
    else:
        print(f"结果: {status}  分数 {score}")
    print(f"Agent 说明: {final.get('thought', '')}")
    print("-" * 40)
    print()
    return 0


def cmd_how(_: argparse.Namespace) -> int:
    print(
        """
══════════════════════════════════════════
  学校信息贴在哪里？怎么贴？（给 TA）
══════════════════════════════════════════

【从哪里复制】
  打开候选人 LinkedIn → 找到「教育经历 / Education」
  把学校名、学位、时间选中，Ctrl+C 复制
  （有名校关键词最好：MIT / Stanford / CMU / Berkeley / UCLA /
   Cornell / UIUC / Michigan / Duke 等）

【贴到哪里】—— 三种方式任选

  方式 A（推荐，最简单）
    1) python scripts/validate_agent.py list
    2) python scripts/validate_agent.py resume 1
    3) 在黑窗口里 Ctrl+V 粘贴，空一行再回车

  方式 B（一行命令）
    python scripts/validate_agent.py resume 1 --education "Stanford BS CS 2019"

  方式 C（先贴进记事本）
    1) 新建 school.txt，粘贴教育经历，保存
    2) python scripts/validate_agent.py resume 1 --file school.txt

【不要贴在】
  ✗ 不要贴进 GitHub 网页
  ✗ 不要贴进报告 HTML
  ✗ 不要只发 Slack 却不跑命令（Agent 读不到）

贴完后 Agent 会自动重新打分，并告诉你 Ready 还是未达标。
"""
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="验证候选人学校信息（LinkedIn 教育经历）— Validation Agent",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="查看待验证名单").set_defaults(func=cmd_list)
    sub.add_parser("how", help="说明：学校信息贴在哪里").set_defaults(func=cmd_how)

    p_show = sub.add_parser("show", help="查看某个人的详情")
    p_show.add_argument("key", help="列表序号 1 或 github:123")
    p_show.set_defaults(func=cmd_show)

    p_res = sub.add_parser("resume", help="粘贴学校信息并让 Agent 继续")
    p_res.add_argument("key", help="列表序号 1 或 github:123")
    p_res.add_argument(
        "--education",
        default="",
        help="可选：一行写完教育经历；不填则进入粘贴模式",
    )
    p_res.add_argument(
        "--file",
        default="",
        help="可选：从文本文件读取教育经历（如 school.txt）",
    )
    p_res.set_defaults(func=cmd_resume)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
