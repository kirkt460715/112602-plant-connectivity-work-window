"""本地联调端（partner_feed）客户端：拉取到达事实、核实回执。

只访问回环资料端，不接真实设备。回执 503（延迟可查）按退避重试，
404 视为查无此回执并显式返回 None，由协调引擎决定如何标注。
"""

import json
from http.client import HTTPConnection
from time import sleep
from urllib.parse import urlencode


class FeedUnavailable(RuntimeError):
    """资料端不可达或返回非预期状态。"""


class ReceiptClient:
    def __init__(self, host="127.0.0.1", port=8765, timeout=2, retries=3, backoff=0.05):
        self.base = (host, port)
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def _get(self, path: str):
        connection = HTTPConnection(*self.base, timeout=self.timeout)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body.decode("utf-8")) if body else {}
        finally:
            connection.close()

    def fetch_register(self) -> dict:
        status, data = self._get("/register")
        if status != 200:
            raise FeedUnavailable(f"register 返回 {status}")
        return data

    def iter_intake(self, since=0, page_limit=50):
        """逐页拉取到达记录，产出每条事件；游标来自资料端。"""
        cursor = since
        while True:
            query = urlencode({"since": cursor, "limit": page_limit})
            status, data = self._get(f"/intake?{query}")
            if status != 200:
                raise FeedUnavailable(f"intake 返回 {status}")
            items = data.get("items", [])
            for item in items:
                yield item
            if not items:
                return
            next_cursor = data["cursor"]
            if next_cursor == cursor:
                return
            cursor = next_cursor

    def fetch_receipt(self, action: str, desk: str, ref: str):
        """回执查询。503 延迟可查则重试；404 返回 None。"""
        query = urlencode({"action": action, "desk": desk, "ref": ref})
        path = f"/receipt?{query}"
        for attempt in range(self.retries):
            status, data = self._get(path)
            if status == 200:
                return data.get("evidence", {})
            if status == 503:
                sleep(self.backoff * (attempt + 1))
                continue
            if status == 404:
                return None
            raise FeedUnavailable(f"receipt {ref} 返回 {status}")
        raise FeedUnavailable(f"receipt {ref} 多次 503 仍不可查")
