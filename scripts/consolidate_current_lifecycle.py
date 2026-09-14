"""Consolidate known duplicate lifecycle records created during historical import."""

import argparse
from pathlib import Path

from novelagent.trace.file_lifecycle import FileLifecycleStore


P6448 = "6448aaa5-3539-4ced-a749-5597075897da"
P5D = "5d737335-983b-4018-9041-ebee34080818"


def remove(files: FileLifecycleStore, record: dict) -> None:
    (files.project / record["file_path"]).unlink(missing_ok=True)


def merge(files: FileLifecycleStore, layer: str, target_id: str, source_ids: list[str], *,
          claim: str, category: str | None = None, weight: float | None = None,
          promote: bool = False, apply: bool) -> None:
    records = [files.get(layer, record_id) for record_id in source_ids]
    if any(record is None for record in records):
        missing = [record_id for record_id, record in zip(source_ids, records) if record is None]
        raise RuntimeError(f"missing {layer}: {missing}")
    target = files.get(layer, target_id)
    if target is None:
        raise RuntimeError(f"missing target {target_id}")
    event_ids = list(dict.fromkeys(event for record in records for event in record.get("source_event_ids", [])))
    trace_ids = list(dict.fromkeys(trace for record in records for trace in [record.get("trace_id", ""), *record.get("trace_ids", [])] if trace))
    target.update(claim=claim, source_event_ids=event_ids, trace_ids=trace_ids,
                  support_count=len(event_ids), weight=weight if weight is not None else min(300, 15 * len(event_ids)))
    if category:
        target["category"] = category
    print({"layer": layer, "target": target_id, "records": len(records), "events": len(event_ids), "promote": promote})
    if not apply:
        return
    for record in records:
        if record["id"] != target_id:
            remove(files, record)
    if promote:
        files._move(target, "memory")
    else:
        old_path = files.project / target["file_path"]
        if category and category != files.get(layer, target_id).get("category"):
            old_path.unlink(missing_ok=True)
        files.write(layer, target)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    a = FileLifecycleStore("workspace", P6448)
    merge(a, "evidence", "evi_c9a1e20e46b247cba60e44d83099f0df", [
        "evi_33ff1f2028974807a36ab0ef86633a1f", "evi_391b55690d1d448d9a5456fc81fcffcf",
        "evi_3cdf29f8295545fdb6356bdf4d75aae8", "evi_472c4f3164074ae0a7126900ccf4bd18",
        "evi_4a65b86ef69e4d02a8bb40fd5b696cb0", "evi_6aaaa754a00c4dc8b39461cbdb77a1be",
        "evi_70b8fdc82ea542aaa5b3619b4d41fc2c", "evi_8dbdf7d5ff8f4f999800b38710913ebc",
        "evi_8dea41b630954c4c8c6b24d8de9d0dde", "evi_9adc6ba5d4f243d69c9d71afc8d677e7",
        "evi_bcf1805e012d4340b87dfd13d5111e4a", "evi_c443fafad90d48aaa69664241be87c13",
        "evi_c9a1e20e46b247cba60e44d83099f0df", "evi_e613c4dde32e450c9d6e7a9dd8490755",
    ], claim="用户多次质疑 Agent 调用 AskUserQuestion 的时机；后续应核查并优化主 Agent、SubAgent 的调度条件。", category="agent", promote=True, apply=args.apply)
    merge(a, "evidence", "evi_372944fdc371472189cabd8a5bd399f6", [
        "evi_372944fdc371472189cabd8a5bd399f6", "evi_5f797c6a5e464e6c8d5a27bf3e3bbf8e",
        "evi_9ae73763108e471c8f5e9be9719b2833", "evi_b4b3ac519d084161bbc422a3803f2095",
        "evi_2c7c5478119f4a64af7e749aac6a80b7",
    ], claim="用户多次反馈小说大纲规划提示词会在思考阶段截断；需核查指令冲突、工具限制与输出执行约束。", category="agent", apply=args.apply)
    merge(a, "memory", "mem_ba9832ba88894abfa51c3f3f9ddc76b5", [
        "mem_ba9832ba88894abfa51c3f3f9ddc76b5", "mem_985843654c0a479e81ddeb90b6cf34bc",
        "mem_a1fec7bda94c4ee7b3278a127bb92e6d",
    ], claim="大纲应先呈现混乱、死亡与绝望，再逐步呈现废墟中的生机和希望；过程中保留起伏与微光节点，避免长期单调下沉导致读者失去阅读动力。", weight=100, apply=args.apply)

    b = FileLifecycleStore("workspace", P5D)
    merge(b, "evidence", "evi_331664ebe51d42edab015deae03d2f29", [
        "evi_331664ebe51d42edab015deae03d2f29", "evi_71a1cc5197c6462f9af81df76251cf19",
        "evi_a8beee5e16a44a8593b35b61633f4051",
    ], claim="用户质疑 Agent 对 AskUserQuestion 工具的调用时机，需要在后续调度优化中核查。", category="agent", apply=args.apply)
    merge(b, "memory", "mem_88fa8be9e9f24b5681737cf4b1d5abd0", [
        "mem_5ba7941c33d64500927308d438a6eaee", "mem_88fa8be9e9f24b5681737cf4b1d5abd0",
    ], claim="《碎星纪元》前三章中，“苏棠”是误写，必须替换为反派的正确名字。", weight=65, apply=args.apply)


if __name__ == "__main__":
    main()
