# app/services/cache.py
from __future__ import annotations
import asyncio
import functools
import time
from typing import Any, Callable, Dict, Tuple

# In-process TTL caches by namespace
# value = (expires_at_epoch, stored_at_epoch, data)
_CACHES: Dict[str, Dict[Tuple[Any, ...], Tuple[float, int, Any]]] = {}

def _cache_for(namespace: str) -> Dict[Tuple[Any, ...], Tuple[float, int, Any]]:
    if namespace not in _CACHES:
        _CACHES[namespace] = {}
    return _CACHES[namespace]

def _now() -> float:
    return time.time()

def cache_route(
    *,
    namespace: str,
    ttl_seconds: int,
    key_builder: Callable[..., Tuple[Any, ...]],
    cache_control: str | None = None,  # defaults to private,max-age=ttl
):
    """
    Decorator for FastAPI routes (sync or async).
    - Caches the returned data by a computed key.
    - Sets X-Cache: HIT|MISS, X-Cache-Stored-At, and Cache-Control on the Response if present in kwargs.
    """
    cache = _cache_for(namespace)

    def decorator(fn: Callable):
        is_async = asyncio.iscoroutinefunction(fn)

        async def _call(*args, **kwargs):
            return await fn(*args, **kwargs) if is_async else fn(*args, **kwargs)

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            response = kwargs.get("response")  # FastAPI Response if included in signature
            key = key_builder(*args, **kwargs)
            now = _now()

            hit = False
            entry = cache.get(key)
            if entry:
                exp_at, stored_at, data = entry
                if exp_at > now:
                    hit = True
                    if response is not None:
                        response.headers["X-Cache"] = "HIT"
                        response.headers["X-Cache-Stored-At"] = str(stored_at)
                        response.headers["Cache-Control"] = cache_control or f"private, max-age={ttl_seconds}"
                    return data
                else:
                    cache.pop(key, None)

            # MISS → call downstream
            data = await _call(*args, **kwargs)
            stored_at = int(now)
            cache[key] = (now + ttl_seconds, stored_at, data)
            if response is not None:
                response.headers["X-Cache"] = "MISS"
                response.headers["X-Cache-Stored-At"] = str(stored_at)
                response.headers["Cache-Control"] = cache_control or f"private, max-age={ttl_seconds}"
            return data

        return wrapper
    return decorator

# ------------- common key helpers -------------

def key_tuple(*parts: Any) -> Tuple[Any, ...]:
    return tuple(parts)

def key_user_path_query(*, user_id: str, path: str, query_items: Tuple[Tuple[str, str], ...]) -> Tuple[Any, ...]:
    # stable by user + path + normalized query
    return (user_id, path, query_items)
