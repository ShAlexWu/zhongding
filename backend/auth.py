# -*- coding: utf-8 -*-
"""轻量级登录鉴权：单账号（USER_NAME / PASSWORD 环境变量），无注册、不落库。

用进程内存里的一个 token -> 过期时间 映射标记「已登录」，配合 HttpOnly Cookie
下发给浏览器；没有持久化，后端一重启所有人都要重新登录——对一个演示系统来说
这个代价可以接受，换来的是不用额外引入数据库/用户表。
"""

import secrets
import time
from typing import Dict, Optional

import config

COOKIE_NAME = "zd_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 天免重复登录

_sessions: Dict[str, float] = {}  # token -> 过期时间戳（time.time() 基准）


def auth_enabled() -> bool:
    """.env 没配置账号密码时视为鉴权关闭——避免部署时忘配，把自己锁在门外。"""
    return bool(config.AUTH_USERNAME and config.AUTH_PASSWORD)


def verify_credentials(username: str, password: str) -> bool:
    """校验用户名密码；用时间安全比较，避免逐字符提前返回的时序侧信道。"""
    if not auth_enabled():
        return True
    ok_user = secrets.compare_digest(username or "", config.AUTH_USERNAME)
    ok_pass = secrets.compare_digest(password or "", config.AUTH_PASSWORD)
    return ok_user and ok_pass


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL_SECONDS
    return token


def is_valid(token: Optional[str]) -> bool:
    if not auth_enabled():
        return True
    if not token:
        return False
    expires = _sessions.get(token)
    if expires is None:
        return False
    if expires < time.time():
        _sessions.pop(token, None)
        return False
    return True


def revoke(token: Optional[str]) -> None:
    if token:
        _sessions.pop(token, None)
