"""复现并验证 11148「tool calls and tool results do not match」。

假设：当同一轮有 **多个** 工具调用、且它们的输出里含图片时，
`_convert_input_items` 会在每条 tool 消息后立刻插入一条 user 消息，
把后续的 tool 结果与 assistant.tool_calls 隔开 → 上游判配对失败。

用法: .venv/Scripts/python.exe _t/repro_11148.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gtwb.responses_adapter import _convert_input_items  # noqa: E402

IMG_A = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
IMG_B = {"type": "input_image", "image_url": "data:image/png;base64,BBBB"}


def build(calls):
    """calls: [(call_id, name, output), ...] —— 先全部 call，再全部 output（并行语义）"""
    items = [{"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "look at these"}]}]
    for cid, name, _ in calls:
        items.append({"type": "function_call", "call_id": cid, "name": name, "arguments": "{}"})
    for cid, name, out in calls:
        items.append({"type": "function_call_output", "call_id": cid, "output": out})
    return items


def shape(msgs):
    return [(m["role"], m.get("tool_call_id", "")) for m in msgs]


def diagnose(label, msgs, declared):
    """按 Chat 协议校验：assistant.tool_calls 之后必须紧跟同等数量的 tool 消息。"""
    seq = shape(msgs)
    print(f"\n── {label} ──")
    for i, (r, tid) in enumerate(seq):
        extra = ""
        m = msgs[i]
        if r == "assistant" and m.get("tool_calls"):
            extra = f"  tool_calls={[t['id'] for t in m['tool_calls']]}"
        if r == "user" and isinstance(m.get("content"), list):
            extra = f"  parts={[p.get('type') for p in m['content']]}"
        print(f"    [{i}] {r:<10} {tid}{extra}")

    # 校验：assistant(tool_calls=N) 后面连续 N 条 tool
    ok = True
    i = 0
    while i < len(seq):
        if seq[i][0] == "assistant" and msgs[i].get("tool_calls"):
            n = len(msgs[i]["tool_calls"])
            ids = [t["id"] for t in msgs[i]["tool_calls"]]
            follow = seq[i + 1:i + 1 + n]
            got = [f[1] for f in follow if f[0] == "tool"]
            if len(follow) != n or any(f[0] != "tool" for f in follow):
                ok = False
                print(f"    ✗ assistant 声明 {n} 个 tool_call {ids}，"
                      f"但紧随的 {len(follow)} 条是 {follow}")
            elif got != ids:
                ok = False
                print(f"    ✗ tool_call 顺序不匹配：声明 {ids}，实到 {got}")
        i += 1
    print(f"    {'✓ 配对合法' if ok else '✗ 配对被破坏 —— 上游会报 11148'}")
    return ok


def main():
    print("=" * 68)
    r1 = diagnose("场景1：单个 view_image（输出含图片）",
                  _convert_input_items(build([("A", "view_image", [IMG_A])])), 1)

    r2 = diagnose("场景2：两个并行 view_image（输出各含图片）★ 怀疑点",
                  _convert_input_items(build([("A", "view_image", [IMG_A]),
                                              ("B", "view_image", [IMG_B])])), 2)

    r3 = diagnose("场景3：两个并行 exec_command（纯文本输出）",
                  _convert_input_items(build([("A", "exec_command", "ok"),
                                              ("B", "exec_command", "ok")])), 2)

    r4 = diagnose("场景4：两个并行，只有第一个带图",
                  _convert_input_items(build([("A", "view_image", [IMG_A]),
                                              ("B", "view_image", "no image")])), 2)

    print("\n" + "=" * 68)
    print(f"结论：单图={'合法' if r1 else '破坏'}  "
          f"双图={'合法' if r2 else '破坏'}  "
          f"纯文本并行={'合法' if r3 else '破坏'}  "
          f"混合={'合法' if r4 else '破坏'}")
    if not r2 or not r4:
        print(">>> 复现成功：并行工具调用 + 图片输出 → 配对被 user 消息隔断")


if __name__ == "__main__":
    main()
