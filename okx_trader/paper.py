# -*- coding: utf-8 -*-
"""PaperClient：纸面模式客户端。

行情/因子走真实公开接口；账户状态用本地虚拟数据（不调任何需要鉴权的接口）。
凭证显式传 "-1"（python-okx 约定的不签名标记），绝不改写共享的 cfg 对象。
"""
from .client import OKXClient


class PaperClient(OKXClient):

    def __init__(self, cfg, logger=None):
        super().__init__(cfg, logger=logger,
                         api_key="-1", api_secret_key="-1", passphrase="-1",
                         flag="1")
        self.paper_equity = float(getattr(cfg, "PAPER_EQUITY", 10000.0))

    def get_equity(self):
        return {"total_eq": self.paper_equity, "usdt_eq": self.paper_equity,
                "usdt_avail": self.paper_equity, "raw": {"paper": True}}

    def get_positions(self, inst_id=""):
        return []

    def get_pending_orders(self, inst_id=""):
        return []  # 纸面从不持有真实挂单（风控 R4 查重用）

    def set_leverage(self, inst_id, lever):
        self.log.info("（纸面）set_leverage %s %sx", inst_id, lever)

    def get_account_mode(self, refresh=False):
        return {"uid": "paper", "acctLv": "5", "posMode": "net_mode"}
