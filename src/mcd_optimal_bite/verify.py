# -*- coding: utf-8 -*-
"""mcd_optimal_bite.verify — calculate-price 官方复核层

职责：把求解器/拆单给出的方案，翻译成 calculate-price 的 items 格式，
逐单调用官方算价，把实付金额写回 Solution/SubOrder，与估算价对比告警。

items 结构（core_schemas.json 实测）：
- 单品：    {"productCode": code, "quantity": n}
- 套餐：    {"productCode": combo_code, "quantity": 1,
             "roundList": [{"round": 轮次名, "comboItemList": [{"code": pick, "quantity": 1}]}]}
- 挂券：    item 上加 "couponId" / "couponCode"（一单仅挂一张）
- 价格字段：data.price / data.discount 等，单位=分 -> /100 转元

安全：本层只调官方 calculate-price（只读），不碰 create-order。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .models import Catalog, PurchaseAction, Solution
from .split import SplitPlan, SubOrder

CENT = Decimal("100")


def _to_yuan(cents) -> Decimal:
    return Decimal(str(cents or 0)) / CENT


@dataclass
class VerifyReport:
    """一次复核结果"""
    est_total: Decimal          # 求解器估算价（元）
    official_total: Decimal     # 官方算价（元）
    official_discount: Decimal  # 官方优惠额（元）
    raw: dict                   # 原始返回（供调试）
    warnings: Tuple[str, ...] = ()

    @property
    def diff(self) -> Decimal:
        return self.official_total - self.est_total


def build_items(catalog: Catalog, actions: Tuple[PurchaseAction, ...],
                coupon_id: Optional[str] = None,
                coupon_code: Optional[str] = None,
                coupon_product_codes: Optional[frozenset] = None) -> List[dict]:
    """方案动作 -> calculate-price items。

    套餐动作按 detail.picks 生成 roundList（非默认替换选项精确表达）；
    coupon 挂到第一个【匹配该券 product_codes】的商品上（一单一券）。
    coupon_product_codes=None 时退回"任意券适用"（单券场景兼容）。
    """
    items: List[dict] = []
    coupon_attached = False
    for act in actions:
        if act.kind == "single":
            code = next(iter(act.cover), None)
            if code is None:
                continue
            qty = act.cover[code]
            item: Dict[str, Any] = {"productCode": code, "quantity": qty}
        else:  # combo
            combo_code = act.detail.get("combo_code")
            if combo_code is None:
                continue
            item = {"productCode": combo_code, "quantity": 1}
            # picks {round_idx: code} -> roundList（round 必须传轮次 id，名字会被服务端拒）
            combo = catalog.combos.get(combo_code)
            picks = act.detail.get("picks") or {}
            if combo is not None and picks:
                round_list = []
                for ri, rnd in enumerate(combo.rounds):
                    pick = picks.get(ri)
                    if pick is None:
                        continue
                    round_list.append({
                        "round": str(rnd.id or ri + 1),
                        "comboItemList": [{"code": pick, "quantity": 1}],
                    })
                if round_list:
                    item["roundList"] = round_list
        # 挂券：第一个匹配该券适用商品的商品
        if (coupon_id or coupon_code) and not coupon_attached:
            for c in act.cover:
                if coupon_product_codes is not None:
                    hit = c in coupon_product_codes
                else:
                    hit = _is_coupon_eligible(catalog, c)
                if hit:
                    if coupon_id:
                        item["couponId"] = coupon_id
                    if coupon_code:
                        item["couponCode"] = coupon_code
                    coupon_attached = True
                    break
        items.append(item)
    return items


def _is_coupon_eligible(catalog: Catalog, code: str) -> bool:
    return any(code in c.product_codes for c in catalog.coupons)


def verify_actions(client, catalog: Catalog,
                   actions: Tuple[PurchaseAction, ...],
                   est_total: Decimal,
                   coupon_id: Optional[str] = None,
                   coupon_code: Optional[str] = None,
                   coupon_product_codes: Optional[frozenset] = None,
                   be_type: int = 1, order_type: int = 1) -> VerifyReport:
    """对一个购物车（一批 actions）调官方算价并对比。"""
    items = build_items(catalog, actions, coupon_id=coupon_id, coupon_code=coupon_code,
                        coupon_product_codes=coupon_product_codes)
    warns: List[str] = []
    if not items:
        return VerifyReport(est_total=est_total, official_total=Decimal("0"),
                            official_discount=Decimal("0"), raw={},
                            warnings=("方案为空，无从复核",))
    args = {"beType": be_type, "orderType": order_type,
            "storeCode": catalog.store_code, "items": items}
    data = client.call_tool_json("calculate-price", args)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    official = _to_yuan(data.get("price"))
    discount = _to_yuan(data.get("discount"))
    diff = official - est_total
    if abs(diff) >= Decimal("0.01"):
        warns.append(f"估算与官方差 ¥{diff:+}（估算{est_total} vs 官方{official}），以官方为准")
    return VerifyReport(est_total=est_total, official_total=official,
                        official_discount=discount, raw=data if isinstance(data, dict) else {},
                        warnings=tuple(warns))


def verify_solution(client, catalog: Catalog, solution: Solution,
                    be_type: int = 1, order_type: int = 1) -> VerifyReport:
    """整单复核（无拆单场景）。写回 solution.official_* 字段。"""
    report = verify_actions(client, catalog, solution.actions, solution.est_total,
                            be_type=be_type, order_type=order_type)
    solution.official_price = report.official_total
    solution.official_discount = report.official_discount
    solution.official_raw = report.raw
    return report


def verify_split(client, catalog: Catalog, plan: SplitPlan,
                 be_type: int = 1, order_type: int = 1) -> List[VerifyReport]:
    """逐子单复核（带券场景）。每单挂自己那张券。"""
    reports: List[VerifyReport] = []
    for sub in plan.suborders:
        coupon = sub.coupon
        report = verify_actions(
            client, catalog, sub.actions, sub.est_total,
            coupon_id=coupon.coupon_id if coupon else None,
            coupon_code=coupon.coupon_code if coupon else None,
            coupon_product_codes=coupon.product_codes if coupon else None,
            be_type=be_type, order_type=order_type)
        reports.append(report)
    return reports
