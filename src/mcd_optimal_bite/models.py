# -*- coding: utf-8 -*-
"""mcd_optimal_bite.data_models — 麦麦最优解数据模型

字段名与麦当劳 MCP Server 真实返回对齐：
- query-meals:        data.meals{code: {name,currentPrice,originalPrice,...}}
- query-meal-detail:  data.rounds[].choices[]{code,diffPrice,isDefault}
- query-store-coupons:data[]{couponId,couponCode,title,products[].productCode}
- calculate-price:    data{productPrice,originalPrice,discount,price}（单位:分）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, FrozenSet, Tuple


def to_decimal(v) -> Decimal:
    """'34' / '34.5' / 34 / 34.5 -> Decimal；None -> 0"""
    if v is None:
        return Decimal("0")
    return Decimal(str(v))


@dataclass(frozen=True)
class Product:
    """单品（菜单上可直接购买的商品）

    price 口径（2026-10-10 实测）：
    - 普通促销（promo_type="促销优惠"）：price=currentPrice，官方算价直接生效
    - 随单购卡促销（promo_type="随单购早餐卡优惠"）：price=originalPrice（裸价，
      与官方算价一致）；promo_price=currentPrice 为买卡后价，提示层展示
    """
    code: str
    name: str
    price: Decimal            # 估算用价（裸价口径，元）
    original_price: Decimal   # 原价（元）
    category: str = ""        # 所属分类名
    promo_type: str = ""      # 促销类型（空=无促销）
    promo_price: Decimal = Decimal("0")  # 促销价（仅随单购卡型有值=currentPrice）


@dataclass(frozen=True)
class ComboChoice:
    """套餐某轮次里的一个可选项"""
    product_code: str
    diff_price: Decimal       # 相对默认选项的差价（元，正=加钱）
    is_default: bool = False


@dataclass(frozen=True)
class ComboRound:
    """套餐的一个选配轮次（例：'选择套餐内小食'）

    id：服务端轮次ID（calculate-price 的 roundList[].round 必须传 id，
    传轮次名会报服务器错误——2026-10-10 实测）。
    """
    name: str
    choices: Tuple[ComboChoice, ...]
    id: int = 0

    def default(self) -> ComboChoice:
        for c in self.choices:
            if c.is_default:
                return c
        return self.choices[0]


@dataclass(frozen=True)
class Combo:
    """套餐 = 基础价 + 每轮选一项（可替换，差价另计）"""
    code: str
    name: str
    base_price: Decimal       # 默认组合价格（元）
    rounds: Tuple[ComboRound, ...]
    product_codes: FrozenSet[str]  # 所有轮次可能出现过的商品（用于快速筛 Combo 候选）

    def price_for(self, picks: Dict[int, str]) -> Decimal:
        """给定 {轮次下标: 选择的product_code}，返回该组合价格（元）"""
        total = self.base_price
        for i, rnd in enumerate(self.rounds):
            pick = picks.get(i)
            if pick is None:
                continue
            choice = next((c for c in rnd.choices if c.product_code == pick), None)
            if choice is None:
                raise ValueError(f"round {i} has no choice {pick}")
            total += choice.diff_price
        return total

    def default_cover(self) -> Dict[str, int]:
        """默认组合覆盖的商品 {code: 1}（每轮默认项各一）"""
        cover: Dict[str, int] = {}
        for rnd in self.rounds:
            c = rnd.default()
            cover[c.product_code] = cover.get(c.product_code, 0) + 1
        return cover


@dataclass(frozen=True)
class Coupon:
    """优惠券（抵扣金额以官方 calculate-price 为准）"""
    coupon_id: str
    title: str
    product_codes: FrozenSet[str]  # 适用商品编码集合
    coupon_code: str = ""          # 券码（calculate-price 可用）


@dataclass
class Catalog:
    """门店目录：单品 + 套餐 + 券

    fetched_at：目录拉取时间（ISO 字符串）。菜单严格按时段轮换（早餐/正餐/夜市），
    券池随营销活动轮换，上层须据此判断缓存新鲜度。
    """
    store_code: str
    store_name: str
    products: Dict[str, Product]                 # code -> Product
    combos: Dict[str, Combo]                     # code -> Combo
    coupons: Tuple[Coupon, ...] = ()
    fetched_at: str = ""
    # 组件名映射（套餐轮次选项里可能引用不在 products 表的 code，如 3050 可乐）
    component_names: Dict[str, str] = field(default_factory=dict)

    def candidates_covering(self, code: str):
        """所有能提供商品 code 的购买方式（v1: 单品 + 默认/替换后可含 code 的套餐）"""
        # 单点
        yield ("single", code, None)
        # 套餐（该商品出现在某轮 choices 里即可考虑）
        for combo in self.combos.values():
            for i, rnd in enumerate(combo.rounds):
                if any(c.product_code == code for c in rnd.choices):
                    yield ("combo", combo.code, i)
                    break


@dataclass
class Demand:
    """需求：想吃的商品 {code: 数量}"""
    items: Dict[str, int] = field(default_factory=dict)

    def key(self) -> Tuple[Tuple[str, int], ...]:
        """规范化状态键（排序后 tuple，用于 memo）"""
        return tuple(sorted((c, q) for c, q in self.items.items() if q > 0))

    @staticmethod
    def from_key(k: Tuple[Tuple[str, int], ...]) -> "Demand":
        return Demand({c: q for c, q in k})

    def clone(self) -> "Demand":
        return Demand(dict(self.items))

    def is_empty(self) -> bool:
        return all(q <= 0 for q in self.items.values())

    def smallest_code(self):
        """剩余需求中最小的商品编码（搜索分支规范化入口）"""
        alive = sorted(c for c, q in self.items.items() if q > 0)
        return alive[0] if alive else None


@dataclass
class PurchaseAction:
    """一次购买动作"""
    kind: str                  # "single" | "combo"
    label: str                 # 展示名
    est_price: Decimal         # 估算价（元）
    cover: Dict[str, int]      # 覆盖需求 {code: qty}
    detail: dict = field(default_factory=dict)  # combo: {combo_code, picks{round_idx: code}}


@dataclass
class Solution:
    """完整方案"""
    actions: Tuple[PurchaseAction, ...]
    est_total: Decimal
    official_price: Decimal | None = None      # calculate-price 复核价（元）
    official_discount: Decimal | None = None   # 官方优惠额（元）
    official_raw: dict | None = None
    baseline_total: Decimal | None = None      # 全单点基线（需求含仅套餐可买项时为 None）
    coupon_hints: Tuple[str, ...] = ()
    near_miss_hints: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def savings_vs_single(self) -> Decimal | None:
        if self.baseline_total is None:
            return None
        return self.baseline_total - self.est_total

    @property
    def final_price(self) -> Decimal:
        return self.official_price if self.official_price is not None else self.est_total
