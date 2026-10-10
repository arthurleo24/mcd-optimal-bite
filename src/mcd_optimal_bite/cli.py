# -*- coding: utf-8 -*-
"""mcd_optimal_bite.cli — 命令行入口（零第三方依赖，argparse）

子命令：
  stores  搜门店   mcd-optimal-bite stores --city 苏州市 [--keyword 园区]
  order   算最省方案
          mcd-optimal-bite order --store 1990526 --items "吉士蛋麦满分×2,脆薯饼×2" --people 2

items 语法：商品名[×数量]，逗号/顿号分隔；数量支持 ×/x/X/*；缺省 1。
名称解析：精确匹配优先，其次唯一子串；多候选时报错并列出前 8 个。

Token：环境变量 MCD_MCP_TOKEN（绝不写进任何文件）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from .campaigns import active_campaigns, claimable_coupons, parse_available_coupons, summarize_coupons
from .catalog import CatalogBuilder, load_catalog, save_catalog
from .mcp_client import McpClient, McpError
from .models import Catalog, Demand, Solution
from .optimizer import solve
from .split import plan_split
from .verify import verify_solution, verify_split

CACHE_DEFAULT = Path(__file__).resolve().parents[2] / "tmp" / "catalog_cache.json"
_CACHE_MAX_AGE_H = 2.0  # 超过则提示 --refresh（菜单随时段轮换）


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------- items 语法与名称解析 ----------

def parse_items_expr(expr: str) -> List[Tuple[str, int]]:
    """'吉士蛋麦满分×2, 脆薯饼 x1' -> [("吉士蛋麦满分",2), ("脆薯饼",1)]"""
    out: List[Tuple[str, int]] = []
    for part in re.split(r"[,，、]", expr or ""):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.*?)\s*[×xX*]\s*(\d+)$", part)
        if m and m.group(1).strip():
            out.append((m.group(1).strip(), int(m.group(2))))
        else:
            out.append((part, 1))
    return out


def _find_code(catalog: Catalog, name: str):
    """解析优先级：单品精确 > 组件精确 > 单品子串 > 组件子串。
    （用户说"汉堡包"应命中 12.5 的经典汉堡组件，而非"吉士汉堡包套餐"）
    命中返回 code；未知返回 None；多候选返回 code 列表。"""
    exact = sorted(c for c, p in catalog.products.items() if p.name == name)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact
    comp_exact = sorted(c for c, n in catalog.component_names.items() if n == name)
    if comp_exact:
        # 同名组件多个码时，优先可单点的那个（在 products 表中）
        in_products = [c for c in comp_exact if c in catalog.products]
        if len(in_products) == 1:
            return in_products[0]
        if len(comp_exact) == 1:
            return comp_exact[0]
        return comp_exact
    subs = sorted(c for c, p in catalog.products.items() if name in p.name)
    if len(subs) == 1:
        return subs[0]
    if len(subs) > 1:
        return subs
    comp_subs = sorted(c for c, n in catalog.component_names.items() if name in n)
    if len(comp_subs) == 1:
        return comp_subs[0]
    if len(comp_subs) > 1:
        return comp_subs
    return None


def resolve_demand(catalog: Catalog, expr: str) -> Tuple[Demand, List[str], List[Tuple[str, List[str]]]]:
    """items 表达式 -> (需求, 未知名称, 歧义[名称, 候选])"""
    demand = Demand()
    unknown: List[str] = []
    ambiguous: List[Tuple[str, List[str]]] = []
    for name, qty in parse_items_expr(expr):
        hit = _find_code(catalog, name)
        if hit is None:
            unknown.append(name)
        elif isinstance(hit, list):
            ambiguous.append((name, hit[:8]))
        else:
            demand.items[hit] = demand.items.get(hit, 0) + qty
    return demand, unknown, ambiguous


# ---------- 目录加载 ----------

def _cache_usable(cat: Catalog, store_code: str) -> Tuple[bool, str]:
    """缓存可用性判定（纯函数）：门店必须一致，超龄仅提示不拒用。"""
    age_h = _cache_age_hours(cat.fetched_at)
    if cat.store_code != store_code:
        return False, (f"缓存门店({cat.store_code or '未知'})与请求门店({store_code})不一致，"
                       f"菜单/价格因店而异，重新拉取")
    note = f"缓存 {cat.fetched_at or '未知时间'}（{age_h:.1f}小时前）"
    if age_h > _CACHE_MAX_AGE_H:
        note += f" ⚠️超过{_CACHE_MAX_AGE_H}小时，菜单随时段轮换，建议 --refresh"
    return True, note


def _load_or_build(catalog_cache: Path, refresh: bool, store_code: str) -> Tuple[Catalog, str]:
    """返回 (catalog, 来源说明)。缓存跨门店时重建（菜单/价格因店而异）。"""
    if not refresh and catalog_cache.exists():
        cat = load_catalog(str(catalog_cache))
        ok, note = _cache_usable(cat, store_code)
        if ok:
            return cat, note
    with McpClient() as client:
        cat = CatalogBuilder(client, store_code=store_code).build()
    save_catalog(cat, str(catalog_cache))
    cat.store_code = store_code
    return cat, f"实时拉取（已缓存到 {catalog_cache.name}）"


def _cache_age_hours(fetched_at: str) -> float:
    if not fetched_at:
        return 1e9
    try:
        dt = datetime.fromisoformat(fetched_at)
        return max(0.0, (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600)
    except ValueError:
        return 1e9


# ---------- 渲染 ----------

def _name(catalog: Catalog, code: str) -> str:
    p = catalog.products.get(code)
    if p:
        return p.name
    return catalog.component_names.get(code, code)


def _cover_str(catalog: Catalog, act) -> str:
    return " ".join(f"{_name(catalog, c)}×{q}" for c, q in sorted(act.cover.items()))


def render_order(catalog: Catalog, solution: Solution, split_lines: List[str],
                 hint_lines: List[str], people: int, demand_str: str,
                 catalog_src: str, official_line: Optional[str],
                 campaign_lines: Optional[List[str]] = None) -> str:
    out: List[str] = []
    out.append("═" * 46)
    out.append(" 麦麦最优解")
    out.append("═" * 46)
    out.append(f"需求: {demand_str}" + (f"（{people}人）" if people > 1 else ""))
    out.append(f"目录: {catalog_src}（{len(catalog.products)}商品/{len(catalog.combos)}套餐/{len(catalog.coupons)}券）")
    out.append("")
    out.append("── 最省方案 ──")
    if not solution.actions:
        out.append("（无可行方案）")
    for i, act in enumerate(solution.actions, 1):
        out.append(f"{i}. [{ '套餐' if act.kind == 'combo' else '单品' }] {act.label}  ¥{act.est_price}")
        out.append(f"   含: {_cover_str(catalog, act)}")
    base = solution.baseline_total
    line = f"估算合计 ¥{solution.est_total}"
    if official_line:
        line += f"   | {official_line}"
    if base is not None:
        line += f"   | 比全单点省 ¥{base - solution.est_total}"
    out.append(line)
    if split_lines:
        out.append("")
        out.append("── 按券拆单建议 ──")
        out.extend(split_lines)
    if campaign_lines:
        out.append("")
        out.append("── 活动提醒 ──")
        out.extend(campaign_lines)
    if hint_lines:
        out.append("")
        out.append("── 提示 ──")
        out.extend(hint_lines)
    out.append("═" * 46)
    return "\n".join(out)


def promo_hints(catalog: Catalog, solution: Solution) -> List[str]:
    """随单购卡促销提示（v1 不自动加购卡，是否划算取决于复购）。"""
    hints: List[str] = []
    seen = set()
    for act in solution.actions:
        codes = list(act.cover.keys())
        if act.kind == "combo" and act.detail:
            codes.append(act.detail.get("combo_code", ""))
        for code in codes:
            p = catalog.products.get(code)
            if p is None or p.promo_type != "随单购早餐卡优惠" or code in seen:
                continue
            seen.add(code)
            hints.append(
                f"{p.name} 官方裸价 ¥{p.price}；购「随单购早餐卡」后可享 ¥{p.promo_price}"
                f"（卡价以官方为准，是否划算取决于复购频次）")
    return hints


# ---------- 子命令 ----------

def cmd_stores(args) -> int:
    with McpClient() as client:
        data = client.call_tool_json("query-nearby-stores", {
            "beType": 1, "searchType": 2, "city": args.city,
            **({"keyword": args.keyword} if args.keyword else {})})
    stores = data.get("data") if isinstance(data, dict) else data
    if not stores:
        print("未找到门店")
        return 1
    print(f"共 {len(stores)} 家门店：")
    for s in stores[:15]:
        print(f"  {s.get('storeCode')}  {s.get('storeName')}  "
              f"{s.get('address', '')}（{s.get('distance', '?')}m）")
    return 0


def cmd_coupons(args) -> int:
    """麦麦省券位概览：可领 / 已领。--claim 显式领取全部可领券。"""
    with McpClient() as client:
        text = client.content_text(client.call_tool("available-coupons", {}))
        summary = summarize_coupons(text)
        print(f"麦麦省券位：共 {summary['total']} 张 | 待领 {summary['claimable']} | 已领 {summary['claimed']}")
        items = parse_available_coupons(text)
        for i, c in enumerate(items[:15], 1):
            mark = "✅已领" if not c.claimable else "👉待领"
            print(f"  {i}. [{mark}] {c.title}")
        if summary["claimable"]:
            if args.claim:
                print("\n--claim：调用 auto-bind-coupons 领取全部可领券…")
                try:
                    resp = client.call_tool("auto-bind-coupons", {})
                    out = client.content_text(resp)
                    print(out[:600] or "（服务端无文本返回，请到麦当劳 App 确认券位）")
                except McpError as e:
                    print(f"⚠️ 领取失败：{e}（可到麦当劳 App 手动领取）")
                    return 1
            else:
                print(f"\n💡 有 {summary['claimable']} 张券待领取：加 --claim 一键领取，或到麦当劳 App 领取")
    return 0


def cmd_order(args) -> int:
    cache = Path(args.cache) if args.cache else CACHE_DEFAULT
    catalog, src = _load_or_build(cache, args.refresh, args.store)
    catalog.store_code = args.store

    demand, unknown, ambiguous = resolve_demand(catalog, args.items)
    if unknown:
        print(f"❌ 未找到商品：{'、'.join(unknown)}（用 stores 子命令确认门店后重试，或检查名称）")
        return 1
    for name, cands in ambiguous:
        print(f"❌ 「{name}」有多个候选，请用全名：")
        for c in cands:
            print(f"   - {_name(catalog, c)}"
                  + ("" if c in catalog.products else "（仅随套餐提供）"))
        return 1
    if demand.is_empty():
        print("❌ 需求为空")
        return 1

    result = solve(catalog, demand)
    sol = result.solution
    if not sol.actions:
        print("❌ 无可行方案：")
        for w in sol.warnings:
            print(f"   {w}")
        if result.unreachable:
            print(f"   不可得商品：{'、'.join(_name(catalog, c) for c in result.unreachable)}")
        return 1

    official_line = None
    degrade_notes: List[str] = []
    split_lines: List[str] = []
    campaign_lines: List[str] = []
    plan = plan_split(catalog, sol)

    if not args.no_verify:
        try:
            with McpClient() as client:
                report = verify_solution(client, catalog, sol)
                official_line = f"官方算价 ¥{sol.official_price}" + (
                    f"（优惠 ¥{sol.official_discount}）" if sol.official_discount else "")
                if abs(report.diff) >= Decimal("0.01"):
                    official_line += f" ⚠️与估算差 ¥{report.diff:+}"

                # 活动提醒（非核心，失败静默）
                if not getattr(args, "no_campaigns", False):
                    try:
                        cal_text = client.content_text(
                            client.call_tool("campaign-calendar", {}))
                        acts = active_campaigns(cal_text)
                        if acts:
                            campaign_lines.append("── 活动提醒 ──")
                            campaign_lines += [
                                f"  [{c.date}] {c.title}" for c in acts]
                    except Exception:
                        pass

                if plan.is_split:
                    reports = verify_split(client, catalog, plan)
                    split_lines.append(plan.reason)
                    for sub, rep in zip(plan.suborders, reports):
                        line = (f"  子单{sub.index}（{sub.coupon_label}）："
                                f"估算 ¥{sub.est_total} -> 官方 ¥{rep.official_total}")
                        if rep.official_discount:
                            line += f"（券抵 ¥{rep.official_discount}）"
                        split_lines.append(line)
                    split_total = sum(r.official_total for r in reports)
                    compare = f"  拆单合计官方 ¥{split_total}"
                    if sol.official_price is not None:
                        delta = sol.official_price - split_total
                        compare += f" vs 整单官方 ¥{sol.official_price}"
                        compare += ("  ✅拆单更省 ¥%s" % delta) if delta > 0 else \
                                   ("  ⚠️拆单不省（贵 ¥%s），建议整单" % -delta) if delta < 0 else "  持平"
                    split_lines.append(compare)
        except McpError as e:
            official_line = "官方算价失败（以下为估算价）"
            degrade_notes.append(f"⚠️ 官方算价失败：{e}")
    elif plan.is_split:
        split_lines.append(plan.reason)
        for sub in plan.suborders:
            acts = "；".join(f"{a.label} ¥{a.est_price}" for a in sub.actions)
            split_lines.append(f"  子单{sub.index}（{sub.coupon_label}）：{acts}")

    if not split_lines and plan.warnings:
        split_lines.extend(f"  {w}" for w in plan.warnings)

    hints = promo_hints(catalog, sol)
    hints += list(sol.near_miss_hints)
    hints += [f"有券提示：{h}" for h in sol.coupon_hints]
    hints += [f"⚠️ {w}" for w in sol.warnings if "不可得" in w or "不可单点" in w]
    hints.extend(degrade_notes)

    demand_str = "、".join(
        f"{_name(catalog, c)}×{q}" for c, q in sorted(demand.items.items()))
    print(render_order(catalog, sol, split_lines, hints,
                       args.people, demand_str, src, official_line,
                       campaign_lines=campaign_lines))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    _utf8()
    parser = argparse.ArgumentParser(prog="mcd-optimal-bite",
                                     description="麦麦最优解 — 说想吃啥，算出最省的官方点单方案")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stores = sub.add_parser("stores", help="搜附近门店（到店自取）")
    p_stores.add_argument("--city", required=True, help="城市，如 苏州市")
    p_stores.add_argument("--keyword", default="", help="门店名关键字，可选")
    p_stores.set_defaults(func=cmd_stores)

    p_coupons = sub.add_parser("coupons", help="麦麦省券位概览（可领/已领）")
    p_coupons.add_argument("--claim", action="store_true",
                           help="一键领取全部待领券（涉账户变更，显式确认才执行）")
    p_coupons.set_defaults(func=cmd_coupons)

    p_order = sub.add_parser("order", help="算最省点单方案")
    p_order.add_argument("--store", required=True, help="门店编码（stores 子命令查询）")
    p_order.add_argument("--items", required=True,
                         help='想吃的东西，如 "吉士蛋麦满分×2,脆薯饼"')
    p_order.add_argument("--people", type=int, default=1, help="吃饭人数（影响拆单提示）")
    p_order.add_argument("--cache", default=None, help="目录缓存路径")
    p_order.add_argument("--refresh", action="store_true", help="强制重拉菜单目录")
    p_order.add_argument("--no-verify", action="store_true", help="跳过官方算价复核")
    p_order.add_argument("--no-campaigns", action="store_true", help="跳过活动日历提醒")
    p_order.set_defaults(func=cmd_order)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
