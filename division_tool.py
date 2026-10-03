#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
division_tool.py — 多体系区划管理与校验工具（纯标准库，单文件）

输入格式（行文本，# 之后为注释，空行忽略）：

  # 体系定义：system 体系名 层级1 层级2 ... （层级按自顶向下顺序）
  system 民政 省 市 区县

  # 区划流：div 体系 名称 层级 上级 范围
  #   上级为 - 表示无上级（顶级区划）
  #   范围为 x1,y1,x2,y2 矩形，或 @体系:名称 / @名称 表示“范围同该区划”（跨体系引用）
  div 民政 浙江 省 - 0,0,100,100
  div 统计 华东 大区 - @民政:浙江

  # 调整操作流：
  op add 体系 名称 层级 上级 范围        # 增
  op delete 体系 名称                   # 删
  op reparent 体系 名称 新上级          # 改归属

跨体系引用规则（自定）：
  区划的范围可写为 @体系:名称，表示其范围“派生自”另一体系的区划。
  理由：跨体系的相关区划（如统计片区跟随民政市）常需保持一致范围，
  派生引用避免坐标重复维护造成漂移；引用为只读派生（目标范围变更自动传导），
  保证单一事实来源。引用目标必须存在（否则报 missing_ref），
  引用链不得成环（否则报 ref_cycle）。

输出：调整后区划状态（按体系树形展示，层级为级联修正后的值）+ 错误清单。

用法：python3 division_tool.py [输入文件]   （缺省读标准输入）
"""

import sys
from dataclasses import dataclass, field


@dataclass
class Division:
    system: str
    name: str
    level: str
    parent: str          # 上级名称，'-'/'' 表示无
    scope_spec: str      # 'x1,y1,x2,y2' 或 '@体系:名称' 或 '@名称'

    @property
    def key(self):
        return (self.system, self.name)


class Engine:
    def __init__(self):
        self.systems = {}          # 体系名 -> [层级...]
        self.divisions = {}        # (体系, 名称) -> Division
        self.ops = []              # 原始操作（按序）
        self.errors = []           # (类别, 描述)
        self.deleted = set()       # 被删除的 (体系, 名称)，用于区分“悬挂”与“未定义上级”
        self.scope_memo = {}       # 范围解析缓存（操作流应用完毕后统一解析）

    # ---------- 解析 ----------
    def err(self, kind, msg):
        self.errors.append((kind, msg))

    def parse(self, text):
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            tok = line.split()
            head = tok[0]
            try:
                if head == 'system':
                    name, levels = tok[1], tok[2:]
                    if not levels:
                        self.err('bad_system', f'第{lineno}行: 体系 {name} 未定义层级')
                        continue
                    if name in self.systems:
                        self.err('duplicate_system', f'第{lineno}行: 体系重复定义: {name}')
                        continue
                    self.systems[name] = levels
                elif head == 'div':
                    self._add_division(tok[1], tok[2], tok[3], tok[4], tok[5],
                                       src=f'第{lineno}行')
                elif head == 'op':
                    self.ops.append((lineno, tok[1:]))
                else:
                    self.err('bad_line', f'第{lineno}行: 无法识别的指令: {head}')
            except IndexError:
                self.err('bad_line', f'第{lineno}行: 字段不足: {line}')

    def _add_division(self, system, name, level, parent, scope, src):
        if system not in self.systems:
            self.err('unknown_system', f'{src}: 未定义的体系: {system}')
            return
        key = (system, name)
        if key in self.divisions:
            self.err('duplicate_division',
                     f'{src}: 区划重复定义: {system}/{name}（保留首次定义，忽略本次）')
            return
        if level not in self.systems[system]:
            self.err('unknown_level',
                     f'{src}: 体系 {system} 无层级 "{level}"（仍收录，级联时将修正）')
        self.divisions[key] = Division(system, name, level,
                                       '' if parent == '-' else parent, scope)

    # ---------- 操作流 ----------
    def apply_ops(self):
        for lineno, args in self.ops:
            if not args:
                continue
            kind, rest = args[0], args[1:]
            src = f'操作(第{lineno}行)'
            if kind == 'add' and len(rest) == 5:
                self._add_division(rest[0], rest[1], rest[2], rest[3], rest[4], src=src)
            elif kind == 'delete' and len(rest) == 2:
                key = (rest[0], rest[1])
                div = self.divisions.pop(key, None)
                if div is None:
                    self.err('unknown_division', f'{src}: 删除不存在的区划: {key[0]}/{key[1]}')
                    continue
                self.deleted.add(key)
                orphans = [d for d in self.divisions.values() if d.parent == div.name]
                for child in orphans:
                    self.err('dangling_child',
                             f'{src}: 删除 {div.system}/{div.name} 后，其下级 '
                             f'{child.system}/{child.name} 悬挂（上级已不存在）')
            elif kind == 'reparent' and len(rest) == 3:
                key = (rest[0], rest[1])
                div = self.divisions.get(key)
                if div is None:
                    self.err('unknown_division', f'{src}: 改归属失败，区划不存在: {key[0]}/{key[1]}')
                    continue
                div.parent = '' if rest[2] == '-' else rest[2]
            else:
                self.err('bad_op', f'{src}: 无法识别的操作: {" ".join(args)}')

    # ---------- 上级解析 ----------
    def resolve_parent(self, div):
        """返回 (父Division 或 None, 状态)。状态: ok/none/missing/cross_system"""
        if not div.parent:
            return None, 'none'
        same = (div.system, div.parent)
        if same in self.divisions:
            return self.divisions[same], 'ok'
        owners = [k[0] for k in self.divisions if k[1] == div.parent]
        if owners:
            return self.divisions[(owners[0], div.parent)], 'cross_system'
        return None, 'missing'

    def check_parents(self):
        for div in self.divisions.values():
            parent, status = self.resolve_parent(div)
            if status == 'missing':
                if (div.system, div.parent) in self.deleted:
                    continue  # 删除导致的悬挂已在 delete 时报告
                self.err('missing_parent',
                         f'{div.system}/{div.name}: 上级 "{div.parent}" 不存在')
            elif status == 'cross_system':
                self.err('cross_system_parent',
                         f'{div.system}/{div.name}: 跨体系归属，上级 "{div.parent}" '
                         f'属于体系 {parent.system}（归属限本体系内）')

    # ---------- 环检测（含跨体系边） ----------
    def check_cycles(self):
        reported = set()
        for div in self.divisions.values():
            seen, path = {}, []
            node = div
            while node is not None and node.key not in seen:
                seen[node.key] = len(path)
                path.append(node)
                parent, status = self.resolve_parent(node)
                node = parent if status in ('ok', 'cross_system') else None
            if node is not None and node.key in seen:
                cycle = path[seen[node.key]:]
                ckeys = frozenset(d.key for d in cycle)
                if ckeys not in reported:
                    reported.add(ckeys)
                    desc = ' -> '.join(f'{d.system}/{d.name}' for d in cycle)
                    self.err('cycle', f'归属环: {desc} -> {cycle[0].system}/{cycle[0].name}')

    # ---------- 范围解析（含跨体系引用） ----------
    def resolve_scope(self, div, memo, path):
        if div.key in memo:
            return memo[div.key]
        if div.key in path:
            idx = path.index(div.key)
            chain = ' -> '.join(f'{s}/{n}' for s, n in path[idx:] + [div.key])
            self.err('ref_cycle', f'范围引用环: {chain}')
            memo[div.key] = None
            return None
        spec = div.scope_spec
        if spec.startswith('@'):
            ref = spec[1:]
            if ':' in ref:
                rsys, rname = ref.split(':', 1)
            else:
                rsys, rname = div.system, ref
            target = self.divisions.get((rsys, rname))
            if target is None:
                self.err('missing_ref',
                         f'{div.system}/{div.name}: 范围引用的区划不存在: {rsys}/{rname}')
                memo[div.key] = None
                return None
            path.append(div.key)
            rect = self.resolve_scope(target, memo, path)
            path.pop()
            memo[div.key] = rect
            return rect
        try:
            parts = tuple(float(v) for v in spec.split(','))
            assert len(parts) == 4
            rect = (min(parts[0], parts[2]), min(parts[1], parts[3]),
                    max(parts[0], parts[2]), max(parts[1], parts[3]))
        except (ValueError, AssertionError):
            self.err('bad_scope', f'{div.system}/{div.name}: 范围格式错误: {spec}')
            rect = None
        memo[div.key] = rect
        return rect

    def check_overlaps(self):
        rects = {}
        for div in self.divisions.values():
            rects[div.key] = self.resolve_scope(div, self.scope_memo, [])
        divs = list(self.divisions.values())
        for i in range(len(divs)):
            for j in range(i + 1, len(divs)):
                a, b = divs[i], divs[j]
                if a.system == b.system:
                    continue
                ra, rb = rects.get(a.key), rects.get(b.key)
                if ra is None or rb is None:
                    continue
                if ra[0] < rb[2] and rb[0] < ra[2] and ra[1] < rb[3] and rb[1] < ra[3]:
                    self.err('scope_overlap',
                             f'范围重叠: {a.system}/{a.name} 与 {b.system}/{b.name} '
                             f'（体系对: {a.system} × {b.system}）')

    # ---------- 层级级联 ----------
    def cascade_levels(self):
        for div in self.divisions.values():
            levels = self.systems.get(div.system, [])
            depth, node = 0, div
            seen = {div.key}
            cycled = broken = False
            while node.parent:
                parent, status = self.resolve_parent(node)
                if status != 'ok':      # 缺失/跨体系：链断裂，无法权威定级，错误已另行报告
                    broken = True
                    break
                if parent.key in seen:
                    cycled = True
                    break
                seen.add(parent.key)
                node = parent
                depth += 1
            if cycled or broken:        # 环上或断裂链上的节点不强行改级
                continue
            if depth >= len(levels):
                self.err('level_overflow',
                         f'{div.system}/{div.name}: 级联深度 {depth} 超出体系 '
                         f'{div.system} 已定义层级数 {len(levels)}')
                continue
            correct = levels[depth]
            if div.level != correct:
                self.err('level_fixed',
                         f'{div.system}/{div.name}: 层级 "{div.level}" 与归属关系不符，'
                         f'已级联修正为 "{correct}"')
                div.level = correct

    # ---------- 输出 ----------
    def report(self, out):
        out.append('== 区划状态 ==')
        for sysname, levels in self.systems.items():
            out.append(f'体系 {sysname}（层级: {" > ".join(levels)}）')
            memo = self.scope_memo
            divs = [d for d in self.divisions.values() if d.system == sysname]
            children = {}
            roots = []
            for d in divs:
                parent, status = self.resolve_parent(d)
                if status == 'ok':
                    children.setdefault(parent.key, []).append(d)
                else:
                    roots.append(d)
            def scope_str(d):
                r = self.resolve_scope(d, memo, [])
                return str(r) if r else f'未解析({d.scope_spec})'
            emitted = set()
            def emit(d, indent):
                emitted.add(d.key)
                out.append(f'{"  " * indent}{d.name} [{d.level}] 范围={scope_str(d)}')
                for c in sorted(children.get(d.key, []), key=lambda x: x.name):
                    if c.key not in emitted:
                        emit(c, indent + 1)
            for r in sorted(roots, key=lambda x: x.name):
                if r.key not in emitted:
                    emit(r, 1)
            unattached = [d for d in divs if d.key not in emitted]
            for d in sorted(unattached, key=lambda x: x.name):
                out.append(f'  {d.name} [{d.level}] 范围={scope_str(d)}（未挂接/环上，上级="{d.parent}"）')
            if not divs:
                out.append('  （无区划）')
        out.append('')
        out.append('== 错误报告 ==')
        if not self.errors:
            out.append('（无错误）')
        else:
            for i, (kind, msg) in enumerate(self.errors, 1):
                out.append(f'{i}. [{kind}] {msg}')
        return '\n'.join(out)

    def run(self, text):
        self.parse(text)
        self.apply_ops()
        self.check_parents()
        self.check_cycles()
        self.check_overlaps()
        self.cascade_levels()
        return self.report([])


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding='utf-8') as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    print(Engine().run(text))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
