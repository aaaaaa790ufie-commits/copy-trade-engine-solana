import asyncio, json, sys

async def test_ws(url, name):
    try:
        import websockets
    except ImportError:
        import subprocess
        subprocess.run(["pip", "install", "websockets", "-q"], capture_output=True)
        import websockets
    try:
        async with websockets.connect(url) as ws:
            payload = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "logsSubscribe",
                "params": [{"mentions": ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"]}]
            })
            await ws.send(payload)
            resp = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(resp)
            print(f"{name}: OK -- result={data.get('result')}")
            await ws.close()
    except Exception as e:
        print(f"{name}: FAIL -- {type(e).__name__}: {e}")

async def main():
    key = "33a9f314-bc9f-452d-bd59-ced96126d602"
    await test_ws(f"wss://mainnet.helius-rpc.com/?api-key={key}", "mainnet.helius-rpc.com")
    await test_ws(f"wss://rpc.helius.xyz/?api-key={key}", "rpc.helius.xyz")
    await test_ws(f"wss://atlas-mainnet.helius-rpc.com/?api-key={key}", "atlas-mainnet")

asyncio.run(main())
