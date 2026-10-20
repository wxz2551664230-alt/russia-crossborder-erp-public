from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ProductCandidate:
    source_file: str
    source_row: int
    rank: int | None = None
    sku: str = ""
    title: str = ""
    ozon_url: str = ""
    image_url: str = ""
    image_urls: list[str] = field(default_factory=list)
    brand: str = ""
    category: str = ""
    sale_method: str = ""
    selling_price_rub: float | None = None
    sales: int | None = None
    revenue_rub: float | None = None
    sales_growth: float | None = None
    revenue_growth: float | None = None
    gross_margin: float | None = None
    rating: float | None = None
    review_count: int | None = None
    seller: str = ""
    seller_type: str = ""
    delivery_method: str = ""
    weight_grams: float | None = None
    listed_date: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
