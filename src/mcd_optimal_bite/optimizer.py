# -*- coding: utf-8 -*-
"""mcd_optimal_bite.optimizer — 确定性求解：想吃清单 -> 最省官方点单方案

问题定义：
    需求 demand {code: 数量} -> 从购买方式图选一组动作，覆盖全部需求且总价最小。
    价格用目录估算（官方口径由 calculate-price 复核层负责）。

购买方式（PurchaseAction）：
- 单点：products 表直达商品，价 = currentPrice。
- 套餐变体：目标轮选产出需求码的项；其余轮选"差价最小项"或"需求命中项"。
  覆盖语义：套餐产出的项若在需求中则【抵扣】需求；多出的项视为随套餐附赠（不冲突）。
  套餐价 < 单点价是常态（如 7.5 元早餐组合含汉堡+饮料），因此赠品不影响最优性。

搜索：按需求码规范顺序 DFS + 价格升序剪枝（cost+v_price >= best 即 break）+ 时间预算。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import product as iproduct
from typing import Dict, List, Optional, Tuple

from .models import Catalog, Combo, Demand, PurchaseAction, Solution

# (价格, 覆盖{code:qty}, picks{round_idx:code})
Variant = Tuple[Decimal, Dict[str, int], Dict[int, str]]

_MAX_VARIANTS_PER_CODE = 6   # 单个需求码保留的最便宜变体数（剪枝：长尾变体几乎不可能进最优解）


@dataclass
class OptimizeResult:
    """optimize() 的完整返回（Solution + 调试信息）"""
    solution: Solution
    unreachable: Dict[str, int] = field(default_factory=dict)  # 无法覆盖 {code: qty}
    elapsed_ms: int = 0


def combo_variants(combo: Combo, target_code: str,
                   need_codes: Optional[frozenset] = None) -> List[Variant]:
    """枚举套餐 combo 中产出 target_code 的有意义变体（价格升序、去重）。

    目标轮选 target_code；其余轮候选 = 差价最小项 + 至多 2 个需求命中项。
    单次调用最多返回 16 个变体（保险丝）。
    """
    variants: List[Variant] = []
    seen: set = set()
    for ti, rnd in enumerate(combo.rounds):
        if not any(c.product_code == target_code for c in rnd.choices):
            continue
        target_choice = next(c for c in rnd.choices if c.product_code == target_code)
        other_idx = [i for i in range(len(combo.rounds)) if i != ti]

        per_round: List[List] = []
        for oi in other_idx:
            cs = sorted(combo.rounds[oi].choices,
                        key=lambda c: (c.diff_price, c.product_code))
            picked = [cs[0]]  # 差价最小项
            if need_codes and len(cs) > 1:
                picked += [c for c in cs[1:] if c.product_code in need_codes][:2]
            per_round.append(picked)

        for picks_combo in iproduct(*per_round):
            picks: Dict[int, str] = {ti: target_code}
            price = combo.base_price + target_choice.diff_price
            cover: Dict[str, int] = {target_code: 1}
            for oi, ch in zip(other_idx, picks_combo):
                picks[oi] = ch.product_code
                price += ch.diff_price
                cover[ch.product_code] = cover.get(ch.product_code, 0) + 1
            key = tuple(sorted(picks.items()))
            if key in seen:
                continue
            seen.add(key)
            variants.append((price, cover, picks))
    variants.sort(key=lambda v: (v[0], sorted(v[2].items())))
    return variants[:16]


class Optimizer:
    """覆盖搜索求解器。"""

    def __init__(self, catalog: Catalog, time_budget_ms: int = 3000):
        self.catalog = catalog
        self.time_budget_ms = time_budget_ms

    def _name(self, code: str) -> str:
        p = self.catalog.products.get(code)
        return p.name if p else code

    def _variant_actions(self, combo: Combo, need_codes: set) -> List[Variant]:
        """套餐的全部候选变体（对每个可选目标码生成），去重 + 价格升序。"""
        need = frozenset(need_codes)
        out: List[Variant] = []
        for rnd in combo.rounds:
            for ch in rnd.choices:
                out.extend(combo_variants(combo, ch.product_code, need))
        seen: set = set()
        uniq: List[Variant] = []
        for price, cover, picks in out:
            key = tuple(sorted(picks.items()))
            if key not in seen:
                seen.add(key)
                uniq.append((price, cover, picks))
        uniq.sort(key=lambda v: v[0])
        return uniq[:40]

    # ---------- 基线 ----------

    def baseline(self, demand: Demand) -> Tuple[Optional[Decimal], List[str]]:
        """全单点基线。需求含不可单点项（仅随套餐提供）时返回 (None, warnings)。"""
        total = Decimal("0")
        warns: List[str] = []
        for code, qty in demand.items.items():
            if qty <= 0:
                continue
            p = self.catalog.products.get(code)
            if p is None:
                warns.append(f"{self._name(code)} 不可单点（仅随套餐提供），无法计算全单点基线")
                continue
            total += p.price * qty
        return (total if not warns else None, warns)

    # ---------- 主搜索 ----------

    def optimize(self, demand: Demand) -> OptimizeResult:
        t0 = time.monotonic()
        demand = Demand({c: q for c, q in demand.items.items() if q > 0})
        warnings: List[str] = []

        # 预处理：套餐变体（只保留与需求有交集的套餐）+ 单点表
        need_codes = set(demand.items.keys())
        variant_cache: Dict[str, List[Variant]] = {}
        for cb in self.catalog.combos.values():
            if not (cb.product_codes & need_codes):
                continue
            vs = self._variant_actions(cb, need_codes)
            if vs:
                variant_cache[cb.code] = vs
        single_map = {code: p.price for code, p in self.catalog.products.items()
                      if code in need_codes}

        # 可达性：单品表 ∪ 候选套餐的全部商品
        reachable = set(single_map.keys())
        for cb_code in variant_cache:
            reachable |= set(self.catalog.combos[cb_code].product_codes)
        unreachable = {c: q for c, q in demand.items.items() if c not in reachable}

        # 需求码 -> 覆盖它的全部套餐变体（按价格升序，每码截断到最便宜的 N 个）
        code_to_variants: Dict[str, List[Tuple[str, Decimal, Dict[str, int], Dict[int, str]]]] = {}
        for cb_code, variants in variant_cache.items():
            for v_price, v_cover, v_picks in variants:
                for c in v_cover:
                    code_to_variants.setdefault(c, []).append((cb_code, v_price, v_cover, v_picks))
        for c in code_to_variants:
            code_to_variants[c].sort(key=lambda v: v[1])
            code_to_variants[c] = code_to_variants[c][:_MAX_VARIANTS_PER_CODE]

        bl_total, bl_warns = self.baseline(demand)
        warnings.extend(bl_warns)

        effective = Demand({c: q for c, q in demand.items.items() if c not in unreachable})
        if not effective.items:
            if unreachable:
                names = "、".join(f"{self._name(c)}×{q}" for c, q in unreachable.items())
                warnings.append(f"无可行方案：{names} 在当店当前时段菜单不可得")
            else:
                warnings.append("需求为空")
            sol = Solution(actions=(), est_total=Decimal("0"), baseline_total=None,
                           coupon_hints=self._coupon_hints(demand),
                           warnings=tuple(warnings))
            return OptimizeResult(solution=sol, unreachable=unreachable,
                                  elapsed_ms=int((time.monotonic() - t0) * 1000))
        if unreachable:
            names = "、".join(f"{self._name(c)}×{q}" for c, q in unreachable.items())
            warnings.append(f"以下需求在当前时段菜单不可得，未纳入方案：{names}")

        best: List = [None]  # [0] = (cost, actions)
        deadline = t0 + self.time_budget_ms / 1000
        nodes = [0]
        timed_out = [False]
        memo: Dict[Tuple, Decimal] = {}  # 状态 -> 已达该状态的最小 cost（支配剪枝）

        # 贪心上界：每个需求码独立选最便宜的覆盖方式（单点 or 最便宜套餐变体），
        # 快速给出可行解作为剪枝上界，避免搜索前段无界扩散。
        greedy_actions: List[PurchaseAction] = []
        greedy_cost = Decimal("0")
        greedy_ok = True
        for code, qty in effective.items.items():
            if code in single_map:
                p = self.catalog.products[code]
                greedy_actions.append(PurchaseAction(
                    kind="single", label=p.name,
                    est_price=single_map[code] * qty,  # 总价（单价×数量，与 cover 一致）
                    cover={code: qty}))
                greedy_cost += single_map[code] * qty
            else:
                cands = code_to_variants.get(code, [])
                if not cands:
                    greedy_ok = False
                    break
                cb_code, v_price, v_cover, v_picks = cands[0]
                cb = self.catalog.combos[cb_code]
                greedy_actions.append(PurchaseAction(
                    kind="combo", label=_combo_label(cb, v_picks), est_price=v_price,
                    cover=dict(v_cover), detail={"combo_code": cb.code, "picks": dict(v_picks)}))
                greedy_cost += v_price * qty
        if greedy_ok:
            best[0] = (greedy_cost, greedy_actions)

        def dfs(remaining: Tuple[Tuple[str, int], ...],
                acc: List[PurchaseAction], cost: Decimal) -> None:
            nodes[0] += 1
            if nodes[0] % 512 == 0 and time.monotonic() > deadline:
                timed_out[0] = True
                raise TimeoutError
            if best[0] is not None and cost >= best[0][0]:
                return  # 剪枝1：已不可能更优
            if not remaining:
                best[0] = (cost, list(acc))
                return
            # 剪枝2：同一状态已用更低 cost 到达过，当前分支不可能更优
            prev = memo.get(remaining)
            if prev is not None and prev <= cost:
                return
            memo[remaining] = cost
            code, qty = remaining[0]
            rest = remaining[1:]

            # 分支1：单点
            if code in single_map:
                p = self.catalog.products[code]
                acc.append(PurchaseAction(kind="single", label=p.name,
                                          est_price=single_map[code], cover={code: 1}))
                nxt = (((code, qty - 1),) + rest) if qty > 1 else rest
                dfs(nxt, acc, cost + single_map[code])
                acc.pop()

            # 分支2：套餐变体（价格升序；超上界即 break）
            for cb_code, v_price, v_cover, v_picks in code_to_variants.get(code, []):
                if best[0] is not None and cost + v_price >= best[0][0]:
                    break
                cb = self.catalog.combos[cb_code]
                acc.append(PurchaseAction(
                    kind="combo", label=_combo_label(cb, v_picks), est_price=v_price,
                    cover=dict(v_cover),
                    detail={"combo_code": cb.code, "picks": dict(v_picks)}))
                items: Dict[str, int] = {code: qty}
                items.update(rest)
                for c, q in v_cover.items():
                    items[c] = items.get(c, 0) - q  # 抵扣需求
                nxt = tuple(sorted((c, q) for c, q in items.items() if q > 0))
                dfs(nxt, acc, cost + v_price)
                acc.pop()

        try:
            dfs(effective.key(), [], Decimal("0"))
        except TimeoutError:
            warnings.append(f"搜索超时（>{self.time_budget_ms}ms），返回当前最优解")

        if best[0] is None:
            if not timed_out[0]:
                warnings.append("无可行方案：当前时段菜单无法覆盖需求")
            sol = Solution(actions=(), est_total=Decimal("0"), baseline_total=bl_total,
                           coupon_hints=self._coupon_hints(demand), warnings=tuple(warnings))
            return OptimizeResult(solution=sol, unreachable=unreachable,
                                  elapsed_ms=int((time.monotonic() - t0) * 1000))

        actions = tuple(best[0][1])
        sol = Solution(
            actions=actions,
            est_total=best[0][0],
            baseline_total=bl_total,
            coupon_hints=self._coupon_hints(demand),
            near_miss_hints=self._near_miss(actions, demand),
            warnings=tuple(warnings),
        )
        return OptimizeResult(solution=sol, unreachable=unreachable,
                              elapsed_ms=int((time.monotonic() - t0) * 1000))

    # ---------- 附加信息 ----------

    def _coupon_hints(self, demand: Demand) -> Tuple[str, ...]:
        hints: List[str] = []
        for coupon in self.catalog.coupons:
            hit = coupon.product_codes & set(demand.items.keys())
            if hit:
                names = "、".join(self._name(c) for c in sorted(hit))
                hints.append(f"有券「{coupon.title}」适用于：{names}（最终抵扣以官方算价为准）")
        return tuple(hints)

    def _near_miss(self, actions: Tuple[PurchaseAction, ...],
                   demand: Demand) -> Tuple[str, ...]:
        """升级提示：已选套餐里，某个需求项没被选中、加点钱就能换入。"""
        hints: List[str] = []
        for act in actions:
            if act.kind != "combo" or not act.detail:
                continue
            cb = self.catalog.combos.get(act.detail.get("combo_code", ""))
            if cb is None:
                continue
            picks: Dict[int, str] = act.detail.get("picks", {})
            for ri, picked in picks.items():
                if ri >= len(cb.rounds):
                    continue
                cur = next((c for c in cb.rounds[ri].choices
                            if c.product_code == picked), None)
                if cur is None:
                    continue
                for ch in cb.rounds[ri].choices:
                    if ch.product_code == picked or ch.product_code not in demand.items:
                        continue
                    delta = ch.diff_price - cur.diff_price
                    if delta > 0:
                        hints.append(
                            f"升级提示：「{act.label}」中把{self._name(picked)}"
                            f"换成{self._name(ch.product_code)}需加 ¥{delta}")
                        break
        return tuple(hints)


def _combo_label(cb: Combo, picks: Dict[int, str]) -> str:
    """套餐展示名：有非默认选项时标注（含替换选项）。"""
    non_default = False
    for ri, rnd in enumerate(cb.rounds):
        pick = picks.get(ri)
        if pick is None:
            continue
        ch = next((c for c in rnd.choices if c.product_code == pick), None)
        if ch is not None and not ch.is_default:
            non_default = True
            break
    return cb.name if not non_default else f"{cb.name}（含替换选项）"


def solve(catalog: Catalog, demand: Demand, time_budget_ms: int = 3000) -> OptimizeResult:
    """便捷入口。"""
    return Optimizer(catalog, time_budget_ms=time_budget_ms).optimize(demand)
