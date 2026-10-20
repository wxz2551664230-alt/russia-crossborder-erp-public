from contextvars import ContextVar

# Request-local identity propagates into FastAPI's worker thread. Never use a
# process-global "current user" in a server shared by multiple people.
task_identity: ContextVar[dict | None] = ContextVar("erp_task_identity", default=None)
