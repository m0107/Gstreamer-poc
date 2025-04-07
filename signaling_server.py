#!/usr/bin/env python3
import asyncio
import websockets
import json

clients = set()

async def handler(websocket):
    clients.add(websocket)
    try:
        async for message in websocket:
            # Broadcast message to all other clients
            for client in clients:
                if client != websocket:
                    await client.send(message)
    except websockets.ConnectionClosed:
        pass
    finally:
        clients.remove(websocket)

async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        print("Signaling server running at ws://localhost:8765")
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())