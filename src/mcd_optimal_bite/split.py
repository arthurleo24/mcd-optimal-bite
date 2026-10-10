# -*- coding: utf-8 -*-
"""mcd_optimal_bite.split — 按券拆单（分餐）

业务理解（晨哥拍板 2026-10-10）：
- 不做"谁吃什么"归属：只要总人数 + 总食物清单（Demand 全局模型不变）
- 分餐唯一价值 = 按券拆单建议：优惠券一单只能用一张；
  点 N 份券适用商品、手里有 M 张可用券时，单一下单只用得上 1 张，
  其余券被浪费 -> 建议拆成 K=min(M,N) 单，每单挂 1 张券，
  并列出每个子单点什么。

产物 SubOrder 是"购物车分组"，不是人物归属；最终抵扣金额以
calculate-price 逐子单复核为准（items 支持逐项 couponId/couponCode）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from .models import Catalog, Coupon, PurchaseAction, Solution


@dataclass
class SubOrder:
    """一个拆分子单（购物车分组）。"""
    index: int                              # 1-based
    actions: Tuple[PurchaseAction, ...]
    est_total: Decimal
    coupon: Optional[Coupon] = None         # 挂在该单的券（无券单为 None）
    note: str = ""                          # 展示备注（为何拆/为何无券）

    @property
    def coupon_label(self) -> str:
        return f"券「{self.coupon.title}」" if self.coupon else "无券"


@dataclass
class SplitPlan:
    """拆单方案。单单可行时 suborders 也总是有 1 个元素（不出现空列表）。"""
    suborders: Tuple[SubOrder, ...]
    reason: str = ""                        # 拆单原因（无人/单不拆时不解释）
    eligible_qty: int = 0                   # 方案中券适用商品份数
    coupon_capacity: int = 0                # 可用券张数（同标题按张数计）
    warnings: Tuple[str, ...] = ()

    @property
    def is_split(self) -> bool:
        return len(self.suborders) > 1

    @property
    def est_total(self) -> Decimal:
        return sum(s.est_total for s in self.suborders)


def plan_split(catalog: Catalog, solution: Solution) -> SplitPlan:
    """把最优方案按券拆成多个子单；不满足拆单条件时返回单子单。

    拆单条件（全部满足）：
      1) 方案中券适用商品总份数 eligible_qty >= 2
      2) 可用券张数 capacity >= 2
    否则原样一单（券挂到第一个含适用商品的子单=整单，仅当 eligible_qty>=1）。
    """
    if not solution.actions:
        return SplitPlan(suborders=(), warnings=("方案为空，无法拆单",))

    # 方案覆盖的商品 -> 匹配券
    covered: Dict[str, int] = {}
    for act in solution.actions:
        for code, q in act.cover.items():
            covered[code] = covered.get(code, 0) + q
    applicable: List[Coupon] = [
        c for c in catalog.coupons if c.product_codes & set(covered)
    ]
    eligible_qty = sum(
        q for code, q in covered.items()
        if any(code in c.product_codes for c in applicable)
    )
    capacity = len(applicable)

    # 不满足拆单条件 -> 单子单，首单挂最便宜可用券（若有）
    if eligible_qty < 2 or capacity < 2:
        warns: List[str] = []
        if eligible_qty >= 1 and capacity == 1:
            warns.append(f"仅有 1 张可用券，{eligible_qty} 份券适用商品只能用券 1 份，"
                         f"其余按目录价计入（券抵扣以官方算价为准）")
        sub = SubOrder(index=1, actions=solution.actions,
                       est_total=solution.est_total,
                       coupon=applicable[0] if applicable else None)
        return SplitPlan(suborders=(sub,),
                         reason="不满足拆单条件（券适用商品<2份或可用券<2张），整单下单",
                         eligible_qty=eligible_qty, coupon_capacity=capacity,
                         warnings=tuple(warns))

    # 拆单：K = min(券张数, 券适用商品份数)
    k = min(capacity, eligible_qty)
    buckets: List[List[PurchaseAction]] = [[] for _ in range(k)]
    bucket_totals = [Decimal("0") for _ in range(k)]

    # 策略：含券适用商品的 action 优先轮转（保证每单至少 1 份适用商品），
    # 其余 action 装进当前最轻的单（尽量平衡但不强制）。
    # 单份展开：multi-unit 的 single action（cover={code:3}）无法整块进单个子单，
    # 先按份展开成 cover={code:1} 的 action 序列（拆单才能真正执行）。
    def has_eligible(act: PurchaseAction) -> bool:
        return any(any(code in c.product_codes for c in applicable)
                   for code in act.cover)

    def expand_units(act: PurchaseAction) -> List[PurchaseAction]:
        """cover 单码多份的 single action -> 多个单份 action。

        金额按分取整均分，余数并入最后一份，保证总价精确守恒
        （Decimal 直除会产生 28 位尾差，sum 后与原价不等）。
        """
        if act.kind != "single" or len(act.cover) != 1:
            return [act]
        code, q = next(iter(act.cover.items()))
        if q <= 1:
            return [act]
        per = (act.est_price / q).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        prices = [per] * (q - 1) + [act.est_price - per * (q - 1)]
        return [PurchaseAction(kind="single", label=act.label, est_price=pr,
                               cover={code: 1}, detail=dict(act.detail))
                for pr in prices]

    eligible_actions: List[PurchaseAction] = []
    other_actions: List[PurchaseAction] = []
    for act in solution.actions:
        target = eligible_actions if has_eligible(act) else other_actions
        target.extend(expand_units(act) if has_eligible(act) else [act])

    for i, act in enumerate(eligible_actions):
        b = i % k
        buckets[b].append(act)
        bucket_totals[b] += act.est_price
    for act in other_actions:
        b = bucket_totals.index(min(bucket_totals))
        buckets[b].append(act)
        bucket_totals[b] += act.est_price

    # 每单挂 1 张券（前 capacity 张，k<=capacity 保证够分）
    subs: List[SubOrder] = []
    for i, (acts, total) in enumerate(zip(buckets, bucket_totals)):
        if not acts:
            continue
        subs.append(SubOrder(index=i + 1, actions=tuple(acts), est_total=total,
                             coupon=applicable[i] if i < capacity else None))
    # 修正序号（可能有空桶被跳过）
    subs = [SubOrder(index=i + 1, actions=s.actions, est_total=s.est_total,
                     coupon=s.coupon)
            for i, s in enumerate(subs)]

    reason = (f"方案含 {eligible_qty} 份券适用商品、可用券 {capacity} 张，"
              f"一单只能用 1 张券 -> 建议拆成 {len(subs)} 单，每单各用 1 张券")
    return SplitPlan(suborders=tuple(subs), reason=reason,
                     eligible_qty=eligible_qty, coupon_capacity=capacity)
