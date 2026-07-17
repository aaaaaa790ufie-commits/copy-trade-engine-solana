"""Find a real PumpSwap swap transaction by scanning for Instruction: Buy/Sell in logs."""
import json, urllib.request, base64, sys

HELIUS = "https://mainnet.helius-rpc.com/?api-key=33a9f314-bc9f-452d-bd59-ced96126d602"
PUMP_SWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

def rpc_call(method, params):
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(HELIUS, data=data, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())

# Get signatures
sigs_data = rpc_call("getSignaturesForAddress", [PUMP_SWAP, {"limit": 50}])
sigs = [s["signature"] for s in sigs_data.get("result", []) if s.get("err") is None]

print(f"Got {len(sigs)} successful signatures, scanning...")

for sig in sigs:
    try:
        tx_data = rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        tx = tx_data.get("result")
        if not tx:
            continue
        logs = tx["meta"].get("logMessages", [])
        for l in logs:
            if "Instruction: Buy" in l or "Instruction: Sell" in l:
                print(f"SWAP: {sig}")
                
                # Save transaction
                with open(r"C:\Users\Admin\sentinel\tests\tx_pumpswap_swap.json", "w") as f:
                    json.dump(tx_data, f)
                    
                # Print details
                msg = tx["transaction"]["message"]
                accts = msg["accountKeys"]
                print(f"\nAccounts ({len(accts)}):")
                for i, a in enumerate(accts):
                    pk = a.get("pubkey", str(a)) if isinstance(a, dict) else str(a)
                    print(f"  [{i}] {pk}")
                
                # Print matching log
                for l in logs:
                    if "Instruction" in l:
                        print(f"  LOG: {l}")
                
                sys.exit(0)
    except Exception as e:
        print(f"  Error for {sig}: {e}")
        continue

print("No swap tx found")
