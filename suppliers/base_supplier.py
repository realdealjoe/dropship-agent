from dataclasses import dataclass
from abc import ABC, abstractmethod


@dataclass
class SupplierProduct:
    supplier: str
    product_id: str
    sku: str
    title: str
    description: str
    price: float
    images: list[str]
    shipping_days: int
    category: str


@dataclass
class SupplierOrder:
    supplier_order_id: str
    status: str
    tracking_number: str = ""
    tracking_url: str = ""
    carrier: str = ""


class BaseSupplier(ABC):
    name: str = ""

    @abstractmethod
    def search_products(self, keyword: str, max_price: float = 50.0,
                        limit: int = 10) -> list[SupplierProduct]:
        ...

    @abstractmethod
    def place_order(self, product_id: str, sku: str, quantity: int,
                    shipping_address: dict) -> SupplierOrder:
        ...

    @abstractmethod
    def get_order_status(self, supplier_order_id: str) -> SupplierOrder:
        ...
