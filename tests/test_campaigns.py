# -*- coding: utf-8 -*-
"""campaigns 解析单测：fixtures 取自 2026-10-10 真实探针输出。"""
import unittest

from mcd_optimal_bite.campaigns import (
    active_campaigns,
    claimable_coupons,
    parse_available_coupons,
    parse_campaign_calendar,
    summarize_coupons,
)

REAL_AVAILABLE_TEXT = """### 麦麦省优惠券列表：
- 优惠券标题：巧克力味厚松饼猪柳蛋套餐 \\
  状态：已领取 \\
  优惠券图片：\\
    <img src="https://img.mcd.cn/cms/images/a887.png" alt="巧克力味厚松饼猪柳蛋套餐" height="auto" width="300">
- 优惠券标题：9.9元中杯冰美式 \\
  状态：可领取 \\
  优惠券图片：\\
    <img src="https://img.mcd.cn/cms/images/b887.png" alt="9.9元中杯冰美式" height="auto" width="300">
"""

REAL_CALENDAR_TEXT = """### 当前时间：2026-10-10 11:43:30

### 活动列表：


#### 2026年10月7日 往期回顾

-   **活动标题**：超值𝟗.𝟗元早餐两件套陪你开工啦😋
    **活动内容介绍**：早八的快乐，一堡一咖已就位🍔☕

#### 2026年10月8日 往期回顾

-   **活动标题**：麦当劳 X PEACEMINUSONE
    **活动内容介绍**：GD同款联名复古棒球帽

#### 2026年10月10日 进行中

-   **活动标题**：过家家派对上线，快来一起解锁梦想职业
    **活动内容介绍**：萌娃cosplay梦想职业
-   **活动标题**：麦麦秒周四会员日
    **活动内容介绍**：会员专属优惠
"""


class TestParseAvailableCoupons(unittest.TestCase):
    def test_real_sample(self):
        items = parse_available_coupons(REAL_AVAILABLE_TEXT)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "巧克力味厚松饼猪柳蛋套餐")
        self.assertFalse(items[0].claimable)          # 已领取
        self.assertEqual(items[1].title, "9.9元中杯冰美式")
        self.assertTrue(items[1].claimable)           # 可领取

    def test_claimable_filter(self):
        claim = claimable_coupons(REAL_AVAILABLE_TEXT)
        self.assertEqual([c.title for c in claim], ["9.9元中杯冰美式"])

    def test_summary(self):
        s = summarize_coupons(REAL_AVAILABLE_TEXT)
        self.assertEqual(s, {"total": 2, "claimable": 1, "claimed": 1})

    def test_empty(self):
        self.assertEqual(parse_available_coupons(""), [])
        self.assertEqual(parse_available_coupons("没有券位信息"), [])


class TestParseCampaignCalendar(unittest.TestCase):
    def test_real_sample(self):
        acts = parse_campaign_calendar(REAL_CALENDAR_TEXT)
        self.assertEqual(len(acts), 4)
        self.assertEqual(acts[0].date, "2026年10月7日")
        self.assertEqual(acts[0].tag, "往期回顾")
        self.assertTrue(acts[0].expired)
        self.assertIn("PEACEMINUSONE", acts[1].title)

    def test_active_filter_excludes_expired(self):
        acts = active_campaigns(REAL_CALENDAR_TEXT)
        self.assertEqual([a.title for a in acts],
                         ["过家家派对上线，快来一起解锁梦想职业", "麦麦秒周四会员日"])
        self.assertFalse(any(a.expired for a in acts))

    def test_limit(self):
        acts = active_campaigns(REAL_CALENDAR_TEXT, limit=1)
        self.assertEqual(len(acts), 1)

    def test_empty(self):
        self.assertEqual(parse_campaign_calendar(""), [])
        self.assertEqual(active_campaigns("无活动信息"), [])


if __name__ == "__main__":
    unittest.main()
