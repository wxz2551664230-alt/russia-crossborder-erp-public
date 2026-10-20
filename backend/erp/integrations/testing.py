from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeSupplierAdapter:
    calls: list[str] = field(default_factory=list)

    def get_product(self, source_type: str, sku: str) -> dict[str, Any]:
        self.calls.append(sku)
        return {"source_type":source_type,"sku":sku,"name":f"测试产品 {sku}","purchase_price_cny":20,"package_length_mm":300,"package_width_mm":200,"package_height_mm":100,"package_weight_g":500}


@dataclass
class FakeAIAdapter:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def generate(self, facts: dict[str, Any], store_id: str) -> dict[str, Any]:
        self.calls.append((str(facts["sku"]),store_id))
        return {"title":f"RU {facts['sku']} {store_id}","description_ru":f"Fresh copy for {store_id}","tags":[f"#store_{store_id}"],"attributes":[{"id":1,"values":[{"value":facts["sku"]}]}]}


@dataclass
class FakeImageAdapter:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def generate(self, sku: str, role: str) -> str:
        self.calls.append((sku,role))
        return f"memory://{sku}/{role}.jpg"


@dataclass
class FakeOssAdapter:
    calls: list[str] = field(default_factory=list)

    def upload(self, image: str) -> str:
        self.calls.append(image)
        digest=hashlib.sha256(image.encode()).hexdigest()[:16]
        return f"https://fake-oss.invalid/{digest}.jpg"


@dataclass
class FakeOzonAdapter:
    calls: list[dict[str, Any]] = field(default_factory=list)
    submitted: dict[str, int] = field(default_factory=dict)

    def submit(self, package: dict[str, Any], idempotency_key: str) -> int:
        if idempotency_key in self.submitted: return self.submitted[idempotency_key]
        task_id=len(self.submitted)+1;self.submitted[idempotency_key]=task_id;self.calls.append(package)
        return task_id


@dataclass
class PublicationTestHarness:
    supplier: FakeSupplierAdapter = field(default_factory=FakeSupplierAdapter)
    ai: FakeAIAdapter = field(default_factory=FakeAIAdapter)
    image: FakeImageAdapter = field(default_factory=FakeImageAdapter)
    oss: FakeOssAdapter = field(default_factory=FakeOssAdapter)
    ozon: FakeOzonAdapter = field(default_factory=FakeOzonAdapter)

    def run(self, source_type: str, sku: str, store_ids: list[str], image_policy: str = "reuse") -> list[dict[str, Any]]:
        facts=self.supplier.get_product(source_type,sku);results=[]
        for store_id in store_ids:
            content=self.ai.generate(facts,store_id)
            main="https://fake-oss.invalid/reused-main.jpg"
            if image_policy in {"regenerate_main","regenerate_all"}: main=self.oss.upload(self.image.generate(sku,"main"))
            package={"offer_id":f"{store_id}-{sku}","name":content["title"],"description_ru":content["description_ru"],"attributes":content["attributes"],"primary_image":main,"source_type":source_type}
            package["task_id"]=self.ozon.submit(package,f"{source_type}:{sku}:{store_id}");results.append(package)
        return results
