#!/usr/bin/env python3
"""
Health check for Helius HTTP reachability.
Exit code 0 = Helius reachable, 1 = not reachable.
Logs clear message for tunnel-down vs other failures.
"""

import json
import os
import sys
import urllib.request


def check_helius():
    """Return (ok: bool, message: str)."""
    key = os.getenv("HELIUS_API_KEY") or os.getenv("SOLANA_API_KEY") or ""
    if not key:
        return (False, "NO_KEY — set HELIUS_API_KEY (or SOLANA_API_KEY) in the environment")
    helius_url = f"https://mainnet.helius-rpc.com/?api-key={key}"

    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "getHealth"
    }).encode('utf-8')

    try:
        req = urllib.request.Request(
            helius_url,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8')
            data = json.loads(body)

        if data.get('result') == 'ok':
            return (True, "OK — Helius HTTP reachable, getHealth returned 'ok'")
        elif 'error' in data:
            err = data['error']
            code = err.get('code', '?')
            msg = err.get('message', '?')
            if code in (-32000, -32001, -32002, 401, 403):
                return (True, f"HELIUS_REACHABLE_BUT_API_ERROR — code={code}: {msg}. Tunnel works, key issue.")
            else:
                return (False, f"Helius responded but with error: code={code}: {msg}")
        else:
            return (True, f"Helius responded: {json.dumps(data)[:200]}")
    except urllib.error.URLError as e:
        if 'Tunnel' in str(e) or 'connect' in str(e) or 'refused' in str(e).lower():
            return (False, f"TUNNEL_OR_NETWORK_DOWN — {e}")
        return (False, f"NETWORK_ERROR — {e}")
    except json.JSONDecodeError as e:
        return (False, f"PARSE_ERROR — bad response: {e}")
    except Exception as e:
        return (False, f"UNKNOWN_ERROR — {e}")

def main():
    ok, msg = check_helius()
    if ok:
        print(f"[HEALTH] {msg}")
        sys.exit(0)
    else:
        print(f"[HEALTH] FAIL — {msg}")
        sys.exit(1)

if __name__ == '__main__':
    main()
