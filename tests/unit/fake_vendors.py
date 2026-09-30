"""Stand-in vendors for the tests: config.toml names them as "fake_vendors:Class"."""

from amlcheck.core.models import Address
from amlcheck.vendor import Attribution


class Knows:
    name = "Knows"

    async def attribute(self, address: Address) -> Attribution | None:
        return Attribution(
            "Binance", "exchange", "low", {"cluster": f"cluster of {address.display}"}
        )


class KnowsNothing:
    name = "Knows nothing"

    async def attribute(self, address: Address) -> Attribution | None:
        return None


class Broken:
    name = "Broken"

    async def attribute(self, address: Address) -> Attribution | None:
        raise RuntimeError("the vendor is down")
