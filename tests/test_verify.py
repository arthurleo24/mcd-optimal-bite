# -*- coding: utf-8 -*-
"""verify（官方复核层）单测：mock client 离线验证 items 翻译与汇报逻辑。"""
import unittest
from decimal import Decimal

from mcd_optimal_bite.catalog import load_catalog
from mcd_optimal_bite.models import Catalog, Coupon, Demand, Product, PurchaseAction, Solution
from mcd_optimal_bite.split import plan_split
from mcd_optimal_bite.verify import build_items, verify_actions, verify_solution
from pathlib import Path

TMP = Path(__file__).resolve().parents[2] / "tmp"


def D(v):
    return Decimal(str(v))


class FakeClient:
    """记录请求、返回预置算价。"""
    def __init__(self, price_cents=1500, discount_cents=0):
        self.calls = []
        self.price_cents = price_cents
        self.discount_cents = discount_cents

    def call_tool_json(self, name, args):
        self.calls.append((name, args))
        assert name == "calculate-price"
        return {"success": True, "data": {"price": self.price_cents,
                                          "discount": self.discount_cents}}


def make_catalog_with_coupon():
    p = Product(code="NEW", name="新品", price=D("10"), original_price=D("10"))
    coupon = Coupon(coupon_id="CID1", title="新品券", product_codes=frozenset({"NEW"}),
                    coupon_code="CCODE1")
    return Catalog(store_code="1990526", store_name="", products={"NEW": p},
                   combos={}, coupons=(coupon,))


class TestBuildItems(unittest.TestCase):
    def test_single_and_combo(self):
        """单品 + 套餐（含替换选项 roundList）"""
        cat = load_catalog(TMP / "catalog_cache.json")
        from mcd_optimal_bite.models import Combo, ComboChoice, ComboRound
        combo = cat.combos["9900003432"]  # 吉士蛋麦满分组合
        # 人为改一下 roundList 场景：直接用该套餐 + picks 替换
        action = PurchaseAction(
            kind="combo", label=combo.name, est_price=D("7.5"),
            cover={"5888": 1, "1088": 1},
            detail={"combo_code": "9900003432", "picks": {0: "5888", 1: "3050"}})
        single = PurchaseAction(kind="single", label="脆薯饼", est_price=D("8"),
                                cover={"4825": 1})
        items = build_items(cat, (single, action))
        self.assertEqual(items[0], {"productCode": "4825", "quantity": 1})
        it = items[1]
        self.assertEqual(it["productCode"], "9900003432")
        self.assertEqual(it["quantity"], 1)
        self.assertEqual(len(it["roundList"]), 2)
        r0 = it["roundList"][0]
        # round 必须传轮次 id（缓存无 id 时回退为序号字符串）
        self.assertEqual(r0["round"], str(combo.rounds[0].id or 1))
        self.assertEqual(r0["comboItemList"], [{"code": "5888", "quantity": 1}])

    def test_coupon_attached_to_eligible(self):
        cat = make_catalog_with_coupon()
        items = build_items(cat, (PurchaseAction(kind="single", label="新品",
                                                 est_price=D("10"), cover={"NEW": 2}),),
                            coupon_id="CID1", coupon_code="CCODE1")
        self.assertEqual(items[0]["couponId"], "CID1")
        self.assertEqual(items[0]["couponCode"], "CCODE1")

    def test_coupon_attached_to_matching_coupon_only(self):
        """🔴回归：多券场景，券A（适用X）不能挂到券B适用商品 Y 上"""
        cat = make_catalog_with_coupon()  # 券适用 NEW
        p2 = Product(code="FRIES", name="薯条", price=D("11"), original_price=D("11"))
        cat.products["FRIES"] = p2
        # 指定券只适用 FRIES：NEW item 不得挂券，FRIES item 挂券
        items = build_items(cat, (
            PurchaseAction(kind="single", label="新品", est_price=D("10"), cover={"NEW": 1}),
            PurchaseAction(kind="single", label="薯条", est_price=D("11"), cover={"FRIES": 1}),
        ), coupon_id="CID1", coupon_code="CCODE1", coupon_product_codes=frozenset({"FRIES"}))
        self.assertNotIn("couponId", items[0])
        self.assertEqual(items[1]["couponId"], "CID1")
        self.assertEqual(items[1]["productCode"], "FRIES")

    def test_coupon_skipped_when_not_eligible(self):
        cat = make_catalog_with_coupon()
        p2 = Product(code="FRIES", name="薯条", price=D("11"), original_price=D("11"))
        cat.products["FRIES"] = p2
        items = build_items(cat, (PurchaseAction(kind="single", label="薯条",
                                                 est_price=D("11"), cover={"FRIES": 1}),),
                            coupon_id="CID1")
        self.assertNotIn("couponId", items[0])


class TestVerifyActions(unittest.TestCase):
    def test_price_conversion_and_diff_warning(self):
        """分 -> 元；估算差 >0.01 出警告"""
        cat = make_catalog_with_coupon()
        client = FakeClient(price_cents=950, discount_cents=50)
        acts = (PurchaseAction(kind="single", label="新品", est_price=D("10"),
                               cover={"NEW": 1}),)
        r = verify_actions(client, cat, acts, est_total=D("10"))
        self.assertEqual(r.official_total, D("9.5"))
        self.assertEqual(r.official_discount, D("0.5"))
        self.assertEqual(len(client.calls), 1)
        args = client.calls[0][1]
        self.assertEqual(args["storeCode"], "1990526")
        self.assertEqual(args["items"][0]["productCode"], "NEW")
        self.assertTrue(any("以官方为准" in w for w in r.warnings))

    def test_no_warning_when_match(self):
        cat = make_catalog_with_coupon()
        client = FakeClient(price_cents=1000)
        acts = (PurchaseAction(kind="single", label="新品", est_price=D("10"),
                               cover={"NEW": 1}),)
        r = verify_actions(client, cat, acts, est_total=D("10"))
        self.assertEqual(r.warnings, ())
        self.assertEqual(r.diff, D("0"))


class TestVerifySolution(unittest.TestCase):
    def test_writes_back_to_solution(self):
        cat = make_catalog_with_coupon()
        client = FakeClient(price_cents=1000, discount_cents=100)
        sol = Solution(actions=(PurchaseAction(kind="single", label="新品",
                                               est_price=D("10"), cover={"NEW": 1}),),
                       est_total=D("10"))
        verify_solution(client, cat, sol)
        self.assertEqual(sol.official_price, D("10"))
        self.assertEqual(sol.official_discount, D("1"))
        self.assertEqual(sol.final_price, D("10"))


if __name__ == "__main__":
    unittest.main()
