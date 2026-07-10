"""保留給未來 TKS Line 會員 API 串接的介面。目前沒有任何真正的 Line API
細節可用，先定義好形狀（參考 Kart_app/smartkart_app 的 LINE_INTEGRATION_PLAN.md
描述的流程：Login -> callback -> exchange code -> fetch profile -> link
lineUserId），讓之後接上真正的實作時，只需要新增一個 LineMemberClient
子類別、換掉 app.py 裡建構的實例，不需要改動呼叫端。

users.line_user_id 欄位已經存在（見 models.py），目前恆為 None。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LineProfile:
    line_user_id: str
    display_name: str | None
    picture_url: str | None


class LineMemberClient(ABC):
    @abstractmethod
    async def exchange_code_for_profile(self, code: str) -> LineProfile: ...


class NotImplementedLineMemberClient(LineMemberClient):
    async def exchange_code_for_profile(self, code: str) -> LineProfile:
        raise NotImplementedError(
            "TKS Line 會員 API 尚未串接，此為預留擴充點（見模組說明）"
        )
