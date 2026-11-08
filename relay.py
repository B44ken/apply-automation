#!/usr/bin/env python3
"""Local unauthenticated CONNECT relay -> upstream egress proxy with auth.

Chromium/Playwright in this VM cannot traverse the egress proxy directly
(ERR_TUNNEL_CONNECTION_FAILED), while curl/requests can. This relay listens
on 127.0.0.1:18080, accepts plain CONNECTs, and re-issues them upstream
with the Proxy-Authorization taken from the https_proxy env URL.
"""
import asyncio
import base64
import os
import sys
from urllib.parse import urlparse

UP = urlparse(os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or "")
UP_HOST = UP.hostname or "hatch-egress-proxy"
UP_PORT = UP.port or 3128
AUTH = None
if UP.username:
    AUTH = base64.b64encode(
        f"{UP.username}:{UP.password or ''}".encode()).decode()


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    try:
        writer.close()
    except Exception:
        pass


async def handle(client_r, client_w):
    try:
        head = await client_r.readuntil(b"\r\n\r\n")
    except Exception:
        client_w.close()
        return
    first = head.split(b"\r\n")[0].decode("latin1")
    parts = first.split(" ")
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        client_w.write(b"HTTP/1.1 405 Only CONNECT supported\r\n\r\n")
        await client_w.drain()
        client_w.close()
        return
    target = parts[1]
    try:
        up_r, up_w = await asyncio.open_connection(UP_HOST, UP_PORT)
    except Exception as e:
        print(f"[relay] upstream connect failed: {e}", flush=True)
        client_w.close()
        return
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}",
             "Proxy-Connection: Keep-Alive"]
    if AUTH:
        lines.append(f"Proxy-Authorization: Basic {AUTH}")
    up_w.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await up_w.drain()
    try:
        resp = await up_r.readuntil(b"\r\n\r\n")
    except Exception:
        client_w.close(); up_w.close()
        return
    status = resp.split(b"\r\n")[0].decode("latin1")
    if " 200 " not in status:
        print(f"[relay] upstream refused {target}: {status}", flush=True)
        client_w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await client_w.drain()
        client_w.close(); up_w.close()
        return
    client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await client_w.drain()
    await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 18080)
    print("[relay] listening on 127.0.0.1:18080 -> "
          f"{UP_HOST}:{UP_PORT} (auth={'yes' if AUTH else 'no'})", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
