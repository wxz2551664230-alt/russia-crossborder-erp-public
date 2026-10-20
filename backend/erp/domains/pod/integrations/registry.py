from __future__ import annotations

from sqlalchemy.orm import Session

from erp.core.errors import AppError
from erp.domains.pod.integrations.common import load_supplier_connection
from erp.domains.pod.integrations.hicustom import HicustomClient
from erp.domains.pod.integrations.s2b import S2BClient


def supplier_client(session: Session, provider: str, connection_id: str = "") -> HicustomClient | S2BClient:
    normalized = str(provider or "").strip().casefold()
    connection = load_supplier_connection(session, normalized, connection_id)
    if normalized == "hicustom":
        return HicustomClient(connection)
    if normalized == "s2b":
        return S2BClient(connection)
    raise AppError("POD 当前只支持 HICUSTOM 和 S2B", code="pod_supplier_unsupported", status_code=422)
