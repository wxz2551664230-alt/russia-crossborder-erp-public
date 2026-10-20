import pytest
from pydantic import ValidationError

from erp.domains.pod.api import PodWbAvailability, PodWbExportCreate, PodWbExportPreview


def test_wb_product_batch_accepts_1000_and_rejects_1001():
    ids = [f'product-{index}' for index in range(1001)]
    create = dict(product_ids=ids[:1000], store_id='store', template_id='template',
                  brand='No brand', price_cny=1)
    assert len(PodWbExportCreate.model_validate(create).product_ids) == 1000
    assert len(PodWbExportPreview(product_ids=ids[:1000], subject_id=1).product_ids) == 1000
    assert len(PodWbAvailability(store_id='store', product_ids=ids[:1000]).product_ids) == 1000
    for model, payload in (
        (PodWbExportCreate, {**create, 'product_ids': ids}),
        (PodWbExportPreview, {'product_ids': ids, 'subject_id': 1}),
        (PodWbAvailability, {'store_id': 'store', 'product_ids': ids}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)
