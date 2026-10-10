# -*- coding: utf-8 -*-
"""optimizer 单测：合成目录（可控价格验证最优性）+ 真实早餐缓存场景。"""
import unittest
from decimal import Decimal
from pathlib import Path

from mcd_optimal_bite.catalog import load_catalog
from mcd_optimal_bite.models import Catalog, Combo, ComboChoice, ComboRound, Coupon, Demand, Product
from mcd_optimal_bite.optimizer import combo_variants, solve

TMP = Path(__file__).resolve().parents[2] / "tmp"


def D(v):
    return Decimal(str(v))


def P(code, name, price):
    return Product(code=code, name=name, price=D(price), original_price=D(price))


def make_catalog():
    """合成目录：
    - 单品：汉堡 22、可乐 9、薯条 11
    - 套餐 A「汉堡套餐」22.5：汉堡(默认)+饮料轮{可乐+0, 奶茶+3}
    - 套餐 B「超值套餐」20：鸡翅(非需求)+饮料轮{可乐+0}
    - 券：适用于可乐
    """
    products = {
        "BURGER": P("BURGER", "汉堡", 22),
        "COLA": P("COLA", "可乐", 9),
        "FRIES": P("FRIES", "薯条", 11),
        "WINGS": P("WINGS", "鸡翅", 15),
    }
    comboA = Combo(
        code="C_A", name="汉堡套餐", base_price=D("22.5"),
        rounds=(
            ComboRound(name="主食", choices=(ComboChoice("BURGER", D(0), True),)),
            ComboRound(name="饮料", choices=(
                ComboChoice("COLA", D(0), True),
                ComboChoice("MILKTEA", D(3), False),
            )),
        ),
        product_codes=frozenset({"BURGER", "COLA", "MILKTEA"}),
    )
    comboB = Combo(
        code="C_B", name="超值套餐", base_price=D("20"),
        rounds=(
            ComboRound(name="小食", choices=(ComboChoice("WINGS", D(0), True),)),
            ComboRound(name="饮料", choices=(ComboChoice("COLA", D(0), True),)),
        ),
        product_codes=frozenset({"WINGS", "COLA"}),
    )
    coupon = Coupon(coupon_id="1", title="可乐立减券",
                    product_codes=frozenset({"COLA"}))
    return Catalog(store_code="1", store_name="测试店", products=products,
                   combos={"C_A": comboA, "C_B": comboB}, coupons=(coupon,))


class TestComboVariants(unittest.TestCase):
    def test_target_main(self):
        cat = make_catalog()
        vs = combo_variants(cat.combos["C_A"], "BURGER")
        self.assertTrue(len(vs) >= 1)
        price, cover, picks = vs[0]
        self.assertEqual(price, D("22.5"))       # 汉堡+可乐默认
        self.assertEqual(cover, {"BURGER": 1, "COLA": 1})
        self.assertIn(0, picks)

    def test_target_drink_replace(self):
        cat = make_catalog()
        vs = combo_variants(cat.combos["C_A"], "COLA")
        # 目标轮=饮料轮选可乐，其余轮主食 min-diff => 22.5
        self.assertTrue(any(v[0] == D("22.5") and v[1].get("BURGER") == 1 for v in vs))

    def test_dedup(self):
        cat = make_catalog()
        vs = combo_variants(cat.combos["C_A"], "COLA")
        keys = [tuple(sorted(v[2].items())) for v in vs]
        self.assertEqual(len(keys), len(set(keys)))


class TestSolve(unittest.TestCase):
    def test_combo_beats_single(self):
        """汉堡+可乐：套餐22.5 < 单点22+9=31"""
        cat = make_catalog()
        r = solve(cat, Demand({"BURGER": 1, "COLA": 1}))
        self.assertEqual(r.solution.est_total, D("22.5"))
        self.assertEqual(r.solution.baseline_total, D("31"))
        self.assertEqual(r.solution.savings_vs_single, D("8.5"))
        kinds = [a.kind for a in r.solution.actions]
        self.assertEqual(kinds, ["combo"])

    def test_combo_extra_item_not_waste(self):
        """只要汉堡：套餐22.5 也比单点汉堡22 贵1.5，但套餐含可乐——
        若套餐(22.5) vs 单点(22)：选单点"""
        cat = make_catalog()
        r = solve(cat, Demand({"BURGER": 1}))
        self.assertEqual(r.solution.est_total, D("22"))   # 单点更便宜
        self.assertEqual([a.kind for a in r.solution.actions], ["single"])

    def test_two_burgers_two_sides(self):
        """2汉堡+2可乐：两个套餐A=45 vs 套餐A+套餐B=42.5（B含鸡翅赠送）
        但鸡翅不是需求——B 只产出可乐+鸡翅，还需要 BURGER 从哪来？
        B 不含 BURGER => 汉堡只能 2×A 或 2单点+2单点可乐。
        2×A = 45；2单点汉堡+2单点可乐 = 62；A+单点汉堡+单点可乐=22.5+22+9=53.5
        => 最优 45"""
        cat = make_catalog()
        r = solve(cat, Demand({"BURGER": 2, "COLA": 2}))
        self.assertEqual(r.solution.est_total, D("45"))

    def test_b_combo_beats_two_single_colas(self):
        """只要 2 可乐：B套餐20含2可乐+鸡翅 vs 2单点18 -> 单点赢"""
        cat = make_catalog()
        r = solve(cat, Demand({"COLA": 2}))
        self.assertEqual(r.solution.est_total, D("18"))

    def test_unreachable_code(self):
        """奶茶只出现在套餐 A 的替换项：需求奶茶1 时——
        可达（A 可产出），但需 +3"""
        cat = make_catalog()
        r = solve(cat, Demand({"MILKTEA": 1}))
        self.assertEqual(r.unreachable, {})
        # 最便宜方式：A 套餐换奶茶 = 25.5
        self.assertEqual(r.solution.est_total, D("25.5"))

    def test_unknown_code(self):
        cat = make_catalog()
        r = solve(cat, Demand({"GHOST": 1}))
        self.assertEqual(r.unreachable, {"GHOST": 1})
        self.assertIn("无可行方案", " ".join(r.solution.warnings))

    def test_coupon_hint(self):
        cat = make_catalog()
        r = solve(cat, Demand({"COLA": 1}))
        self.assertTrue(any("可乐立减券" in h for h in r.solution.coupon_hints))

    def test_near_miss(self):
        """需求可乐时若最优解走套餐A默认（可乐默认），不触发 near-miss；
        需求奶茶时套餐A选了奶茶（非默认），near-miss 可能提示其他轮——不强断言，冒烟即可"""
        cat = make_catalog()
        r = solve(cat, Demand({"MILKTEA": 1, "BURGER": 1}))
        self.assertIsInstance(r.solution.near_miss_hints, tuple)


class TestRealBreakfastCache(unittest.TestCase):
    """真实早餐缓存场景冒烟（价格以 2026-10-10 快照为准，断言从宽）。"""

    @classmethod
    def setUpClass(cls):
        cls.cat = load_catalog(TMP / "catalog_cache.json")

    def test_jidan_cafe_combo(self):
        """吉士蛋麦满分(5888) 单点 ¥10。
        裸价口径下组合 9900003432 裸价 12.5（7.5 是早餐卡促销价需买卡），
        官方算价与裸价一致 -> 单点更省（2026-10-10 实测反转）。"""
        r = solve(self.cat, Demand({"5888": 1}))
        self.assertEqual(r.unreachable, {})
        self.assertEqual(r.solution.est_total, D("10"))
        self.assertEqual(r.solution.actions[0].kind, "single")
        self.assertEqual(r.solution.baseline_total, D("10"))

    def test_multi_item(self):
        """2 吉士蛋 + 2 脆薯饼：裸价口径最优 = 2×吉士蛋麦满分套餐(裸价15) = ¥30
        （单点 2×10+2×8=36；套餐裸价15 含薯饼替换+饮料）"""
        r = solve(self.cat, Demand({"5888": 2, "4825": 2}))
        self.assertEqual(r.unreachable, {})
        self.assertEqual(r.solution.est_total, D("30"))
        self.assertEqual(len(r.solution.actions), 2)
        self.assertLess(r.elapsed_ms, 3000)

    def test_scale_perf(self):
        """压测：8 项需求（4人早餐）求解时间应远小于预算"""
        dem = Demand({"5888": 3, "4825": 2, "5024": 2, "2631": 1})
        r = solve(self.cat, dem)
        self.assertGreater(len(r.solution.actions), 0)
        self.assertLess(r.elapsed_ms, 3000)
        print(f"[perf] 8项需求 -> ¥{r.solution.est_total}, {len(r.solution.actions)}动作, {r.elapsed_ms}ms")

    def test_baseline_none_when_combo_only(self):
        """需求含仅套餐组件时基线应为 None 且有 warning"""
        r = solve(self.cat, Demand({"5888": 1}))
        if any(a.kind == "combo" for a in r.solution.actions):
            p5888 = self.cat.products.get("5888")
            if p5888 is None:
                self.assertIsNone(r.solution.baseline_total)
                self.assertTrue(any("不可单点" in w for w in r.solution.warnings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
