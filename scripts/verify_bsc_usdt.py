# /// script
# requires-python = ">=3.12"
# dependencies = ["certifi", "pycryptodome"]
# ///
"""Can USDT on BNB Smart Chain freeze or blacklist an address? (docs/verification.md, V6)

Reads the token's runtime bytecode from a public BSC node, lists every function the
contract dispatches and names them from a public signature database. No API key needed:

    uv run scripts/verify_bsc_usdt.py
"""

import json
import ssl
import urllib.request
from typing import Any

import certifi
from Crypto.Hash import keccak

RPC = "https://bsc-dataseed.bnbchain.org"
USDT = "0x55d398326f99059fF775485246999027B3197955"
SIGNATURES = "https://api.4byte.sourcify.dev/signature-database/v1/lookup?filter=true&function="
PROXY_SLOTS = {
    "EIP-1967 implementation": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
    "EIP-1967 beacon": "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50",
    "OpenZeppelin legacy": "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3",
}
# Names used by Tether (TRON/Ethereum USDT), Circle (USDC) and common freezable tokens.
FREEZE_FUNCTIONS = [
    "isBlackListed(address)",
    "getBlackListStatus(address)",
    "addBlackList(address)",
    "removeBlackList(address)",
    "destroyBlackFunds(address)",
    "isBlacklisted(address)",
    "blacklist(address)",
    "unBlacklist(address)",
    "freeze(address)",
    "unfreeze(address)",
    "freezeAccount(address,bool)",
    "frozenAccount(address)",
    "pause()",
    "paused()",
]
CONTEXT = ssl.create_default_context(cafile=certifi.where())


def fetch(url: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "User-Agent": "amlcheck-verification"}
    request = urllib.request.Request(url, data, headers)
    with urllib.request.urlopen(request, timeout=60, context=CONTEXT) as response:
        return json.loads(response.read())


def rpc(method: str, *params: Any) -> Any:
    reply = fetch(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)})
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply["result"]


def selector(signature: str) -> str:
    return "0x" + keccak.new(digest_bits=256, data=signature.encode()).hexdigest()[:8]


def call_string(signature: str) -> str:
    raw = bytes.fromhex(rpc("eth_call", {"to": USDT, "data": selector(signature)}, "latest")[2:])
    length = int.from_bytes(raw[32:64], "big")
    return raw[64 : 64 + length].decode()


def dispatched(code: bytes) -> list[str]:
    """Selectors in Solidity's dispatcher: PUSH4 <selector> followed within two opcodes by EQ."""
    ops, i = [], 0
    while i < len(code):
        size = code[i] - 0x5F if 0x60 <= code[i] <= 0x7F else 0
        ops.append((code[i], code[i + 1 : i + 1 + size].hex()))
        i += 1 + size
    found = set()
    for j, (op, value) in enumerate(ops):
        if op == 0x63 and any(later == 0x14 for later, _ in ops[j + 1 : j + 3]):
            found.add("0x" + value)
    return sorted(found)


def main() -> None:
    print(f"chain id {int(rpc('eth_chainId'), 16)}, block {int(rpc('eth_blockNumber'), 16)}")
    decimals = int(rpc("eth_call", {"to": USDT, "data": selector("decimals()")}, "latest"), 16)
    print(f"{USDT}: {call_string('name()')} ({call_string('symbol()')}), {decimals} decimals")
    for name, slot in PROXY_SLOTS.items():
        empty = int(rpc("eth_getStorageAt", USDT, slot, "latest"), 16) == 0
        print(f"proxy slot {name}: {'empty' if empty else 'SET, check the implementation too'}")

    selectors = dispatched(bytes.fromhex(rpc("eth_getCode", USDT, "latest")[2:]))
    names = fetch(SIGNATURES + ",".join(selectors))["result"]["function"]
    print(f"\n{len(selectors)} functions:")
    for sel in selectors:
        print(f"  {sel}  {' | '.join(n['name'] for n in names.get(sel) or []) or '(unknown)'}")

    found = [f for f in FREEZE_FUNCTIONS if selector(f) in selectors]
    print("\nfreeze / blacklist / pause functions:", ", ".join(found) or "none")


if __name__ == "__main__":
    main()
