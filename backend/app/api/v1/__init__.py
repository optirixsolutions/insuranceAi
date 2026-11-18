from fastapi import APIRouter

from app.api.v1 import agents as agent_runs
from app.api.v1 import (
    ai,
    audit,
    auth,
    catalog,
    claims,
    documents,
    health,
    organization,
    roles,
    users,
)
from app.api.v1 import analytics as analytics_api
from app.api.v1 import communications as communications_api
from app.api.v1 import intelligence as intelligence_api
from app.api.v1 import mfa as mfa_api
from app.api.v1 import processing as processing_api
from app.api.v1 import reviews as reviews_api
from app.api.v1 import rules as rules_api

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(organization.router)
api_router.include_router(users.router)
api_router.include_router(roles.router)
api_router.include_router(audit.router)
api_router.include_router(catalog.router)
api_router.include_router(claims.router)
api_router.include_router(documents.router)
api_router.include_router(ai.router)
api_router.include_router(agent_runs.router)
api_router.include_router(processing_api.router)
api_router.include_router(rules_api.router)
api_router.include_router(reviews_api.router)
api_router.include_router(analytics_api.router)
api_router.include_router(communications_api.router)
api_router.include_router(intelligence_api.router)
api_router.include_router(mfa_api.router)
