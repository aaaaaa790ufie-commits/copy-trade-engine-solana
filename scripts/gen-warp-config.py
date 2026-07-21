#!/usr/bin/env python3
"""
Generate a Helius-scoped WARP/AmneziaWG config.
1. Generates Curve25519 keypair (WireGuard-compatible)
2. Registers a new WARP account via Cloudflare API
3. Writes AmneziaWG config that routes only Helius (Cloudflare) IPs
"""

import os
import sys
import json
import base64
import urllib.request
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization

def gen_x25519_keypair():
    """Generate Curve25519 keypair and return (priv_b64, pub_b64)."""
    private_key = X25519PrivateKey.generate()
    public_key = private_key.public_key()

    # WireGuard uses raw 32-byte keys encoded in base64 (not PKCS8)
    priv_raw = private_key.private_bytes_raw()
    pub_raw = public_key.public_bytes_raw()

    priv_b64 = base64.b64encode(priv_raw).decode('ascii')
    pub_b64 = base64.b64encode(pub_raw).decode('ascii')
    return priv_b64, pub_b64

def register_warp(pub_b64):
    """Register a new WARP account via Cloudflare API."""
    import time
    tos = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    payload = json.dumps({
        "install_id": "",
        "tos": tos,
        "key": pub_b64,
        "fcm_token": "",
        "type": "ios",
        "locale": "en_US"
    }).encode('utf-8')

    req = urllib.request.Request(
        "https://api.cloudflareclient.com/v0i1909051800/reg",
        data=payload,
        headers={
            'User-Agent': 'okhttp/3.12.1',
            'Content-Type': 'application/json',
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode('utf-8'))

    if data.get('result', {}).get('id') is None:
        print("ERROR: Registration failed:", json.dumps(data, indent=2))
        sys.exit(1)

    return data

def enable_warp(reg_id, token):
    """Enable WARP on the registered account."""
    payload = json.dumps({"warp_enabled": True}).encode('utf-8')
    req = urllib.request.Request(
        f"https://api.cloudflareclient.com/v0i1909051800/reg/{reg_id}",
        data=payload,
        headers={
            'User-Agent': 'okhttp/3.12.1',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}',
        },
        method='PATCH'
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))

def cloudflare_ipv4_ranges():
    """Return Cloudflare IPv4 CIDR ranges (for AllowedIPs)."""
    return [
        "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
        "104.16.0.0/13", "104.24.0.0/14",
        "108.162.192.0/18",
        "131.0.72.0/22",
        "141.101.64.0/18",
        "162.158.0.0/15",
        "172.64.0.0/13",
        "173.245.48.0/20",
        "188.114.96.0/20",
        "190.93.240.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
    ]

def cloudflare_ipv6_ranges():
    return [
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c1::/29",
        "2c0f:f990::/32",
        "2c0f:f998::/32",
    ]

def build_config(priv_b64, client_ipv4, client_ipv6, peer_pub_b64, endpoint_host="162.159.192.1", endpoint_port="500"):
    """Build an AmneziaWG config that routes ONLY Cloudflare IPs (Helius)."""
    allowed_v4 = ", ".join(cloudflare_ipv4_ranges())
    allowed_v6 = ", ".join(cloudflare_ipv6_ranges())

    config = f"""[Interface]
PrivateKey = {priv_b64}
Address = {client_ipv4}, {client_ipv6}
DNS = 1.1.1.1, 2606:4700:4700::1111, 1.0.0.1, 2606:4700:4700::1001
MTU = 1280
S1 = 0
S2 = 0
Jc = 120
Jmin = 23
Jmax = 911
H1 = 1
H2 = 2
H3 = 3
H4 = 4

[Peer]
PublicKey = {peer_pub_b64}
AllowedIPs = {allowed_v4}, {allowed_v6}
Endpoint = {endpoint_host}:{endpoint_port}
"""
    return config

def main():
    print("[1/4] Generating Curve25519 keypair...")
    priv_b64, pub_b64 = gen_x25519_keypair()
    print(f"  Private key: {priv_b64[:16]}... (hidden)")
    print(f"  Public key:  {pub_b64}")

    print("\n[2/4] Registering with Cloudflare WARP API...")
    reg_data = register_warp(pub_b64)
    reg_id = reg_data['result']['id']
    reg_token = reg_data['result']['token']
    print(f"  Registration ID: {reg_id[:20]}...")
    print(f"  Token: {reg_token[:20]}...")

    print("\n[3/4] Enabling WARP...")
    enable_data = enable_warp(reg_id, reg_token)
    cfg = enable_data.get('result', {}).get('config', {})
    peer_pub = cfg.get('peers', [{}])[0].get('public_key', '')
    client_v4 = cfg.get('interface', {}).get('addresses', {}).get('v4', '')
    client_v6 = cfg.get('interface', {}).get('addresses', {}).get('v6', '')
    print(f"  Peer public key: {peer_pub}")
    print(f"  Client IPv4: {client_v4}")
    print(f"  Client IPv6: {client_v6}")

    print("\n[4/4] Building config...")
    config_text = build_config(priv_b64, client_v4, client_v6, peer_pub)

    # Store outside git repo
    secrets_dir = os.path.expanduser("~/sentinel-secrets")
    os.makedirs(secrets_dir, exist_ok=True)
    config_path = os.path.join(secrets_dir, "warp-helius-only.conf")
    with open(config_path, 'w') as f:
        f.write(config_text)
    print(f"\nConfig written to: {config_path}")
    print(f"  Size: {len(config_text)} bytes")

    # Print summary
    print("\n--- Config preview (first 10 lines) ---")
    for line in config_text.strip().split('\n')[:10]:
        print(line)
    print(f"...({len(config_text.split(chr(10)))} lines total)")

if __name__ == '__main__':
    main()
