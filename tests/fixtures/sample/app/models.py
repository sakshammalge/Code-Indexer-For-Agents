"""Domain models for the inventory service."""
from dataclasses import dataclass

MAX_ITEMS = 500


@dataclass
class Item:
    """A single stock-keeping unit."""
    sku: str
    quantity: int = 0

    @property
    def in_stock(self) -> bool:
        return self.quantity > 0

    def restock(self, amount: int) -> None:
        """Add ``amount`` units to the item."""
        self.quantity += amount


class Warehouse:
    def __init__(self, name: str):
        self.name = name
        self.items: dict[str, Item] = {}

    async def fetch_remote_inventory(self, url: str) -> list[Item]:
        """Pull inventory from a remote warehouse API."""
        return []
