# -*- coding: utf-8 -*-
"""catalog 解析纯函数单测：全部喂 tmp/ 真实探针样本，不调网络。"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from mcd_optimal_bite.catalog import (
    catalog_from_dict,
    catalog_to_dict,
    parse_categories,
    parse_coupons,
    parse_detail,
    parse_diff_price,
    parse_meals,
    unwrap,
)
from mcd_optimal_bite.mcp_client import extract_json
from mcd_optimal_bite.models import Catalog

TMP = Path(__file__).resolve().parents[2] / "tmp"


def load(name):
    with open(TMP / name, encoding="utf-8") as f:
        return json.load(f)


class TestUnwrap(unittest.TestCase):
    def test_success_wrapper(self):
        self.assertEqual(unwrap({"success": True, "data": [1, 2]}), [1, 2])

    def test_bare_list(self):
        self.assertEqual(unwrap([1, 2]), [1, 2])

    def test_bare_dict_without_data_key(self):
        self.assertEqual(unwrap({"a": 1}), {"a": 1})


class TestDiffPrice(unittest.TestCase):
    def test_plus(self):
        self.assertEqual(parse_diff_price("+ ¥1.5"), Decimal("1.5"))

    def test_minus(self):
        self.assertEqual(parse_diff_price("- ¥1.5"), Decimal("-1.5"))

    def test_zero(self):
        self.assertEqual(parse_diff_price("+ ¥0"), Decimal("0"))

    def test_bare_number_str(self):
        self.assertEqual(parse_diff_price("2"), Decimal("2"))

    def test_number(self):
        self.assertEqual(parse_diff_price(2.5), Decimal("2.5"))

    def test_none(self):
        self.assertEqual(parse_diff_price(None), Decimal("0"))

    def test_garbage(self):
        self.assertEqual(parse_diff_price("不优惠"), Decimal("0"))


class TestParseMeals(unittest.TestCase):
    def test_real_sample(self):
        products = parse_meals(load("meals.json"))
        self.assertEqual(len(products), 107)
        p = products["9900005456"]
        self.assertEqual(p.name, "麦辣鸡腿汉堡三件套")
        self.assertEqual(p.price, Decimal("34"))
        self.assertEqual(p.original_price, Decimal("47.5"))

    def test_bare_dict(self):
        products = parse_meals({"1": {"name": "巨无霸", "currentPrice": "25", "originalPrice": None}})
        self.assertEqual(products["1"].price, Decimal("25"))
        self.assertEqual(products["1"].original_price, Decimal("0"))


class TestParseCategories(unittest.TestCase):
    def test_real_sample_full_cover(self):
        cats = parse_categories(load("meals.json"))
        self.assertEqual(len(cats), 107)
        self.assertNotIn("\n", "".join(cats.values()))
        # 换行清理：'鸡肉汉堡\n/卷' -> '鸡肉汉堡/卷'
        self.assertEqual(cats["9900005456"], "鸡肉汉堡/卷")
        # 多分类保序拼接：鸡薯双全盒同时属人气热卖+小食拼盘/多人餐
        self.assertEqual(cats["9900016076"], "人气热卖|小食拼盘/多人餐")


class TestParseDetail(unittest.TestCase):
    def test_real_sample(self):
        raw = load("meal_detail_sample.json")["raw"]       # 指令注入 + JSON 混合文本
        data = extract_json(raw)                            # 剥离提示词
        combo = parse_detail("9900005456", "麦辣鸡腿汉堡三件套", Decimal("34"), data)
        self.assertIsNotNone(combo)
        self.assertEqual(len(combo.rounds), 3)
        # 第 1 轮：麦辣鸡腿汉堡，默认，差价 0
        r0 = combo.rounds[0]
        self.assertEqual(r0.name, "麦辣鸡腿汉堡")
        self.assertEqual(r0.choices[0].product_code, "1440")
        self.assertEqual(r0.choices[0].diff_price, Decimal("0"))
        self.assertTrue(r0.choices[0].is_default)
        # 第 3 轮 22 个选项
        self.assertEqual(len(combo.rounds[2].choices), 22)
        # 负差价：小杯玉米杯 -1.5
        corn = [c for c in combo.rounds[1].choices if c.product_code == "4437"][0]
        self.assertEqual(corn.diff_price, Decimal("-1.5"))
        # 默认组合覆盖：3 轮各 1 项
        cover = combo.default_cover()
        self.assertEqual(cover, {"1440": 1, "4810": 1, "3050": 1})
        # price_for：中薯条换大薯条 +1.5，可乐换雪碧 +0
        self.assertEqual(combo.price_for({0: "1440", 1: "4820", 2: "3060"}), Decimal("35.5"))

    def test_single_item_no_rounds(self):
        self.assertIsNone(parse_detail("1", "派", Decimal("5"), {"code": "1", "rounds": []}))


class TestParseCoupons(unittest.TestCase):
    def test_real_sample(self):
        coupons = parse_coupons(load("coupons.json"))
        self.assertEqual(len(coupons), 2)
        titles = {c.title for c in coupons}
        self.assertEqual(titles, {"麦旋风任选", "薯薯任选"})
        swirl = [c for c in coupons if c.title == "麦旋风任选"][0]
        self.assertEqual(swirl.product_codes, frozenset({"9900014239"}))

    def test_empty(self):
        self.assertEqual(parse_coupons([]), ())


class TestCacheRoundtrip(unittest.TestCase):
    def test_to_from_dict(self):
        products = parse_meals(load("meals.json"))
        cats = parse_categories(load("meals.json"))
        for code, cname in cats.items():
            if code in products:
                from mcd_optimal_bite.models import Product
                p = products[code]
                products[code] = Product(code=p.code, name=p.name, price=p.price,
                                         original_price=p.original_price, category=cname)
        raw = load("meal_detail_sample.json")["raw"]
        combo = parse_detail("9900005456", "麦辣鸡腿汉堡三件套", Decimal("34"), extract_json(raw))
        coupons = parse_coupons(load("coupons.json"))
        cat = Catalog(store_code="1990526", store_name="测试店",
                      products=products, combos={"9900005456": combo}, coupons=coupons)
        rt = catalog_from_dict(catalog_to_dict(cat))
        self.assertEqual(rt.store_code, "1990526")
        self.assertEqual(len(rt.products), 107)
        self.assertEqual(rt.products["9900005456"].category, products["9900005456"].category)
        c2 = rt.combos["9900005456"]
        self.assertEqual(c2.base_price, Decimal("34"))
        self.assertEqual(len(c2.rounds), 3)
        self.assertEqual(c2.price_for({0: "1440", 1: "4820", 2: "3060"}), Decimal("35.5"))
        self.assertEqual(len(rt.coupons), 2)

    def test_fetched_at_roundtrip(self):
        cat = Catalog(store_code="1", store_name="", products={}, combos={},
                      fetched_at="2026-10-10T08:50:00+08:00")
        rt = catalog_from_dict(catalog_to_dict(cat))
        self.assertEqual(rt.fetched_at, "2026-10-10T08:50:00+08:00")


if __name__ == "__main__":
    unittest.main()
