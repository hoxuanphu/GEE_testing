"""Bound concurrent thumbnail work and keep response headers consistent."""
from anyio import CapacityLimiter, to_thread
from fastapi.responses import Response


_workers = CapacityLimiter(4)


async def load(function, *args):
    return await to_thread.run_sync(function, *args, limiter=_workers)


def response(content, media_type='image/png'):
    return Response(content, media_type=media_type, headers={
        'Cache-Control': 'private, max-age=3600',
        'X-Content-Type-Options': 'nosniff',
    })
