"""Fetch real on-chain swap transactions for each DEX venue."""
import json, logging, sys, asyncio

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.signature import Signature

logging.basicConfig(level=logging.WARNING)

HELIUS = "https://mainnet.helius-rpc.com/?api-key=33a9f314-bc9f-452d-bd59-ced96126d602"

VENUES = {
    "PumpFun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "PumpSwap": "pAMMPxompa13c2qojFgUGSXXysyLLCUmSXwG8M7fKtM",
    "RaydiumAmmV4": "675kPX9MHTjS2zt1qfr1NYyze2V9cWzmRpJnLkzFY7",
    "RaydiumCpmm": "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP",
}

async def find_tx(client, label, prog_addr):
    print(f"\n{'='*60}")
    print(f"=== {label} ({prog_addr})")
    print(f"{'='*60}")
    
    from solders.pubkey import Pubkey
    try:
        # Use from_string - some pubkeys have length issues, try alternative
        pk = Pubkey.from_string(prog_addr)
    except:
        import base58
        pk = Pubkey.from_bytes(base58.b58decode(prog_addr))
    
    try:
        sigs = await client.get_signatures_for_address(pk, limit=10, commitment=Confirmed)
    except Exception as e:
        print(f"ERROR get_signatures_for_address: {e}")
        return
    
    if not sigs or not sigs.value:
        print("No signatures found")
        return
    
    for sig_info in sigs.value[:2]:
        sig = sig_info.signature
        print(f"\n--- Signature: {sig} (slot {sig_info.slot}) ---")
        
        try:
            tx = await client.get_transaction(sig, max_supported_transaction_version=0, commitment=Confirmed)
        except Exception as e:
            print(f"  ERROR fetching: {e}")
            continue
        
        if not tx or not tx.value:
            print("  Empty tx")
            continue
        
        meta = tx.value.meta
        msg = tx.value.transaction.message
        accts = msg.account_keys
        prog_pk = accts[0]  # use whatever type from api
        
        # Find a matching instruction by program id string comparison
        found = False
        for ix in msg.instructions:
            prog_id = accts[ix.program_id_index]
            if str(prog_id) == prog_addr:
                data_hex = ix.data
                data_bytes = list(bytes.fromhex(data_hex))
                ix_accts = [str(accts[i]) for i in ix.accounts]
                
                print(f"  DIRECT IX: {len(ix_accts)} accounts, {len(data_bytes)} bytes data")
                print(f"  Data hex: {data_hex[:80]}...")
                print(f"  Data bytes: {data_bytes}")
                print(f"  Accounts [{len(ix_accts)}]:")
                for j, a in enumerate(ix_accts):
                    print(f"    [{j}] {a}")
                found = True
                break
        
        if not found and meta.inner_instructions:
            for inner_set in meta.inner_instructions:
                for inner_ix in inner_set.instructions:
                    if len(accts) > inner_ix.program_id_index:
                        iprog = str(accts[inner_ix.program_id_index])
                        if iprog == prog_addr:
                            data_bytes = list(bytes.fromhex(inner_ix.data))
                            print(f"  INNER IX (CPI): {len(data_bytes)} bytes data")
                            print(f"  Data bytes: {data_bytes}")
                            found = True
                            break
                if found:
                    break
        
        if not found:
            print("  No matching instruction found")

async def main():
    async with AsyncClient(HELIUS) as client:
        for name, addr in VENUES.items():
            await find_tx(client, name, addr)

asyncio.run(main())
