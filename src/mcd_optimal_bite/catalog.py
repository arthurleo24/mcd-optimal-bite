# -*- coding: utf-8 -*-
"""mcd_optimal_bite.catalog — 门店目录构建（菜单 + 套餐组成 + 券 -> 购买方式图）

数据流：
    query-meals        -> 全量价目（单品与套餐同表，name/currentPrice/originalPrice，元，字符串）
    query-meal-detail  -> 逐项判定：rounds 非空即套餐（含轮次/choices/diffPrice）
    query-store-coupons-> 券（couponId/title/products[].productCode）

实测约定：
- 服务端返回形态不固定：有的带 {"success":..,"data":..} 包裹，有的是裸数组 -> unwrap 归一化
- diffPrice 是字符串 "+ ¥1.5" / "- ¥1.5"（负=比默认便宜）-> parse_diff_price 转 Decimal
- 套餐 detail 无价格字段，base_price 取 meals 表 currentPrice
- 返回文本夹带的"展示格式要求"等提示词已由 mcp_client.extract_json 剥离，本层只见 JSON

IO 与解析分离：parse_* 为纯函数可离线测试；CatalogBuilder 负责拉取与组装。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from .mcp_client import McpClient, McpError
from .models import (
    Catalog,
    Combo,
    ComboChoice,
    ComboRound,
    Coupon,
    Product,
    to_decimal,
)

_DIFF_PRICE_RE = re.compile(r"([+-])?\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)")


def unwrap(data: Any) -> Any:
    """归一化：{"success":..,"data":..} -> data；裸数组/裸对象原样。"""
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def parse_diff_price(v: Any) -> Decimal:
    """'+ ¥1.5' / '- ¥1.5' / '1.5' / 1.5 -> Decimal（缺符号视为 +0 差价基数）。"""
    if v is None:
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = str(v).replace(",", "").strip()
    m = _DIFF_PRICE_RE.search(s)
    if not m:
        return Decimal("0")
    sign = -1 if m.group(1) == "-" else 1
    return Decimal(m.group(2)) * sign


def parse_meals(meals_data: Any) -> Dict[str, Product]:
    """query-meals 返回（success 包裹 / data dict / data.meals 映射均可）-> {code: Product}

    price 口径：随单购卡促销（如早餐卡）取 originalPrice（裸价，与官方算价一致），
    其余取 currentPrice（普通促销官方算价直接生效）。
    """
    data = unwrap(meals_data)
    if isinstance(data, dict) and "meals" in data:
        data = data["meals"]
    products: Dict[str, Product] = {}
    for code, info in (data or {}).items():
        promo_type = str(info.get("discountType") or "")
        # 规律（2026-10-10 实测）：所有「随单购X卡优惠」（早餐卡/麦金卡/…）的
        # currentPrice 都是买卡后价，官方裸算按 originalPrice —— 一律按裸价口径
        needs_card = "随单购" in promo_type and "卡" in promo_type
        cur = to_decimal(info.get("currentPrice"))
        orig = to_decimal(info.get("originalPrice"))
        products[str(code)] = Product(
            code=str(code),
            name=str(info.get("name") or "").strip(),
            price=orig if needs_card else cur,
            original_price=orig,
            promo_type=promo_type,
            promo_price=cur if needs_card else Decimal("0"),
        )
    return products


def parse_categories(meals_data: Any) -> Dict[str, str]:
    """query-meals 返回 -> {code: 分类名}。同一商品多分类时用 | 保序拼接。
    入参兼容完整 response / data dict / categories 数组。"""
    data = unwrap(meals_data)
    if isinstance(data, dict) and "categories" in data:
        data = data["categories"]
    cat: Dict[str, List[str]] = {}
    for c in data or []:
        cname = str(c.get("name") or "").replace("\n", "").strip()
        for x in c.get("meals") or []:
            code = x.get("code") if isinstance(x, dict) else x
            if not code:
                continue
            lst = cat.setdefault(str(code), [])
            if cname and cname not in lst:
                lst.append(cname)
    return {code: "|".join(v) for code, v in cat.items()}


def parse_detail(
    code: str,
    name: str,
    base_price: Decimal,
    detail_data: Any,
) -> Optional[Combo]:
    """query-meal-detail 的 data -> Combo（rounds 为空说明是单品，返回 None）。

    base_price：套餐默认组合价（meals 表 currentPrice，元）。
    v1 约定：每轮选 1 项（maxQuantity>1 的轮次按选 1 处理）。
    """
    d = unwrap(detail_data)
    if not isinstance(d, dict):
        return None
    rounds_raw = d.get("rounds") or []
    if not rounds_raw:
        return None
    rounds: List[ComboRound] = []
    all_codes: set = set()
    for r in rounds_raw:
        choices: List[ComboChoice] = []
        for ch in r.get("choices") or []:
            ccode = str(ch.get("code") or "")
            if not ccode:
                continue
            choices.append(
                ComboChoice(
                    product_code=ccode,
                    diff_price=parse_diff_price(ch.get("diffPrice")),
                    is_default=bool(ch.get("isDefault")),
                )
            )
            all_codes.add(ccode)
        if not choices:
            continue  # 空轮次跳过（不可选，不参与模型）
        rounds.append(ComboRound(name=str(r.get("name") or "").strip(),
                                 choices=tuple(choices),
                                 id=int(r.get("id") or 0)))
    if not rounds:
        return None
    return Combo(
        code=str(code),
        name=name,
        base_price=base_price,
        rounds=tuple(rounds),
        product_codes=frozenset(all_codes),
    )


def parse_coupons(coupons_data: Any) -> Tuple[Coupon, ...]:
    """query-store-coupons -> (Coupon, ...)。适用商品取 products[].productCode。"""
    coupons: List[Coupon] = []
    for c in unwrap(coupons_data) or []:
        codes = frozenset(
            str(p.get("productCode"))
            for p in (c.get("products") or [])
            if p.get("productCode")
        )
        coupons.append(
            Coupon(
                coupon_id=str(c.get("couponId") or ""),
                title=str(c.get("title") or "").strip(),
                product_codes=codes,
                coupon_code=str(c.get("couponCode") or ""),
            )
        )
    return tuple(coupons)


class CatalogBuilder:
    """拉取 MCP 数据并组装 Catalog。

    用法::

        with McpClient() as client:
            catalog = CatalogBuilder(client, store_code="1990526").build()

    detail 逐项拉取受客户端限流约束（默认 0.4s/次），107 项约 1 分钟；
    建议用 build() 拉一次后 save_catalog 缓存，离线复用。
    """

    def __init__(self, client: McpClient, store_code: str,
                 be_type: int = 1, order_type: int = 1):
        self.client = client
        self.store_code = store_code
        self.be_type = be_type          # 到店自取=1，不传 beCode
        self.order_type = order_type

    # ---------- 拉取 ----------

    def _base_args(self) -> Dict[str, Any]:
        return {"beType": self.be_type, "orderType": self.order_type,
                "storeCode": self.store_code}

    def fetch_meals(self) -> Any:
        return self.client.call_tool_json("query-meals", self._base_args())

    def fetch_detail(self, code: str) -> Any:
        return self.client.call_tool_json(
            "query-meal-detail", {**self._base_args(), "code": str(code)})

    def fetch_coupons(self) -> Any:
        return self.client.call_tool_json("query-store-coupons", self._base_args())

    # ---------- 组装 ----------

    def build(self, with_details: bool = True,
              progress: Optional[Callable[[int, int], None]] = None) -> Catalog:
        """全量构建。with_details=False 时跳过套餐识别（全部当单品，仅供快速调试）。

        注意：query-meals 返回的是【当前时段】可售菜单（早餐/正餐/夜市轮换），
        券池随活动期轮换，因此构建完成后必须记录 fetched_at 供缓存新鲜度判断。
        """
        fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
        meals_raw = self.fetch_meals()
        products = parse_meals(meals_raw)
        for code, cname in parse_categories(meals_raw).items():
            if code in products:
                p = products[code]
                products[code] = Product(
                    code=p.code, name=p.name, price=p.price,
                    original_price=p.original_price, category=cname,
                    promo_type=p.promo_type, promo_price=p.promo_price)

        combos: Dict[str, Combo] = {}
        component_names: Dict[str, str] = {}
        degraded = 0
        if with_details:
            codes = list(products.keys())
            for i, code in enumerate(codes):
                if progress:
                    progress(i + 1, len(codes))
                p = products[code]
                try:
                    detail_raw = self.fetch_detail(code)
                except McpError as e:
                    # 单商品失败降级为单品处理（重试已在 client 层做过），不报废整个 build
                    degraded += 1
                    print(f"⚠️ {p.name}({code}) 详情拉取失败，按单品处理：{e}",
                          file=sys.stderr)
                    continue
                # 收集套餐轮次选项名（可能引用不在单品表的组件码）
                d = unwrap(detail_raw)
                if isinstance(d, dict):
                    for r in d.get("rounds") or []:
                        for ch in r.get("choices") or []:
                            ccode, cname = str(ch.get("code") or ""), str(ch.get("name") or "").strip()
                            if ccode and cname and ccode not in component_names:
                                component_names[ccode] = cname
                combo = parse_detail(code, p.name, p.price, detail_raw)
                if combo is not None:
                    combos[code] = combo
        if degraded:
            print(f"⚠️ 共 {degraded} 个商品详情拉取失败（已按单品处理，套餐优化可能不完整）",
                  file=sys.stderr)

        coupons = parse_coupons(self.fetch_coupons())
        return Catalog(
            store_code=self.store_code, store_name="",  # 门店名由上层从 store.json 填
            products=products, combos=combos, coupons=coupons,
            fetched_at=fetched_at, component_names=component_names)


# ---------- 缓存（Decimal 序列化为字符串） ----------

def catalog_to_dict(catalog: Catalog) -> dict:
    products = {
        code: {"code": p.code, "name": p.name, "price": str(p.price),
               "original_price": str(p.original_price), "category": p.category,
               "promo_type": p.promo_type, "promo_price": str(p.promo_price)}
        for code, p in catalog.products.items()
    }
    combos = {}
    for code, c in catalog.combos.items():
        combos[code] = {
            "code": c.code, "name": c.name, "base_price": str(c.base_price),
            "rounds": [
                {"name": r.name, "id": r.id,
                 "choices": [{"product_code": ch.product_code,
                              "diff_price": str(ch.diff_price),
                              "is_default": ch.is_default} for ch in r.choices]}
                for r in c.rounds
            ],
            "product_codes": sorted(c.product_codes),
        }
    coupons = [{"coupon_id": c.coupon_id, "title": c.title,
                "coupon_code": c.coupon_code,
                "product_codes": sorted(c.product_codes)} for c in catalog.coupons]
    return {"store_code": catalog.store_code, "store_name": catalog.store_name,
            "products": products, "combos": combos, "coupons": coupons,
            "fetched_at": catalog.fetched_at,
            "component_names": catalog.component_names}


def catalog_from_dict(d: dict) -> Catalog:
    products = {
        code: Product(code=p["code"], name=p["name"], price=Decimal(p["price"]),
                      original_price=Decimal(p["original_price"]),
                      category=p.get("category", ""),
                      promo_type=p.get("promo_type", ""),
                      promo_price=Decimal(p.get("promo_price", "0")))
        for code, p in d["products"].items()
    }
    combos = {}
    for code, c in d.get("combos", {}).items():
        rounds = tuple(
            ComboRound(name=r["name"], id=int(r.get("id") or 0), choices=tuple(
                ComboChoice(product_code=ch["product_code"],
                            diff_price=Decimal(ch["diff_price"]),
                            is_default=ch["is_default"])
                for ch in r["choices"]))
            for r in c["rounds"])
        combos[code] = Combo(code=c["code"], name=c["name"],
                             base_price=Decimal(c["base_price"]),
                             rounds=rounds, product_codes=frozenset(c["product_codes"]))
    coupons = tuple(
        Coupon(coupon_id=c["coupon_id"], title=c["title"],
               product_codes=frozenset(c["product_codes"]),
               coupon_code=c.get("coupon_code", ""))
        for c in d.get("coupons", []))
    return Catalog(store_code=d["store_code"], store_name=d.get("store_name", ""),
                   products=products, combos=combos, coupons=coupons,
                   fetched_at=d.get("fetched_at", ""),
                   component_names=d.get("component_names", {}))


def save_catalog(catalog: Catalog, path: str) -> None:
    """原子写（tmp + os.replace）：写一半崩溃不会留下损坏缓存。"""
    import os
    import tempfile
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(catalog_to_dict(catalog), f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def load_catalog(path: str) -> Catalog:
    with open(path, encoding="utf-8") as f:
        return catalog_from_dict(json.load(f))
