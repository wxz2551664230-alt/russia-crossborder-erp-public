"""Merge read-only POD history produced on the initiating computer.

Existing shared rows are immutable here: this path only fills missing source IDs.
"""
import re
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.models import Asset, CatalogImportRun, CatalogProduct, CatalogProductAsset, CatalogVariant
from erp.services.catalog import POD_LEGACY_SOURCE, _asset_role, _pod_supplier_from_sku, link_catalog_asset


def _text(value) -> str:
    return str(value or '')


def _valid_http(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == 'https' and bool(parsed.hostname)


def accept_pod_history(session: Session, run: CatalogImportRun, value: dict) -> dict:
    if run.kind != 'pod_history' or run.dry_run:
        raise ConflictError('不是 POD 历史正式导入')
    if (not isinstance(value, dict) or set(value) != {'schema', 'source_sha256', 'report', 'entries'}
            or value['schema'] != 1 or not re.fullmatch(r'[a-f0-9]{64}', str(value['source_sha256']))):
        raise ConflictError('POD 历史回执格式无效')
    report, entries = value['report'], value['entries']
    if (not isinstance(report, dict) or not isinstance(entries, list) or len(entries) > 100_000
            or not isinstance(report.get('counts'), dict)
            or report['counts'].get('products') != len(entries)):
        raise ConflictError('POD 历史回执数量无效')
    product_ids, variant_ids, asset_ids = set(), set(), set()
    media_count = variant_count = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'product', 'category', 'version', 'variants', 'media'}:
            raise ConflictError('POD 历史产品结构无效')
        product, version = entry['product'], entry['version']
        variants, media = entry['variants'], entry['media']
        if (not isinstance(product, dict) or not isinstance(version, dict)
                or not isinstance(entry['category'], str) or not isinstance(variants, list)
                or not isinstance(media, list) or len(variants) > 1000 or len(media) > 1000
                or not _text(product.get('id')) or not _text(product.get('sku'))
                or _text(product['id']) in product_ids):
            raise ConflictError('POD 历史产品字段无效')
        product_ids.add(_text(product['id']))
        if version and _text(version.get('id')) != _text(product.get('current_version_id')):
            raise ConflictError('POD 产品当前版本不匹配')
        for row in variants:
            if (not isinstance(row, dict) or not _text(row.get('id'))
                    or _text(row.get('product_id')) != _text(product['id'])
                    or not _text(row.get('sku')) or _text(row['id']) in variant_ids):
                raise ConflictError('POD 历史规格关联无效')
            variant_ids.add(_text(row['id']))
        for row in media:
            url = _text(row.get('oss_url') or row.get('oss_uri')) if isinstance(row, dict) else ''
            if (not isinstance(row, dict) or not version or not _text(row.get('id'))
                    or _text(row.get('version_id')) != _text(version.get('id'))
                    or _text(row['id']) in asset_ids or not _valid_http(url)
                    or not re.fullmatch(r'[a-f0-9]{64}', _text(row.get('sha256')))
                    or type(row.get('size')) is not int or row['size'] <= 0):
                raise ConflictError('POD 历史图片缺少可独立读取的 OSS 数据')
            asset_ids.add(_text(row['id']))
        variant_count += len(variants); media_count += len(media)
    counts = report['counts']
    if (counts.get('product_variants') != variant_count
            or type(counts.get('product_oss_assets')) is not int
            or media_count > counts['product_oss_assets']):
        raise ConflictError('POD 历史规格或图片数量不一致')

    products = {row.legacy_id: row for row in session.scalars(select(CatalogProduct).where(
        CatalogProduct.legacy_source == POD_LEGACY_SOURCE)).all()}
    variants = {row.legacy_id: row for row in session.scalars(select(CatalogVariant).where(
        CatalogVariant.legacy_source == POD_LEGACY_SOURCE)).all()}
    assets = {row.legacy_id: row for row in session.scalars(select(Asset).where(
        Asset.legacy_source == POD_LEGACY_SOURCE)).all()}
    linked = {(row.product_id, row.asset_id) for row in session.scalars(select(CatalogProductAsset).where(
        CatalogProductAsset.source_kind == POD_LEGACY_SOURCE)).all()}
    inserted_products = inserted_variants = inserted_assets = inserted_links = 0
    for entry in entries:
        raw, version = entry['product'], entry['version']
        legacy_id = _text(raw['id'])
        item = products.get(legacy_id)
        if item is None:
            item = CatalogProduct(legacy_source=POD_LEGACY_SOURCE, legacy_id=legacy_id,
                                  source_type='pod', sku=_text(raw['sku']))
            session.add(item); session.flush(); products[legacy_id] = item
            inserted_products += 1
            item.group_code = _text(raw.get('family_no') or raw['sku'] or legacy_id)
            item.name = _text(raw.get('name') or raw['sku'] or 'POD 产品')
            item.category = entry['category']
            item.supplier = _pod_supplier_from_sku(item.sku) or 'HICUSTOM/S2B'
            item.supplier_product_id = _text(version.get('product_type_id'))
            item.notes = _text(raw.get('notes'))
            item.status = _text(raw.get('status') or 'ready')
            item.tags_json = _text(raw.get('tags_json') or '[]')
            item.production_json = dumps({
                'version_id': version.get('id'), 'version_no': version.get('version_no'),
                'job_id': version.get('job_id'), 'job_status': version.get('job_status'),
                'product_type_id': version.get('product_type_id'),
                'default_color_id': version.get('default_color_id'),
                'default_view_id': version.get('default_view_id'),
                'product_code': version.get('product_code'),
                'product_code_status': version.get('product_code_status'),
                'output_folder': version.get('output_folder'),
                'recipe': loads(_text(version.get('recipe_json') or '{}'), {}),
                'assets': loads(_text(version.get('assets_json') or '{}'), {}),
            })
            item.source_snapshot_json = dumps({'legacy_product': raw,
                                              'source_database': report.get('source_database', '')})
        for row in entry['variants']:
            identity = _text(row['id'])
            if identity in variants:
                continue
            variant = CatalogVariant(product_id=item.id, legacy_source=POD_LEGACY_SOURCE, legacy_id=identity)
            variant.sku = _text(row['sku'])
            variant.supplier_product_id = _text(version.get('product_type_id'))
            variant.supplier_sku_id = _text(row.get('stock_item_id'))
            variant.specification = _text(row.get('stock_code'))
            variant.color = _text(row.get('color_name'))
            variant.size = _text(row.get('size_name'))
            variant.package_length_mm = row.get('package_length')
            variant.package_width_mm = row.get('package_width')
            variant.package_height_mm = row.get('package_height')
            variant.weight_grams = row.get('weight')
            variant.active = bool(row.get('active'))
            variant.properties_json = dumps({
                'variant_no': row.get('variant_no'), 'color_id': row.get('color_id'),
                'size_id': row.get('size_id'), 'stock_code': row.get('stock_code'),
                'package_volume': row.get('package_volume'),
                'size_attributes': loads(_text(row.get('size_attributes_json') or '[]'), []),
                'prices': loads(_text(row.get('prices_json') or '{}'), {}),
                'image_group_id': row.get('image_group_id'), 'selected': bool(row.get('selected')),
            })
            session.add(variant); variants[identity] = variant; inserted_variants += 1
        rendering_index = 0
        for position, row in enumerate(entry['media']):
            identity = _text(row['id'])
            asset = assets.get(identity)
            if asset is None:
                asset = Asset(legacy_source=POD_LEGACY_SOURCE, legacy_id=identity)
                asset.kind = _text(row.get('asset_type') or 'image')
                asset.name = _text(row.get('name') or Path(_text(row.get('oss_key') or identity)).name)
                asset.source = 'pod'; asset.local_path = ''
                asset.oss_bucket = _text(row.get('oss_bucket'))
                asset.oss_key = _text(row.get('oss_key'))
                asset.oss_url = _text(row.get('oss_url') or row.get('oss_uri'))
                asset.sha256 = _text(row.get('sha256'))
                asset.mime_type = _text(row.get('content_type') or 'application/octet-stream')
                asset.size_bytes = int(row['size']); asset.read_only = True
                asset.mirror_status = 'mirrored'
                asset.metadata_json = dumps({'color_id': row.get('color_id'),
                                             'color_name': row.get('color_name'),
                                             'view_id': row.get('view_id'),
                                             'etag': row.get('etag'), 'crc64': row.get('crc64')})
                session.add(asset); session.flush(); assets[identity] = asset
                inserted_assets += 1
            asset_type = _text(row.get('asset_type'))
            role_position = rendering_index if asset_type == 'rendering' else position
            if (item.id, asset.id) not in linked:
                link_catalog_asset(session, product=item, asset=asset,
                                   role=_asset_role(asset_type, role_position), position=role_position,
                                   source_kind=POD_LEGACY_SOURCE,
                                   metadata={'color_id': row.get('color_id'),
                                             'color_name': row.get('color_name'),
                                             'view_id': row.get('view_id')})
                linked.add((item.id, asset.id)); inserted_links += 1
            if asset_type == 'rendering':
                rendering_index += 1
    run.status = 'completed'
    run.report_json = dumps(report)
    run.counts_json = dumps({'inserted': inserted_products, 'updated': 0,
                             'variants': inserted_variants, 'assets': inserted_assets,
                             'links': inserted_links})
    return {'inserted': inserted_products, 'updated': 0, 'variants': inserted_variants,
            'assets': inserted_assets, 'links': inserted_links, 'status': 'completed'}
