from erp.core.database import SessionLocal
from erp.models import ResearchProduct


def test_research_products_keep_import_table_order(client):
    with SessionLocal() as session:
        session.add_all([
            ResearchProduct(source="seerfar", source_sku="ROW-3", title="第三行", source_file="products.xlsx", source_row=3),
            ResearchProduct(source="seerfar", source_sku="ROW-1", title="第一行", source_file="products.xlsx", source_row=1),
            ResearchProduct(source="seerfar", source_sku="ROW-2", title="第二行", source_file="products.xlsx", source_row=2),
        ])
        session.commit()
    response = client.get("/api/v1/research-products?page_size=20")
    assert response.status_code == 200
    assert [item["source_sku"] for item in response.json()["items"][:3]] == ["ROW-1", "ROW-2", "ROW-3"]
