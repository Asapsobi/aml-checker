from datetime import UTC, datetime, timedelta
from decimal import Decimal

from amlcheck.exposure.heuristics import Burst, busiest_window, pass_through
from amlcheck.exposure.history import Transfer

ME = "Tme"
T0 = datetime(2026, 9, 1, tzinfo=UTC)
DAY = timedelta(hours=24)


def t(minutes: float, sender: str, recipient: str, amount: str) -> Transfer:
    return Transfer(
        f"tx{minutes}", T0 + timedelta(minutes=minutes), sender, recipient, Decimal(amount)
    )


def test_money_forwarded_within_the_window_passes_through() -> None:
    received, passed = pass_through(ME, [t(0, "A", ME, "100"), t(30, ME, "B", "95")], DAY)
    assert (received, passed) == (Decimal(100), Decimal(95))


def test_money_that_stays_longer_does_not_count() -> None:
    received, passed = pass_through(ME, [t(0, "A", ME, "100"), t(60 * 25, ME, "B", "100")], DAY)
    assert (received, passed) == (Decimal(100), Decimal(0))


def test_first_in_first_out_across_several_arrivals() -> None:
    transfers = [
        t(0, "A", ME, "50"),  # sent on 30 hours later: too slow
        t(60 * 29, "B", ME, "50"),  # sent on within the hour
        t(60 * 30, ME, "C", "100"),
    ]
    assert pass_through(ME, transfers, DAY) == (Decimal(100), Decimal(50))


def test_spending_what_was_there_before_the_history_is_not_pass_through() -> None:
    assert pass_through(ME, [t(0, ME, "C", "100")], DAY) == (Decimal(0), Decimal(0))


def test_busiest_window_counts_distinct_counterparties() -> None:
    events = [
        (T0 + timedelta(hours=h), who) for h, who in [(0, "A"), (1, "A"), (2, "B"), (30, "C")]
    ]
    burst = busiest_window(events, DAY)
    assert burst == Burst(2, T0, T0 + timedelta(hours=2))


def test_busiest_window_slides_forward() -> None:
    events = [(T0 + timedelta(hours=10 * n), f"S{n}") for n in range(6)]
    burst = busiest_window(events, DAY)
    assert burst.count == 3  # any 24 hours hold at most three arrivals ten hours apart
    assert busiest_window([], DAY) == Burst(0)
