"""Bounded process-local throttling. Deploy shared edge limits across workers."""
from collections import OrderedDict
from threading import Lock
from time import monotonic
from math import ceil

from fastapi import HTTPException
from starlette.responses import JSONResponse


class Limiter:
    def __init__(self):
        self.entries = OrderedDict()
        self.lock = Lock()

    def clear(self):
        with self.lock:
            self.entries.clear()

    def hit(self, key, limit, window):
        with self.lock:
            now = monotonic()
            count, expiry = self.entries.get(key, (0, now + window))
            if expiry <= now:
                count, expiry = 0, now + window
            if count >= limit:
                raise HTTPException(429, 'Too many requests. Try again later.',
                                    headers={'Retry-After': str(max(1, ceil(expiry-now)))})
            self.entries[key] = (count + 1, expiry)
            self.entries.move_to_end(key)
            while len(self.entries) > 10000:
                self.entries.popitem(last=False)


limiter = Limiter()


def source(request):
    # Trust only the ASGI peer; proxy forwarding must be configured at the server.
    return request.client.host if request.client else 'unknown'


class RequestLimits:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH'):
            return await self.app(scope, receive, send)
        path = scope['path'].rstrip('/')
        cap = 4096 if path in ('/api/auth/login', '/api/auth/register', '/api/adventurers') else 1048576
        messages, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > cap:
                return await JSONResponse({'detail': 'Request body too large.'}, status_code=413)(scope, receive, send)
            messages.append(message)
            if not message.get('more_body', False):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        await self.app(scope, replay, send)
