"""Address validation and normalization (PRD §9 "Normalization").

TRON addresses stay in base58check form (`T…`). BSC addresses are stored lowercase and shown with
their EIP-55 checksum; a mixed-case BSC address must carry a valid checksum (AT-10).
"""

import re

import base58
from eth_utils.address import is_checksum_address, to_checksum_address

from amlcheck.core.models import Address, Chain

TRON_FORMAT = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")
EVM_FORMAT = re.compile(r"0x[0-9a-fA-F]{40}")


class AddressError(ValueError):
    """The text is not a valid address, or not one of the chain asked for."""


def parse(text: str, chain: Chain | None = None) -> Address:
    """Validate an address and detect its chain from the format, or check it against `chain`."""
    value = text.strip()
    if TRON_FORMAT.fullmatch(value):
        found = Chain.tron
    elif EVM_FORMAT.fullmatch(value):
        found = Chain.bsc
    else:
        raise AddressError(
            f"{value!r} is not a TRON address (T followed by 33 characters) "
            "or a BSC address (0x followed by 40 hex digits)"
        )
    if chain is not None and chain != found:
        raise AddressError(f"{value} is a {found.upper()} address, not {chain.upper()}")
    if found is Chain.tron:
        check_tron(value)
        return Address(Chain.tron, value, value)
    return _evm(value)


def check_tron(value: str) -> None:
    try:
        raw = base58.b58decode_check(value)
    except ValueError as e:
        raise AddressError(f"{value} fails its TRON checksum: check it for a typo") from e
    if len(raw) != 21 or raw[0] != 0x41:
        raise AddressError(f"{value} is not a TRON account address")


def _evm(value: str) -> Address:
    body = value[2:]
    if body not in (body.lower(), body.upper()) and not is_checksum_address(value):
        raise AddressError(
            f"{value} mixes upper and lower case but its EIP-55 checksum is wrong: "
            "check it for a typo"
        )
    normalized = value.lower()
    return Address(Chain.bsc, normalized, to_checksum_address(normalized))


def tron_from_hex(value: str) -> str:
    """A TRON address given as 20 bytes of hex (how TronGrid reports event arguments) or 21 with
    the 41 prefix, in its base58 `T…` form."""
    raw = bytes.fromhex(value.removeprefix("0x"))
    if len(raw) == 20:
        raw = b"\x41" + raw
    return base58.b58encode_check(raw).decode()


def tron_abi_word(address: str) -> str:
    """A TRON address as the 32-byte ABI word a contract call takes as its parameter."""
    return base58.b58decode_check(address)[1:].hex().rjust(64, "0")
