"""One library classification shared by the POD and Amazon selection views."""
from sqlalchemy import and_, case

from erp.models import CatalogProduct


AMAZON_POD_ROOT = "Amazon 美国 POD"


def category_expression():
    # The market is stored in source_type. Old US imports used category for
    # that root too; display their supplier product name until users assign a
    # real internal category. No taxonomy guessing or supplier calls.
    return case(
        (and_(CatalogProduct.source_type == "pod_amazon",
              CatalogProduct.category.in_(["", AMAZON_POD_ROOT])), CatalogProduct.name),
        else_=CatalogProduct.category,
    )


def category_label(product: CatalogProduct) -> str:
    if product.source_type == "pod_amazon" and product.category in ("", AMAZON_POD_ROOT):
        return product.name
    return product.category
