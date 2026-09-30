"""Block explorer links, built only from values that match an address or transaction format.

Tronscan's own front end routes /transaction/:hash and /address/:id behind #/; BNB Chain's
repositories link BscScan as /tx/ and /address/ (docs/verification.md, V14).
"""

import re

from amlcheck.core.models import Chain

EVM_TX = re.compile(r"0x[0-9a-fA-F]{64}")
EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
TRON_TX = re.compile(r"[0-9a-fA-F]{64}")
TRON_ADDRESS = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")


def explorer(chain: str, value: str) -> str | None:
    """A block explorer page for an address or transaction on `chain`, or None for anything else."""
    if chain == Chain.bsc:
        if EVM_TX.fullmatch(value):
            return f"https://bscscan.com/tx/{value}"
        if EVM_ADDRESS.fullmatch(value):
            return f"https://bscscan.com/address/{value}"
    elif chain == Chain.tron:
        if TRON_TX.fullmatch(value):
            return f"https://tronscan.org/#/transaction/{value}"
        if TRON_ADDRESS.fullmatch(value):
            return f"https://tronscan.org/#/address/{value}"
    return None
