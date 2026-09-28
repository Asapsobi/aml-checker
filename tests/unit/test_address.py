import pytest

from amlcheck.core.address import AddressError, parse, tron_abi_word, tron_from_hex
from amlcheck.core.models import Chain

TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
LAZARUS = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"  # EIP-55 form, as OFAC lists it


def test_tron_address_is_detected_and_kept_as_is() -> None:
    address = parse(f"  {TRON_USDT} ")
    assert address.chain is Chain.tron
    assert address.normalized == address.display == TRON_USDT


def test_bsc_address_is_stored_lowercase_and_shown_checksummed() -> None:
    address = parse(LAZARUS)
    assert address.chain is Chain.bsc
    assert address.normalized == LAZARUS.lower()
    assert address.display == LAZARUS


@pytest.mark.parametrize("text", [LAZARUS.lower(), "0x" + LAZARUS[2:].upper()])
def test_single_case_bsc_address_needs_no_checksum(text: str) -> None:
    assert parse(text).normalized == LAZARUS.lower()


def test_mixed_case_with_a_bad_checksum_is_refused() -> None:
    """AT-10."""
    typo = LAZARUS[:-1] + "F"  # 0x…2f96 becomes 0x…2f9F
    with pytest.raises(AddressError, match="EIP-55"):
        parse(typo)


def test_tron_address_with_a_typo_fails_its_checksum() -> None:
    with pytest.raises(AddressError, match="checksum"):
        parse(TRON_USDT[:-1] + "u")


@pytest.mark.parametrize(
    "text",
    ["", "hello", "T123", TRON_USDT + "x", "0x1234", "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"],
)
def test_text_that_is_no_address_is_refused(text: str) -> None:
    with pytest.raises(AddressError):
        parse(text)


def test_chain_given_must_match_the_address() -> None:
    with pytest.raises(AddressError, match="TRON address, not BSC"):
        parse(TRON_USDT, Chain.bsc)
    assert parse(LAZARUS, Chain.bsc).chain is Chain.bsc


def test_tron_hex_forms_convert_to_base58() -> None:
    hex20 = "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"  # how TronGrid reports the USDT contract
    assert tron_from_hex(hex20) == TRON_USDT
    assert tron_from_hex("41" + hex20[2:]) == TRON_USDT
    assert tron_abi_word(TRON_USDT) == hex20[2:].rjust(64, "0")
