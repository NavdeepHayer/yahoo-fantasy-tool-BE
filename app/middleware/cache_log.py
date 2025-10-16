# app/middleware/cache_log.py
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

class CacheHeaderLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        hit = response.headers.get("X-Cache")
        if hit:
            path = request.url.path
            print(f"[CACHE] {path} {hit} cc={response.headers.get('Cache-Control')}")
        return response
