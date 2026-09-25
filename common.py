"""共享基础：业务错误与统一时间戳，供保管记录与引用规则共同使用。"""
from __future__ import annotations

from datetime import datetime, timezone


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
