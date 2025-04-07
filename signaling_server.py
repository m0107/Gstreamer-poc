#!/usr/bin/env python3
"""
A simple WebSocket signaling server for WebRTC.

- Allows multiple clients to connect.
- Broadcasts received messages to all OTHER connected clients.
- Provides verbose logging on connect, disconnect, and message forwarding.

Usage:
    python3 signaling_server.py

Then open ws://localhost:8765 in your clients.
"""

import asyncio
import websockets
import json

clients = set()

async def handler(websocket, path):
    """
    Each new client connects here. We'll log connections, handle messages,
    and broadcast them to all other connected clients.
    """
    print(f"[INFO] New client connected: {websocket.remote_address}")
    clients.add(websocket)

    try:
        async for message in websocket:
            print(f"[DEBUG] Received message from {websocket.remote_address}: {message}")
            # Broadcast to all *other* clients
            for client in clients:
                if client != websocket:
                    print(f"[DEBUG] Forwarding message to {client.remote_address}")
                    await client.send(message)
    except websockets.ConnectionClosed as exc:
        print(f"[WARN] Connection closed for {websocket.remote_address}: {exc}")
    finally:
        clients.remove(websocket)
        print(f"[INFO] Client disconnected: {websocket.remote_address}")

async def main():
    host = "0.0.0.0"
    port = 8765
    print(f"[INFO] Starting signaling server on {host}:{port}...")

    async with websockets.serve(handler, host, port):
        print(f"[INFO] Signaling server running at ws://{host}:{port}")
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[INFO] Server stopped via KeyboardInterrupt.")
