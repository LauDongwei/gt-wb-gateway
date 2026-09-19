"""实测聚合搜索的可用性与质量。"""
import asyncio
import sys

sys.path.insert(0, r"D:\workbuddy\研究院\gt-wb-gateway")

from gtwb import websearch  # noqa: E402


class FakeCfg:
    web_search_proxy = ""
    web_search_timeout_s = 15.0
    web_search_engines = ["gnews", "cn_bing", "so360", "wiki"]
    web_search_api_key = ""


async def main():
    cfg = FakeCfg()
    for q in ["openai codex cli latest release", "国产绣花机品牌", "python 3.14 new features"]:
        print("=" * 78)
        print("QUERY:", q, " → 引擎:", websearch.resolve_engines(cfg))
        res = await websearch.web_search(cfg, q, count=8)
        print(f"→ 合并 {len(res)} 条")
        for i, r in enumerate(res, 1):
            print(f"  [{i}] ({r.get('engine')}) {r['title'][:78]}")
            print(f"      {r['url'][:105]}")
        print()

    print("=" * 78)
    print("给模型的 tool 输出预览：")
    res = await websearch.web_search(cfg, "openai codex cli latest release", count=4)
    print(websearch.format_results("openai codex cli latest release", res)[:1500])


if __name__ == "__main__":
    asyncio.run(main())
