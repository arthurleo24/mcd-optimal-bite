# -*- coding: utf-8 -*-
"""cli 单测：items 语法、名称解析、渲染（全部离线）。"""
import unittest
from decimal import Decimal

from mcd_optimal_bite.cli import parse_items_expr, render_order, resolve_demand, _find_code
from mcd_optimal_bite.catalog import load_catalog
from mcd_optimal_bite.models import Catalog, Demand, Product, PurchaseAction, Solution
from pathlib import Path

TMP = Path(__file__).resolve().parents[2] / "tmp"


def D(v):
    return Decimal(str(v))


def tiny_catalog():
    products = {
        "A": Product(code="A", name="吉士蛋麦满分", price=D("10"), original_price=D("10")),
        "B": Product(code="B", name="脆薯饼", price=D("8"), original_price=D("8")),
        "C": Product(code="C", name="麦满分组合", price=D("12.5"), original_price=D("12.5")),
    }
    return Catalog(store_code="1", store_name="测试店", products=products, combos={})


class TestParseItemsExpr(unittest.TestCase):
    def test_x_syntax(self):
        self.assertEqual(parse_items_expr("吉士蛋麦满分×2,脆薯饼"),
                         [("吉士蛋麦满分", 2), ("脆薯饼", 1)])

    def test_ascii_x_and_star(self):
        self.assertEqual(parse_items_expr("A x3，B*2、C"),
                         [("A", 3), ("B", 2), ("C", 1)])

    def test_spaces(self):
        self.assertEqual(parse_items_expr(" 吉士蛋麦满分 × 2 "), [("吉士蛋麦满分", 2)])

    def test_empty(self):
        self.assertEqual(parse_items_expr(""), [])


class TestResolveDemand(unittest.TestCase):
    def test_exact_and_substring(self):
        cat = tiny_catalog()
        demand, unknown, amb = resolve_demand(cat, "吉士蛋麦满分×2,薯饼")
        self.assertEqual(demand.items, {"A": 2, "B": 1})
        self.assertEqual((unknown, amb), ([], []))

    def test_ambiguous(self):
        cat = tiny_catalog()
        # "麦满分" 同时命中 吉士蛋麦满分 和 麦满分组合
        demand, unknown, amb = resolve_demand(cat, "麦满分")
        self.assertEqual(demand.items, {})
        self.assertEqual(len(amb), 1)
        self.assertEqual(amb[0][0], "麦满分")
        self.assertEqual(len(amb[0][1]), 2)

    def test_unknown(self):
        cat = tiny_catalog()
        demand, unknown, amb = resolve_demand(cat, "星球杯")
        self.assertEqual(unknown, ["星球杯"])
        self.assertTrue(demand.is_empty())

    def test_real_cache_resolution(self):
        cat = load_catalog(TMP / "catalog_cache.json")
        demand, unknown, amb = resolve_demand(cat, "吉士蛋麦满分×2,脆薯饼×2")
        self.assertEqual((unknown, amb), ([], []))
        self.assertEqual(demand.items, {"5888": 2, "4825": 2})


class TestFindCode(unittest.TestCase):
    def test_priority_exact_over_substring(self):
        """精确匹配优先于子串：'脆薯饼' 精确命中 B"""
        cat = tiny_catalog()
        self.assertEqual(_find_code(cat, "脆薯饼"), "B")

    def test_multi_exact_defensive(self):
        cat = tiny_catalog()
        self.assertEqual(_find_code(cat, "不存在的"), None)


class TestVerifyDegradation(unittest.TestCase):
    """🔴回归：官方算价网络失败时降级为估算输出，不崩 CLI"""
    def test_order_degrades_when_verify_fails(self):
        import argparse
        import io
        import contextlib
        from unittest.mock import patch
        from mcd_optimal_bite import cli
        from mcd_optimal_bite.mcp_client import McpError

        class FailingClient:
            def __init__(self, *a, **k):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return None
            def call_tool_json(self, name, args):
                raise McpError("network boom", status=0)

        args = argparse.Namespace(
            cmd="order", store="1990526", items="吉士蛋麦满分×1",
            people=1, cache=str(TMP / "catalog_cache.json"),
            refresh=False, no_verify=False)
        buf = io.StringIO()
        with patch.object(cli, "McpClient", FailingClient):
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_order(args)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("官方算价失败", out)
        self.assertIn("最省方案", out)        # 方案仍然输出
        self.assertIn("¥10", out)             # 估算价仍在


class TestCacheUsable(unittest.TestCase):
    """🔴回归：缓存门店校验——跨门店缓存不得复用（菜单/价格因店而异）"""
    def test_store_mismatch_rejected(self):
        from mcd_optimal_bite.cli import _cache_usable
        cat = tiny_catalog()
        cat.store_code = "111"
        ok, note = _cache_usable(cat, "222")
        self.assertFalse(ok)
        self.assertIn("不一致", note)

    def test_same_store_ok(self):
        from mcd_optimal_bite.cli import _cache_usable
        cat = tiny_catalog()
        cat.store_code = "111"
        cat.fetched_at = "2026-10-10T09:00:00+08:00"
        ok, note = _cache_usable(cat, "111")
        self.assertTrue(ok)
        self.assertIn("缓存", note)


class TestRenderOrder(unittest.TestCase):
    def test_render_contains_sections(self):
        cat = tiny_catalog()
        sol = Solution(actions=(
            PurchaseAction(kind="single", label="吉士蛋麦满分", est_price=D("10"),
                           cover={"A": 2}),
        ), est_total=D("20"), baseline_total=D("20"))
        text = render_order(cat, sol, split_lines=["拆单建议X"], hint_lines=["提示Y"],
                            people=2, demand_str="吉士蛋麦满分×2",
                            catalog_src="缓存", official_line="官方算价 ¥20")
        self.assertIn("麦麦最优解", text)
        self.assertIn("最省方案", text)
        self.assertIn("[单品] 吉士蛋麦满分  ¥10", text)
        self.assertIn("官方算价 ¥20", text)
        self.assertIn("拆单建议X", text)
        self.assertIn("提示Y", text)
        self.assertIn("（2人）", text)


if __name__ == "__main__":
    unittest.main()
