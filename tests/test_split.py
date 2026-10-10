# -*- coding: utf-8 -*-
"""split（按券拆单）单测：合成券场景控制张数/份数，覆盖拆/不拆/警告分支。"""
import unittest
from decimal import Decimal

from mcd_optimal_bite.models import Catalog, Coupon, Demand, PurchaseAction, Solution
from mcd_optimal_bite.split import plan_split


def D(v):
    return Decimal(str(v))


def act(code, name, price, qty=1, kind="single"):
    return PurchaseAction(kind=kind, label=name, est_price=D(price),
                          cover={code: qty}, detail={"combo_code": code} if kind == "combo" else {})


def cat_with(n_coupons, product_codes=("NEW",)):
    coupons = tuple(
        Coupon(coupon_id=f"C{i}", title=f"新品券{i}",
               product_codes=frozenset(product_codes))
        for i in range(n_coupons))
    return Catalog(store_code="1", store_name="", products={}, combos={},
                   coupons=coupons)


class TestPlanSplit(unittest.TestCase):
    def test_split_when_3coupons_3items(self):
        """3 张券 + 3 份新品 -> 拆 3 单，每单 1 张券 1 份"""
        cat = cat_with(3)
        sol = Solution(actions=(act("NEW", "新品", 10), act("NEW", "新品", 10),
                                act("NEW", "新品", 10), act("OTHER", "薯条", 11)),
                       est_total=D("41"))
        plan = plan_split(cat, sol)
        self.assertTrue(plan.is_split)
        self.assertEqual(len(plan.suborders), 3)
        self.assertEqual(plan.eligible_qty, 3)
        self.assertEqual(plan.coupon_capacity, 3)
        for s in plan.suborders:
            self.assertIsNotNone(s.coupon)               # 每单挂券
            self.assertEqual(sum(a.cover.get("NEW", 0) for a in s.actions), 1)
        # 总价守恒
        self.assertEqual(plan.est_total, D("41"))
        self.assertIn("拆成 3 单", plan.reason)

    def test_no_split_when_1_coupon(self):
        """1 张券 + 3 份新品 -> 不拆（拆了也用不上券），给警告"""
        cat = cat_with(1)
        sol = Solution(actions=(act("NEW", "新品", 10), act("NEW", "新品", 10),
                                act("NEW", "新品", 10)),
                       est_total=D("30"))
        plan = plan_split(cat, sol)
        self.assertFalse(plan.is_split)
        self.assertEqual(len(plan.suborders), 1)
        self.assertIsNotNone(plan.suborders[0].coupon)   # 仅有的 1 张挂上
        self.assertTrue(any("只能用券 1 份" in w for w in plan.warnings))

    def test_no_split_when_1_item(self):
        """3 张券 + 1 份新品 -> 不拆（1 单即可用券）"""
        cat = cat_with(3)
        sol = Solution(actions=(act("NEW", "新品", 10),), est_total=D("10"))
        plan = plan_split(cat, sol)
        self.assertFalse(plan.is_split)
        self.assertEqual(plan.reason, "不满足拆单条件（券适用商品<2份或可用券<2张），整单下单")

    def test_no_coupon_items(self):
        """方案不含券适用商品 -> 整单无券"""
        cat = cat_with(3)
        sol = Solution(actions=(act("BURGER", "汉堡", 22),), est_total=D("22"))
        plan = plan_split(cat, sol)
        self.assertFalse(plan.is_split)
        self.assertIsNone(plan.suborders[0].coupon)

    def test_k_min_of_capacity_and_qty(self):
        """2 张券 + 5 份新品 -> 拆 2 单（K=min(2,5)）"""
        cat = cat_with(2)
        sol = Solution(actions=tuple(act("NEW", "新品", 10) for _ in range(5)),
                       est_total=D("50"))
        plan = plan_split(cat, sol)
        self.assertEqual(len(plan.suborders), 2)
        total_new = sum(a.cover.get("NEW", 0) for s in plan.suborders for a in s.actions)
        self.assertEqual(total_new, 5)

    def test_multi_unit_action_expands(self):
        """🔴回归：multi-unit single action（cover={NEW:3}）拆单必须展开成 3 份
        —— 否则 3 券 3 份新品只拆出 1 单，拆单建议失效"""
        cat = cat_with(3)
        sol = Solution(actions=(act("NEW", "新品", 30, qty=3),), est_total=D("30"))
        plan = plan_split(cat, sol)
        self.assertTrue(plan.is_split)
        self.assertEqual(len(plan.suborders), 3)
        for s in plan.suborders:
            self.assertEqual(sum(a.cover.get("NEW", 0) for a in s.actions), 1)
        # 总价精确守恒（余数给最后一份，不得有舍入差）
        self.assertEqual(plan.est_total, D("30"))
        prices = [a.est_price for s in plan.suborders for a in s.actions]
        self.assertEqual(sum(prices), D("30"))

    def test_empty_solution(self):
        plan = plan_split(cat_with(3), Solution(actions=(), est_total=D("0")))
        self.assertEqual(plan.suborders, ())
        self.assertTrue(any("方案为空" in w for w in plan.warnings))

    def test_combo_action_eligible(self):
        """套餐 action 覆盖含券适用码 -> 算券适用商品"""
        cat = cat_with(2)
        sol = Solution(actions=(
            act("C_A", "新品套餐", 25, kind="combo"),      # cover {C_A:1}
            act("C_B", "新品套餐2", 26, kind="combo"),
        ), est_total=D("51"))
        plan = plan_split(cat, sol)
        self.assertEqual(plan.eligible_qty, 0)   # cover 码不含 NEW -> 不适用券

        cat2 = cat_with(2, product_codes=("C_A", "C_B"))
        plan2 = plan_split(cat2, sol)
        self.assertEqual(plan2.eligible_qty, 2)
        self.assertTrue(plan2.is_split)


if __name__ == "__main__":
    unittest.main()
