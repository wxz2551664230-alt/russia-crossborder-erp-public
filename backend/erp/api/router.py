from fastapi import APIRouter

from erp.api.routes import ad_optimization, assets, catalog, connections, dashboard, jobs, listings, ozon_gallery, publications, research, settings, stores, workers
from erp.domains.pod import api as pod_studio
from erp.domains.pod.ai_design import api as pod_ai_design
from erp.domains.orders import api as pod_orders
from erp.api.routes import auth
from erp.api.routes import execution_devices
from erp.api.routes import ai_usage
from erp.api.routes import codex_handoffs
from erp.api.routes import client_releases
from erp.api.routes import codex_actions


api_router = APIRouter()
from erp.api.routes import amazon
api_router.include_router(amazon.router)
api_router.include_router(amazon.local_router)
api_router.include_router(auth.router)
api_router.include_router(execution_devices.router)
api_router.include_router(ai_usage.router)
api_router.include_router(codex_handoffs.router)
api_router.include_router(codex_actions.router)
api_router.include_router(client_releases.router)
api_router.include_router(dashboard.router)
api_router.include_router(connections.router)
api_router.include_router(stores.router)
api_router.include_router(assets.router)
api_router.include_router(catalog.router)
api_router.include_router(publications.router)
api_router.include_router(pod_studio.router)
api_router.include_router(pod_ai_design.router)
api_router.include_router(pod_orders.router)
api_router.include_router(research.router)
api_router.include_router(listings.router)
api_router.include_router(ozon_gallery.router)
api_router.include_router(jobs.router)
api_router.include_router(workers.router)
api_router.include_router(settings.router)
api_router.include_router(ad_optimization.router)
