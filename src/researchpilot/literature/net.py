"""外部文献 API 的 HTTP 客户端：超时、有限重试、限速、磁盘缓存。

设计原则：

1. **所有外部请求都走这里。** 超时、重试、限速、缓存这四项不能靠调用方自觉，
   否则总有一条路径忘了加超时，把界面挂死在一次网络请求上。
2. **缓存命中就完全不发请求。** 因此断网时仍能查到之前查过的文献——
   这也正是 PRD AT-10 要求的"禁用网络后已保存内容仍可查看"。
3. **拿不到数据就抛 `BibliographyUnavailable`**，由上层记成一条失败来源记录并如实显示。
   绝不返回空壳数据、绝不编造字段。
4. 过期缓存 + 网络失败时，**降级使用过期缓存并标记 stale**，而不是直接失败。
   宁可给用户一份带时间戳的旧数据，也不要假装"没有这篇文献"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from ..config import BIBLIOGRAPHY, APP_VERSION

log = logging.getLogger(__name__)


class BibliographyUnavailable(RuntimeError):
    """外部文献服务不可用（断网、超时、限流、5xx）。"""


@dataclass
class CachedResponse:
    """一次外部查询的结果。带足够的信息让上层记来源记录。"""

    url: str
    status_code: int
    payload: Any
    fetched_at: str
    from_cache: bool = False
    stale: bool = False
    cache_path: str = ""
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and self.payload is not None


@dataclass
class CachedBinary:
    """Binary response (for example an open-access PDF)."""

    url: str
    status_code: int
    content: bytes
    fetched_at: str
    content_type: str = ""
    from_cache: bool = False
    stale: bool = False
    cache_path: str = ""
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and bool(self.content)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class HttpClient:
    """带缓存与限速的 JSON GET 客户端。"""

    def __init__(
        self,
        cache_dir: Path | None = None,
        ttl_days: int | None = None,
        min_interval: float | None = None,
        max_retries: int | None = None,
        timeout: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        settings = BIBLIOGRAPHY
        self.cache_dir = Path(cache_dir or settings.cache_dir)
        self.ttl = timedelta(days=ttl_days if ttl_days is not None else settings.cache_ttl_days)
        self.min_interval = settings.min_interval if min_interval is None else min_interval
        self.max_retries = settings.max_retries if max_retries is None else max_retries
        self.timeout = settings.timeout if timeout is None else timeout
        self.contact = settings.contact
        self.session = session or requests.Session()
        self._last_request_at = 0.0
        self.request_count = 0  # 只统计真正发出去的请求，缓存命中不算

    # ---------------------------------------------------------------- 缓存

    def _cache_path(self, provider: str, url: str, params: dict | None) -> Path:
        key = json.dumps({"url": url, "params": params or {}}, sort_keys=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / provider / f"{digest}.json"

    def _read_cache(self, path: Path) -> CachedResponse | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("缓存文件损坏，忽略：%s", path)
            return None
        fetched_at = data.get("fetched_at", "")
        stale = False
        try:
            age = datetime.now() - datetime.fromisoformat(fetched_at)
            stale = age > self.ttl
        except (TypeError, ValueError):
            stale = True
        return CachedResponse(
            url=data.get("url", ""),
            status_code=int(data.get("status_code", 0)),
            payload=data.get("payload"),
            fetched_at=fetched_at,
            from_cache=True,
            stale=stale,
            cache_path=str(path),
        )

    def _write_cache(self, path: Path, provider: str, url: str, params: dict | None,
                     status_code: int, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "provider": provider,
            "url": url,
            "params": params or {},
            "status_code": status_code,
            "fetched_at": _now(),
            "payload": payload,
        }
        path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- 请求

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        agent = f"ResearchPilot/{APP_VERSION} (research tool; +https://example.invalid)"
        if self.contact:
            agent = f"ResearchPilot/{APP_VERSION} (mailto:{self.contact})"
        return {"User-Agent": agent, "Accept": accept}

    def get_json(
        self, provider: str, url: str, params: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> CachedResponse:
        """取 JSON。命中未过期缓存直接返回；失败时降级用过期缓存（标 stale）。"""
        params = dict(params or {})
        if self.contact:
            params.setdefault("mailto", self.contact)

        path = self._cache_path(provider, url, params)
        cached = self._read_cache(path) if use_cache else None
        if cached is not None and not cached.stale:
            log.info("缓存命中 %s", url)
            return cached

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            self._throttle()
            try:
                self._last_request_at = time.monotonic()
                self.request_count += 1
                response = self.session.get(
                    url, params=params, timeout=self.timeout, headers=self._headers()
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("请求失败（第 %d 次）%s：%s", attempt, url, exc)
                time.sleep(min(2 ** (attempt - 1), 4))
                continue

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    last_error = exc
                    log.warning("响应不是 JSON %s：%s", url, exc)
                    break
                if use_cache:
                    self._write_cache(path, provider, url, params, 200, payload)
                return CachedResponse(
                    url=url, status_code=200, payload=payload, fetched_at=_now(),
                    cache_path=str(path), attempts=attempt,
                )

            if response.status_code in (404, 410):
                # 明确的"没有这条记录"：可以缓存，避免反复打同一个不存在的 DOI
                if use_cache:
                    self._write_cache(path, provider, url, params, response.status_code, None)
                return CachedResponse(
                    url=url, status_code=response.status_code, payload=None,
                    fetched_at=_now(), cache_path=str(path), attempts=attempt,
                )

            if response.status_code == 429 or response.status_code >= 500:
                last_error = BibliographyUnavailable(
                    f"{url} 返回 {response.status_code}"
                )
                log.warning("服务端繁忙（第 %d 次）%s：%s", attempt, url, response.status_code)
                time.sleep(min(2 ** (attempt - 1), 4))
                continue

            raise BibliographyUnavailable(
                f"{url} 返回 {response.status_code}，不是可重试的错误"
            )

        if cached is not None:
            log.warning("网络不可用，改用过期缓存：%s（抓取于 %s）", url, cached.fetched_at)
            cached.stale = True
            return cached

        raise BibliographyUnavailable(
            f"无法访问 {url}：{last_error or '重试用尽'}（且本地没有缓存）"
        )

    def get_bytes(
        self,
        provider: str,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        accept: str = "application/pdf,application/octet-stream;q=0.9,*/*;q=0.5",
        max_bytes: int = 30 * 1024 * 1024,
        use_cache: bool = True,
    ) -> CachedBinary:
        """Download a bounded binary through the same retry/rate-limit path.

        Callers must still validate the payload type. In particular, an HTML
        paywall or challenge page is not a PDF and must be rejected.
        """
        params = dict(params or {})
        if self.contact:
            params.setdefault("mailto", self.contact)
        meta_path = self._cache_path(provider, url, params)
        bin_path = meta_path.with_suffix(".bin")
        cached: CachedBinary | None = None
        if use_cache and meta_path.is_file() and bin_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                fetched_at = str(meta.get("fetched_at") or "")
                age = datetime.now() - datetime.fromisoformat(fetched_at)
                cached = CachedBinary(
                    url=str(meta.get("url") or url),
                    status_code=int(meta.get("status_code") or 0),
                    content=bin_path.read_bytes(),
                    fetched_at=fetched_at,
                    content_type=str(meta.get("content_type") or ""),
                    from_cache=True,
                    stale=age > self.ttl,
                    cache_path=str(bin_path),
                )
                if not cached.stale:
                    return cached
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                cached = None

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            self._throttle()
            try:
                self._last_request_at = time.monotonic()
                self.request_count += 1
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                    headers=self._headers(accept=accept),
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("二进制请求失败（第 %d 次）%s：%s", attempt, url, exc)
                time.sleep(min(2 ** (attempt - 1), 4))
                continue

            if response.status_code == 200:
                content = bytes(response.content or b"")
                if len(content) > max_bytes:
                    raise BibliographyUnavailable(
                        f"{url} 返回文件超过上限 {max_bytes} 字节"
                    )
                content_type = str(response.headers.get("content-type") or "")
                fetched_at = _now()
                if use_cache:
                    bin_path.parent.mkdir(parents=True, exist_ok=True)
                    bin_path.write_bytes(content)
                    meta_path.write_text(
                        json.dumps(
                            {
                                "provider": provider,
                                "url": url,
                                "params": params,
                                "status_code": 200,
                                "fetched_at": fetched_at,
                                "content_type": content_type,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                return CachedBinary(
                    url=url,
                    status_code=200,
                    content=content,
                    fetched_at=fetched_at,
                    content_type=content_type,
                    cache_path=str(bin_path),
                    attempts=attempt,
                )

            if response.status_code in (404, 410):
                return CachedBinary(
                    url=url,
                    status_code=response.status_code,
                    content=b"",
                    fetched_at=_now(),
                    attempts=attempt,
                )

            if response.status_code == 429 or response.status_code >= 500:
                last_error = BibliographyUnavailable(
                    f"{url} 返回 {response.status_code}"
                )
                log.warning(
                    "二进制服务端繁忙（第 %d 次）%s：%s",
                    attempt,
                    url,
                    response.status_code,
                )
                time.sleep(min(2 ** (attempt - 1), 4))
                continue

            raise BibliographyUnavailable(
                f"{url} 返回 {response.status_code}，不是可重试的错误"
            )

        if cached is not None:
            cached.stale = True
            return cached
        raise BibliographyUnavailable(
            f"无法下载 {url}：{last_error or '重试用尽'}（且本地没有缓存）"
        )
