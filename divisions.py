#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多体系区划管理工具（纯 Python 标准库，单文件）。

用法:
    python3 divisions.py input.json          # 从文件读取
    cat input.json | python3 divisions.py    # 从标准输入读取

输入 JSON 格式:
{
  "systems":   [ {"name": "行政", "levels": ["省", "市", "县", "街道"]}, ... ],
  "divisions": [ {"system": "行政", "name": "杭州市", "level": "市",
                  "parent": "浙江省",            # 同体系直接写名称；跨体系写 "体系.名称"
                  "extent": [[1, 1], [4, 4]],   # 矩形 [x1,y1,x2,y2] 或矩形列表
                  "refs": ["统计.杭州片区"]}, ... ],   # 可选：引用其他区划
  "operations": [
      {"type": "add", "system": "...", "name": "...", "level": "...",
       "parent": "...", "extent": ..., "refs": [...]},
      {"type": "delete",  "system": "...", "name": "..."},
      {"type": "reparent","system": "...", "name": "...", "new_parent": "..."}
  ]
}

跨体系引用规则（自定）:
    区划可通过 refs 引用其他区划，允许跨体系引用，但被引用区划必须存在；
    删除仍被引用的区划是错误，需先解除引用。
    理由：多体系并存的意义正在于体系间建立语义关联（如统计片区对应行政市），
    引用是"语义关联"而非"结构归属"，故不受"归属限本体系"约束；
    但为保证引用完整性，不允许悬挂引用，否则下游统计口径会静默失效。
"""

import argparse
import json
import sys
from collections import defaultdict


# ---------------------------------------------------------------- 工具函数

def make_key(system, name):
    return (system, name)


def parse_ref(default_system, raw):
    """把 '名称' / '体系.名称' / {"system":..,"name":..} 统一成 (system, name, explicit)。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return (raw.get("system", default_system), raw.get("name"), True)
    if isinstance(raw, str):
        if "." in raw:
            system, name = raw.split(".", 1)
            return (system, name, True)
        return (default_system, raw, False)
    return (default_system, str(raw), False)


def normalize_extent(extent):
    """范围统一为矩形列表 [[x1,y1,x2,y2], ...]；非法输入返回 None。"""
    if extent is None:
        return []
    if (isinstance(extent, list) and len(extent) == 2
            and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in extent)):
        extent = [extent]
    rects = []
    for rect in extent:
        try:
            (x1, y1), (x2, y2) = rect
            x1, x2 = min(x1, x2), max(x1, x2)
            y1, y2 = min(y1, y2), max(y1, y2)
            rects.append([x1, y1, x2, y2])
        except (TypeError, ValueError):
            return None
    return rects


def rects_overlap(a, b):
    """两矩形存在正面积交集（仅贴边不算重叠）。"""
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


# ---------------------------------------------------------------- 上下文

def new_context():
    return {
        "systems": {},      # 体系名 -> 层级列表
        "divisions": {},    # (体系, 名称) -> 区划记录
        "errors": [],       # 错误清单
    }


def report(ctx, etype, message, **detail):
    entry = {"type": etype, "message": message}
    entry.update(detail)
    ctx["errors"].append(entry)


def load_systems(ctx, systems):
    for item in systems or []:
        name = item.get("name")
        levels = item.get("levels") or []
        if not name:
            report(ctx, "system_invalid", "体系定义缺少名称")
            continue
        if name in ctx["systems"]:
            report(ctx, "system_duplicate", "体系重复定义: %s" % name, system=name)
            continue
        ctx["systems"][name] = list(levels)


def check_division_basics(ctx, rec):
    """定义层面的校验（体系存在、层级合法、范围格式）。"""
    key = make_key(rec["system"], rec["name"])
    if rec["system"] not in ctx["systems"]:
        report(ctx, "system_unknown",
               "区划 %s 所属体系未定义: %s" % (rec["name"], rec["system"]),
               division=rec["name"], system=rec["system"])
    levels = ctx["systems"].get(rec["system"], [])
    if levels and rec.get("level") not in levels:
        report(ctx, "level_unknown",
               "区划 %s.%s 层级 '%s' 不在体系层级定义中" % (rec["system"], rec["name"], rec.get("level")),
               division=rec["name"], system=rec["system"], level=rec.get("level"))
    if rec.get("extent") is None and rec.get("extent_raw") is not None:
        report(ctx, "extent_invalid",
               "区划 %s.%s 范围格式非法" % (rec["system"], rec["name"]),
               division=rec["name"], system=rec["system"])
    return key


def build_record(system, raw):
    return {
        "system": system,
        "name": raw.get("name"),
        "level": raw.get("level"),
        "parent_raw": raw.get("parent"),
        "extent_raw": raw.get("extent"),
        "extent": normalize_extent(raw.get("extent")),
        "refs": list(raw.get("refs") or []),
    }


def load_divisions(ctx, divisions):
    for raw in divisions or []:
        system = raw.get("system")
        name = raw.get("name")
        if not system or not name:
            report(ctx, "division_invalid", "区划定义缺少体系或名称: %r" % (raw,))
            continue
        key = make_key(system, name)
        if key in ctx["divisions"]:
            report(ctx, "division_duplicate",
                   "区划重复定义: %s.%s（保留首次定义）" % (system, name),
                   division=name, system=system)
            continue
        rec = build_record(system, raw)
        ctx["divisions"][key] = rec
        check_division_basics(ctx, rec)


# ---------------------------------------------------------------- 结构校验

def parent_key_of(rec):
    """返回 (目标key 或 None, 是否显式跨体系)。"""
    parsed = parse_ref(rec["system"], rec.get("parent_raw"))
    if parsed is None:
        return None, False
    system, name, explicit = parsed
    if name is None:
        return None, False
    return make_key(system, name), explicit and system != rec["system"]


def validate_structure(ctx):
    """归属限本体系、上级存在、层级衔接、归属环、跨体系范围重叠、引用完整性。"""
    divisions = ctx["divisions"]
    edges = {}  # key -> parent key（含非法跨体系边，用于跨体系环检测）

    for key, rec in sorted(divisions.items()):
        pkey, cross = parent_key_of(rec)
        if pkey is None:
            continue
        edges[key] = pkey
        if cross:
            report(ctx, "parent_cross_system",
                   "跨体系归属: %s.%s 的上级不能是 %s.%s" % (key + pkey),
                   division=key[1], system=key[0],
                   parent=pkey[1], parent_system=pkey[0])
        if pkey not in divisions:
            report(ctx, "parent_missing",
                   "上级不存在: %s.%s 的上级 '%s' 未定义或已删除"
                   % (key[0], key[1], rec.get("parent_raw")),
                   division=key[1], system=key[0], parent=rec.get("parent_raw"))
            continue
        # 层级衔接：上级层级应紧邻本区划层级之上
        levels = ctx["systems"].get(key[0], [])
        parent = divisions[pkey]
        if (not cross and levels and rec.get("level") in levels
                and parent.get("level") in levels):
            if levels.index(parent["level"]) != levels.index(rec["level"]) - 1:
                report(ctx, "level_skip",
                       "层级不衔接: %s.%s(%s) 的上级 %s(%s) 不是紧邻上一层级"
                       % (key[0], key[1], rec["level"], pkey[1], parent["level"]),
                       division=key[1], system=key[0], parent=pkey[1])

    # 归属环检测（每个节点至多一个上级，沿指针走即可；含跨体系边）
    state = {}  # 0=未访问 1=在路径上 2=已完成
    for start in edges:
        if state.get(start):
            continue
        path, node = [], start
        while node in edges and state.get(node, 0) == 0:
            state[node] = 1
            path.append(node)
            node = edges[node]
        if node in edges and state.get(node) == 1:
            cycle = path[path.index(node):]
            names = ["%s.%s" % k for k in cycle]
            report(ctx, "parent_cycle",
                   "检测到归属环: %s" % " -> ".join(names + [names[0]]),
                   members=names)
        for n in path:
            state[n] = 2

    # 跨体系范围重叠（报告区划对与体系对）
    keys = sorted(divisions)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            k1, k2 = keys[i], keys[j]
            if k1[0] == k2[0]:
                continue
            r1, r2 = divisions[k1]["extent"], divisions[k2]["extent"]
            if any(rects_overlap(a, b) for a in r1 for b in r2):
                report(ctx, "extent_overlap",
                       "跨体系范围重叠: %s.%s 与 %s.%s（体系对: %s / %s）"
                       % (k1[0], k1[1], k2[0], k2[1], k1[0], k2[0]),
                       divisions=["%s.%s" % k1, "%s.%s" % k2],
                       systems=sorted({k1[0], k2[0]}))

    # 引用完整性（跨体系引用允许，但目标必须存在）
    for key, rec in sorted(divisions.items()):
        for raw_ref in rec["refs"]:
            rsys, rname, _ = parse_ref(rec["system"], raw_ref)
            if make_key(rsys, rname) not in divisions:
                report(ctx, "ref_missing",
                       "引用目标不存在: %s.%s 引用了 %s.%s"
                       % (key[0], key[1], rsys, rname),
                       division=key[1], system=key[0], ref="%s.%s" % (rsys, rname))


# ---------------------------------------------------------------- 操作流

def op_add(ctx, op):
    system, name = op.get("system"), op.get("name")
    if not system or not name:
        report(ctx, "op_invalid", "增操作缺少体系或名称: %r" % (op,))
        return
    key = make_key(system, name)
    if key in ctx["divisions"]:
        report(ctx, "division_duplicate",
               "增操作失败，区划已存在: %s.%s" % (system, name),
               division=name, system=system)
        return
    rec = build_record(system, op)
    ctx["divisions"][key] = rec
    check_division_basics(ctx, rec)


def op_delete(ctx, op):
    key = make_key(op.get("system"), op.get("name"))
    if key not in ctx["divisions"]:
        report(ctx, "op_target_missing",
               "删操作失败，区划不存在: %s.%s" % key,
               division=key[1], system=key[0])
        return
    # 删除前检查：下级悬挂、被引用
    for ckey, rec in sorted(ctx["divisions"].items()):
        pkey, _ = parent_key_of(rec)
        if pkey == key:
            report(ctx, "child_dangling",
                   "删除导致下级悬挂: %s.%s 的下级 %s.%s 失去上级"
                   % (key[0], key[1], ckey[0], ckey[1]),
                   deleted="%s.%s" % key, child="%s.%s" % ckey)
        for raw_ref in rec["refs"]:
            rsys, rname, _ = parse_ref(rec["system"], raw_ref)
            if make_key(rsys, rname) == key:
                report(ctx, "ref_dangling",
                       "删除被引用的区划: %s.%s 仍被 %s.%s 引用，需先解除引用"
                       % (key[0], key[1], ckey[0], ckey[1]),
                       deleted="%s.%s" % key, referenced_by="%s.%s" % ckey)
    del ctx["divisions"][key]


def op_reparent(ctx, op):
    key = make_key(op.get("system"), op.get("name"))
    if key not in ctx["divisions"]:
        report(ctx, "op_target_missing",
               "改归属操作失败，区划不存在: %s.%s" % key,
               division=key[1], system=key[0])
        return
    if "new_parent" not in op:
        report(ctx, "op_invalid", "改归属操作缺少 new_parent: %r" % (op,))
        return
    ctx["divisions"][key]["parent_raw"] = op.get("new_parent")
    # 级联更新无需逐点维护：深度/路径在输出时按最新归属链统一重算，
    # 本体系全部下级自动随之更新。


OPS = {"add": op_add, "delete": op_delete, "reparent": op_reparent,
       "增": op_add, "删": op_delete, "改归属": op_reparent}


def apply_operations(ctx, operations):
    for op in operations or []:
        handler = OPS.get(op.get("type"))
        if handler is None:
            report(ctx, "op_invalid", "未知操作类型: %r" % (op.get("type"),))
            continue
        handler(ctx, op)


# ---------------------------------------------------------------- 状态输出

def compute_path(ctx, key):
    """沿上级链计算路径；遇环或上级缺失则标记不完整。"""
    divisions = ctx["divisions"]
    names, seen, node, intact = [], set(), key, True
    while True:
        if node in seen:
            intact = False
            break
        seen.add(node)
        rec = divisions.get(node)
        if rec is None:
            intact = False
            break
        names.append(rec["name"])
        pkey, cross = parent_key_of(rec)
        if pkey is None:
            break
        if cross or pkey not in divisions:
            intact = False
            break
        node = pkey
    names.reverse()
    return names, intact


def build_state(ctx):
    state = {}
    by_system = defaultdict(list)
    for key, rec in ctx["divisions"].items():
        by_system[key[0]].append((key, rec))
    for system in ctx["systems"]:
        rows = []
        for key, rec in sorted(by_system.get(system, [])):
            path, intact = compute_path(ctx, key)
            parent = rec.get("parent_raw")
            rows.append({
                "name": rec["name"],
                "level": rec.get("level"),
                "parent": parent,
                "depth": (len(path) - 1) if intact else None,
                "path": "/".join(path) + ("" if intact else "  [!链断裂或成环]"),
                "refs": rec["refs"],
            })
        rows.sort(key=lambda r: (r["depth"] is None, r["depth"] or 0, r["name"]))
        state[system] = rows
    return state


# ---------------------------------------------------------------- 入口

def main(argv=None):
    parser = argparse.ArgumentParser(description="多体系区划管理工具")
    parser.add_argument("input", nargs="?", help="输入 JSON 文件（缺省读标准输入）")
    args = parser.parse_args(argv)

    try:
        text = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"errors": [{"type": "input_invalid",
                                      "message": "输入解析失败: %s" % exc}]},
                         ensure_ascii=False, indent=2))
        return 2

    ctx = new_context()
    load_systems(ctx, data.get("systems"))
    load_divisions(ctx, data.get("divisions"))
    apply_operations(ctx, data.get("operations"))
    validate_structure(ctx)

    output = {
        "errors": ctx["errors"],
        "state": build_state(ctx),
        "summary": {
            "systems": len(ctx["systems"]),
            "divisions": len(ctx["divisions"]),
            "errors": len(ctx["errors"]),
        },
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
