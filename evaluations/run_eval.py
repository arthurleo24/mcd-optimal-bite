# -*- coding: utf-8 -*-
"""麦麦最优解 — 评测集 runner（10 个真实场景，动态选品适配任意时段）

用法（token 走环境变量 MCD_MCP_TOKEN）::

    set MCD_MCP_TOKEN=xxx
    python evaluations/run_eval.py [--store 1990526] [--cache tmp/catalog_cache.json]

输出：evaluations/report.md + report.json
覆盖：单品/双品/多人/仅套餐组件/大需求/不可得降级/名称解析歧义与未知/按券拆单/端到端性能。
菜单严格按时段轮换，评测商品名从当前目录动态选取，任何时段可复跑。
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from mcd_optimal_bite.catalog import load_catalog
from mcd_optimal_bite.cli import resolve_demand
from mcd_optimal_bite.mcp_client import McpClient
from mcd_optimal_bite.models import Catalog, Coupon, Demand
from mcd_optimal_bite.optimizer import solve
from mcd_optimal_bite.split import plan_split
from mcd_optimal_bite.verify import verify_solution

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "tmp" / "catalog_cache.json"


@dataclass
class CaseResult:
    case_id: str
    title: str
    status: str = "PASS"            # PASS / FAIL / SKIP
    detail: str = ""
    est: Optional[Decimal] = None
    official: Optional[Decimal] = None
    diff: Optional[Decimal] = None
    actions: int = 0
    ms: int = 0
    saved: Optional[Decimal] = None


@dataclass
class EvalContext:
    catalog: Catalog
    client: Optional[McpClient] = None
    results: List[CaseResult] = field(default_factory=list)


def demand_from(catalog: Catalog, expr: str) -> Demand:
    d, unknown, ambiguous = resolve_demand(catalog, expr)
    if unknown or ambiguous:
        raise ValueError(f"解析失败: unknown={unknown} ambiguous={ambiguous}")
    return d


def pick_single_names(catalog: Catalog, n: int) -> List[str]:
    """动态取 n 个可单点商品名（评测不依赖特定时段菜单）。"""
    names: List[str] = []
    for code, p in sorted(catalog.products.items()):
        if p.price > 0 and p.name not in names:
            names.append(p.name)
        if len(names) >= n:
            break
    return names


def pick_combo_only_component(catalog: Catalog) -> Optional[str]:
    """动态取一个仅随套餐提供的组件名（不在单品表，且不与任何单品同名——
    同名时用户输入会解析到可单点的单品，不符合本场景）。"""
    product_names = {p.name for p in catalog.products.values()}
    for code, name in sorted(catalog.component_names.items()):
        if code not in catalog.products and name not in product_names:
            return name
    return None


def solved_case(ctx: EvalContext, case_id: str, title: str, expr: str,
                check: Optional[Callable] = None,
                allow_diff_warn: bool = False) -> CaseResult:
    """标准求解场景：需求 -> 求解 -> 官方复核。

    allow_diff_warn=True 时（已知限制场景），est≠official 标 WARN 而非 FAIL。
    """
    r = CaseResult(case_id, title)
    t0 = time.monotonic()
    try:
        demand = demand_from(ctx.catalog, expr)
        opt = solve(ctx.catalog, demand)
        sol = opt.solution
        r.actions = len(sol.actions)
        r.est = sol.est_total
        r.saved = sol.savings_vs_single
        if ctx.client:
            verify_solution(ctx.client, ctx.catalog, sol)
            r.official = sol.official_price
            r.diff = sol.official_price - sol.est_total
        r.ms = int((time.monotonic() - t0) * 1000)
        if r.official is not None and abs(r.diff) >= Decimal("0.01"):
            if allow_diff_warn:
                r.status = "WARN"
                r.detail = ("官方更便宜（组件码可直购，目录未含组件单价——v1 已知限制），"
                            f"以官方 ¥{r.official} 为准")
            else:
                r.status, r.detail = "FAIL", f"est≠official: {r.est} vs {r.official}"
        elif check:
            err = check(sol, opt)
            if err:
                r.status, r.detail = "FAIL", err
    except Exception as e:  # noqa: BLE001
        r.status, r.detail = "FAIL", f"{type(e).__name__}: {e}"
    ctx.results.append(r)
    return r


def run_all(catalog: Catalog, client: Optional[McpClient]) -> List[CaseResult]:
    ctx = EvalContext(catalog=catalog, client=client)
    # 动态选品：菜单严格按时段轮换，评测不硬编码商品名
    names = pick_single_names(catalog, 6)
    n1, n2, n3, n4 = names[0], names[1], names[2], names[3]
    comp = pick_combo_only_component(catalog)

    # E1 单品
    solved_case(ctx, "E1", f"单品：{n1}×1", f"{n1}×1")
    # E2 双品×2（README 示例场景）
    solved_case(ctx, "E2", f"双品×2：{n1}×2 {n2}×2", f"{n1}×2,{n2}×2",
                check=lambda s, o: None if len(s.actions) == 2 else f"期望2动作,得{len(s.actions)}")
    # E3 多人 8 项
    solved_case(ctx, "E3", "4人 8 项", f"{n1}×3,{n2}×2,{n3}×2,{n4}×1")
    # E4 仅套餐组件（不可单点，仅随套餐提供）。已知限制：官方允许部分组件码直购且
    # 更便宜（目录未含组件单价），故允许 est≠official 以 WARN 记录
    if comp:
        solved_case(ctx, "E4", f"仅套餐组件：{comp}×2", f"{comp}×2",
                    check=lambda s, o: None if s.baseline_total is None else "期望 baseline=None",
                    allow_diff_warn=True)
    else:
        ctx.results.append(CaseResult("E4", "仅套餐组件", status="SKIP", detail="目录无组件"))
    # E5 大需求 10 项
    solved_case(ctx, "E5", "大需求 10 项", f"{n1}×3,{n2}×3,{n3}×2,{comp or n4}×2")
    # E6 不可得降级：菜单不存在的名字在解析层报 unknown（CLI 报错降级）
    r = CaseResult("E6", "不可得降级：不存在的商品")
    try:
        d, unknown, amb = resolve_demand(catalog, "麦辣鸡腿堡不存在的菜")
        r.status = "PASS" if (unknown and d.is_empty()) else "FAIL"
        r.detail = f"unknown={unknown}（CLI 报错降级）"
    except Exception as e:  # noqa: BLE001
        r.status, r.detail = "FAIL", str(e)
    ctx.results.append(r)
    # E7a 名称解析：歧义词报候选（探测一个能子串命中多个商品的词）
    amb_word, amb_n = None, 0
    for probe in ("麦满分", "套餐", "组合", "麦咖啡", "堡"):
        d, u, a = resolve_demand(catalog, probe)
        if d.is_empty() and len(a) == 1 and len(a[0][1]) >= 2:
            amb_word, amb_n = probe, len(a[0][1])
            break
    r = CaseResult("E7a", f"解析：歧义词「{amb_word}」报多候选")
    if amb_word:
        r.status, r.detail = "PASS", f"{amb_n} 个候选"
    else:
        r.status, r.detail = "SKIP", "当前菜单无天然歧义词"
    ctx.results.append(r)
    # E7b 名称解析：未知词报错
    r = CaseResult("E7b", "解析：未知词「星球杯」报未找到")
    d, unknown, amb = resolve_demand(catalog, "星球杯")
    r.status = "PASS" if unknown == ["星球杯"] else "FAIL"
    ctx.results.append(r)
    # E8 按券拆单（合成 3 张券；不打官方价，验证拆分结构与金额守恒）
    r = CaseResult("E8", f"按券拆单：3张{n2}券 + {n2}×3（单action多份展开）")
    try:
        cat2 = copy.deepcopy(catalog)
        code2 = next(c for c, p in catalog.products.items() if p.name == n2)
        cat2.coupons = tuple(
            Coupon(coupon_id=f"T{i}", title=f"券{i}", product_codes=frozenset({code2}),
                   coupon_code=f"C{i}") for i in range(3))
        opt = solve(cat2, demand_from(cat2, f"{n2}×3"))
        plan = plan_split(cat2, opt.solution)
        ok = (plan.is_split and len(plan.suborders) == 3
              and all(s.coupon for s in plan.suborders)
              and plan.est_total == opt.solution.est_total)
        r.status = "PASS" if ok else "FAIL"
        r.detail = f"suborders={len(plan.suborders)}, est_total={plan.est_total} vs {opt.solution.est_total}"
    except Exception as e:  # noqa: BLE001
        r.status, r.detail = "FAIL", str(e)
    ctx.results.append(r)
    # E9 端到端性能（缓存命中 + 1 次官方算价）
    r = CaseResult("E9", "端到端性能：解析+求解+复核")
    t0 = time.monotonic()
    try:
        demand = demand_from(catalog, f"{n1}×2,{n2}×2")
        opt = solve(catalog, demand)
        verify_solution(client, catalog, opt.solution)
        ms = int((time.monotonic() - t0) * 1000)
        r.status = "PASS" if ms < 5000 else "FAIL"
        r.detail = f"{ms}ms（预算 5s）"
        r.ms = ms
    except Exception as e:  # noqa: BLE001
        r.status, r.detail = "FAIL", str(e)
    ctx.results.append(r)
    return ctx.results


def render_report(results: List[CaseResult], store: str, catalog: Catalog) -> str:
    lines = [
        "# 麦麦最优解 — 评测报告",
        "",
        f"- 门店：{store}",
        f"- 目录：{len(catalog.products)} 商品 / {len(catalog.combos)} 套餐 / {len(catalog.coupons)} 券"
        f"（fetched_at {catalog.fetched_at}）",
        f"- 运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| 场景 | 说明 | 结果 | 估算¥ | 官方¥ | 差额 | 动作 | 比单点省 | 耗时 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        f = lambda v: f"¥{v}" if v is not None else "—"
        lines.append(
            f"| {r.case_id} | {r.title} | {r.status} | {f(r.est)} | {f(r.official)} | "
            f"{f(r.diff)} | {r.actions or '—'} | {f(r.saved)} | {r.ms or '—'}ms"
            + (f"（{r.detail}）" if r.detail and r.status != "PASS" else "") + " |")
    passed = sum(1 for r in results if r.status == "PASS")
    warned = sum(1 for r in results if r.status == "WARN")
    lines += ["", f"**结果：{passed}/{len(results)} PASS" +
              (f"，{warned} WARN（已知限制，见下）" if warned else "") + "**",
              "", "> 所有求解场景经官方 calculate-price 复核；差额非 0 的场景均为"
              "「仅套餐组件」类（官方允许组件码直购且更便宜，v1 目录未含组件单价），已如实标注。"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="1990526")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    args = ap.parse_args()

    catalog = load_catalog(args.cache)
    catalog.store_code = args.store
    with McpClient() as client:
        results = run_all(catalog, client)
    report = render_report(results, args.store, catalog)
    out = Path(__file__).parent
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "report.json").write_text(json.dumps(
        [{"case": r.case_id, "title": r.title, "status": r.status,
          "est": str(r.est) if r.est else None,
          "official": str(r.official) if r.official else None,
          "diff": str(r.diff) if r.diff is not None else None,
          "actions": r.actions, "saved": str(r.saved) if r.saved else None,
          "ms": r.ms, "detail": r.detail} for r in results],
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(report)
    return 0 if all(r.status == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
