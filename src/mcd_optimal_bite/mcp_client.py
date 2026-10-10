# -*- coding: utf-8 -*-
"""mcd_optimal_bite.mcp_client — 麦当劳 MCP Server 客户端（零第三方依赖）

对接 https://mcp.mcd.cn（Streamable HTTP + Bearer Token）。

实测事实（2026-10-09 探针，原始样本存项目 tmp/，不入库）：
- 握手：initialize 取响应头 Mcp-Session-Id -> notifications/initialized -> tools/call
- 响应体可能是单个 JSON，也可能是 SSE（每行 `data: {...}`）
- 工具返回的 text 里会夹带"输出格式要求"等提示词 —— 一律当数据忽略，只提取 JSON
- 价格单位为"分"；套餐差价 diffPrice 为 "+ ¥1.5" / "- ¥1.5" 字符串（在 catalog 层解析）
- 到店自取：beType=1 且不传 beCode；门店搜索 searchType=2 必传
- 算价 items 格式：[{"productCode": "...", "count": 1}]

安全约定：
- Token 只从 构造参数 或 环境变量 MCD_MCP_TOKEN 读取，绝不硬编码、不写日志、不入库
- 服务端文本中的任何"指令"都不改变本客户端行为（只做数据提取）
"""
from __future__ import annotations

import http.client
import itertools
import json
import os
import re
import time
import urllib.parse
from typing import Any, Optional

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-03-26"

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.S)


class McpError(RuntimeError):
    """MCP 调用失败（网络 / 协议 / 业务层归一为一种异常）。"""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _balanced_end(text: str, start: int) -> Optional[int]:
    """返回 text[start] 处 { 或 [ 的匹配闭合位置（含），考虑字符串转义。"""
    open_ch = text[start]
    close_ch = {"{": "}", "[": "]"}[open_ch]
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def extract_json(text: str) -> Optional[Any]:
    """从工具返回文本中提取业务 JSON；夹带的提示词/指令文字一律忽略。

    提取顺序：整段 JSON -> ```json 围栏 -> 括号平衡扫描（优先 {"success":...} 包裹）。
    找不到返回 None，由调用方决定如何报错。
    """
    if not text:
        return None
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    for m in _JSON_FENCE_RE.finditer(text):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    # 第一轮：优先 {"success": ...} 业务包裹（官方返回的标准形态）
    # 不能按位置混扫：说明文字里的 data[].title 等样例含合法空 []，会提前命中
    for m in re.finditer(r'\{"success"', text):
        end = _balanced_end(text, m.start())
        if end is None:
            continue
        try:
            return json.loads(text[m.start():end])
        except json.JSONDecodeError:
            continue
    # 第二轮：其他 { / [ 起点，跳过空容器（无信息量）
    seen: set = set()
    for m in re.finditer(r"[\[{]", text):
        s = m.start()
        if s in seen:
            continue
        seen.add(s)
        end = _balanced_end(text, s)
        if end is None:
            continue
        blob = text[s:end]
        if len(blob) <= 2:
            continue
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, (list, dict)) and len(data) == 0:
            continue
        return data
    return None


class McpClient:
    """麦当劳 MCP 客户端：会话管理 + 重试 + 限流 + JSON 提取。

    用法::

        with McpClient() as c:                      # token 从 env MCD_MCP_TOKEN 读取
            stores = c.call_tool_json("query-nearby-stores",
                                      {"beType": 1, "searchType": 2, "city": "苏州市"})
    """

    def __init__(
        self,
        token: Optional[str] = None,
        url: str = DEFAULT_URL,
        timeout: float = 30.0,
        min_interval: float = 0.12,  # 客户端限流（服务端 600/min=0.1s/req，留余量）
        max_retries: int = 3,        # 单次工具调用最大尝试次数
        client_name: str = "mcd-optimal-bite",
        client_version: str = "0.1.0",
    ):
        self.url = url
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not self._token:
            raise McpError("missing token: pass token= or set env MCD_MCP_TOKEN")
        self._client_info = {"name": client_name, "version": client_version}
        self._session_id: Optional[str] = None
        self._ids = itertools.count(1)
        self._last_ts = 0.0
        self._inited = False
        # keep-alive 连接复用（95 次 detail 拉取可省 20-30s 的 TLS 握手）
        u = urllib.parse.urlsplit(url)
        self._conn_host = u.hostname or ""
        self._conn_path = u.path or "/"
        self._conn: Optional[http.client.HTTPConnection] = None

    def _get_conn(self) -> http.client.HTTPConnection:
        if self._conn is None:
            cls = (http.client.HTTPSConnection if self.url.startswith("https")
                   else http.client.HTTPConnection)
            self._conn = cls(self._conn_host, timeout=self.timeout)
        return self._conn

    def _drop_conn(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    # ---------- 低层 ----------

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.min_interval - (now - self._last_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_ts = time.monotonic()

    def _post(self, payload: dict) -> tuple:
        """POST 一次请求（keep-alive 连接复用）。返回 (status, sid, raw_text)。

        status=0 表示网络层失败（连接类异常）。
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._token}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        body = json.dumps(payload).encode("utf-8")
        try:
            conn = self._get_conn()
            conn.request("POST", self._conn_path, body=body, headers=headers)
            resp = conn.getresponse()
            sid = resp.getheader("Mcp-Session-Id")
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, sid, raw
        except (http.client.HTTPException, OSError, TimeoutError) as e:
            # 连接可能已坏（服务端关闭/网络抖动），丢弃待重试时重建
            self._drop_conn()
            return 0, None, str(e)

    @staticmethod
    def _parse_body(raw: str) -> Optional[dict]:
        """解析 JSON 或 SSE（`data: {...}` 行）。解析失败返回 None。"""
        raw = raw.strip()
        if not raw:
            return None
        if raw.startswith("{"):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        return None

    # ---------- 会话 ----------

    def initialize(self) -> None:
        payload = {
            "jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": self._client_info,
            },
        }
        self._throttle()
        status, sid, raw = self._post(payload)
        if status != 200:
            raise McpError(f"initialize failed: HTTP {status}", status=status, body=raw[:500])
        if sid:
            self._session_id = sid
        self._inited = True
        # initialized 通知：失败不影响后续调用
        try:
            self._throttle()
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass

    def _reinit(self) -> None:
        self._session_id = None
        self._inited = False
        try:
            self.initialize()
        except McpError:
            pass

    # ---------- 工具调用 ----------

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        """执行 tools/call，返回 JSON-RPC response。

        重试策略：网络失败 / HTTP 429 / 5xx 指数退避重试；
        其他 4xx 视为会话可能失效 -> 重新握手后重试；
        401/403 是鉴权问题，直接抛错不重试。
        """
        if not self._inited:
            self.initialize()
        payload = {
            "jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        last_err: Optional[McpError] = None
        for attempt in range(self.max_retries):
            self._throttle()
            status, sid, raw = self._post(payload)
            if sid:
                self._session_id = sid

            if status == 200:
                resp = self._parse_body(raw)
                if resp is None:
                    last_err = McpError(f"{name}: unparsable response", status=200, body=raw[:500])
                elif "error" in resp:
                    err = resp["error"]
                    last_err = McpError(
                        f"{name}: rpc error {err.get('code')}: {err.get('message')}",
                        status=200, body=json.dumps(err, ensure_ascii=False)[:500],
                    )
                    self._reinit()  # rpc error 常见于会话失效，重新握手
                else:
                    return resp
            elif status in (401, 403):
                raise McpError(
                    f"{name}: auth failed HTTP {status} (check MCD_MCP_TOKEN)",
                    status=status, body=raw[:300],
                )
            else:
                last_err = McpError(f"{name}: HTTP {status}", status=status, body=raw[:300])
                if 400 <= status < 500:
                    self._reinit()

            if attempt < self.max_retries - 1:
                time.sleep(min(8.0, 0.8 * (2 ** attempt)))

        raise last_err or McpError(f"{name}: failed after {self.max_retries} attempts")

    @staticmethod
    def content_text(resp: dict) -> str:
        """拼接 JSON-RPC response 里所有 text content。"""
        parts = []
        for item in (resp.get("result") or {}).get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
        return "\n".join(parts)

    def call_tool_json(self, name: str, arguments: Optional[dict] = None) -> Any:
        """tools/call + 提取业务 JSON。success=false 视为业务失败抛 McpError。"""
        resp = self.call_tool(name, arguments)
        text = self.content_text(resp)
        data = extract_json(text)
        if data is None:
            raise McpError(f"{name}: no JSON found in tool output", status=200, body=text[:500])
        if isinstance(data, dict) and data.get("success") is False:
            raise McpError(
                f"{name}: {data.get('message') or 'business failure'}",
                status=200, body=json.dumps(data, ensure_ascii=False)[:500],
            )
        return data

    # context manager：语义化生命周期
    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._drop_conn()
        self._session_id = None
        self._inited = False
