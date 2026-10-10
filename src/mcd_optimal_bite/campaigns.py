# -*- coding: utf-8 -*-
"""mcd_optimal_bite.campaigns — 麦麦省可领券 & 营销活动日历解析

实测事实（2026-10-10）：
- available-coupons 返回 markdown 列表（无 JSON）：
    ### 麦麦省优惠券列表：
    - 优惠券标题：X \
      状态：已领取/可领取 \
      优惠券图片：<img ...>
- campaign-calendar 返回 markdown 分段：
    #### 2026年10月7日 往期回顾
    -   **活动标题**：X
        **活动内容介绍**：Y
        **活动图片介绍**：<img ...>

解析为结构化数据供 CLI 展示。「往期回顾」段落为已结束活动，默认过滤。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

_COUPON_ITEM_RE = re.compile(
    r"- 优惠券标题：(.+?)\\\s*\n\s*状态：(.+?)\s*(?:\\\n|$)", re.S)

_SECTION_RE = re.compile(r"####\s+(\d{4}年\d+月\d+日)\s*(\S*)")
_TITLE_RE = re.compile(r"\*\*活动标题\*\*：(.+)")


@dataclass
class ClaimableCoupon:
    """一张麦麦省券（可领取 / 已领取）"""
    title: str
    status: str

    @property
    def claimable(self) -> bool:
        return "已领取" not in self.status


@dataclass
class Campaign:
    """一条营销活动"""
    date: str            # 2026年10月7日
    tag: str             # 往期回顾 / 进行中 / ...
    title: str

    @property
    def expired(self) -> bool:
        return "往期" in self.tag


def parse_available_coupons(text: str) -> List[ClaimableCoupon]:
    """available-coupons 的 markdown → 券列表（保序）。"""
    if not text:
        return []
    out: List[ClaimableCoupon] = []
    for m in _COUPON_ITEM_RE.finditer(text):
        title = m.group(1).strip().strip("\\").strip()
        status = m.group(2).strip().strip("\\").strip()
        if title:
            out.append(ClaimableCoupon(title=title, status=status))
    return out


def parse_campaign_calendar(text: str) -> List[Campaign]:
    """campaign-calendar 的 markdown → 活动列表（按日期段保序）。"""
    if not text:
        return []
    out: List[Campaign] = []
    # 以日期段标题切分
    parts = _SECTION_RE.split(text)
    # parts: [前导, 日期, tag, 正文, 日期, tag, 正文, ...]
    for i in range(1, len(parts) - 1, 3):
        date, tag, body = parts[i], parts[i + 1], parts[i + 2]
        for t in _TITLE_RE.finditer(body):
            title = t.group(1).strip()
            if title:
                out.append(Campaign(date=date, tag=tag.strip(), title=title))
    return out


def active_campaigns(text: str, limit: int = 3) -> List[Campaign]:
    """进行中（非往期）的活动，最多 limit 条。"""
    return [c for c in parse_campaign_calendar(text) if not c.expired][:limit]


def claimable_coupons(text: str) -> List[ClaimableCoupon]:
    """待领取的券。"""
    return [c for c in parse_available_coupons(text) if c.claimable]


def summarize_coupons(text: str) -> Dict[str, int]:
    """{总数, 待领, 已领} 概览。"""
    items = parse_available_coupons(text)
    return {
        "total": len(items),
        "claimable": sum(1 for c in items if c.claimable),
        "claimed": sum(1 for c in items if not c.claimable),
    }
