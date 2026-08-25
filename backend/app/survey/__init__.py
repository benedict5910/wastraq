"""City-scale survey module.

Deliberately separate from the operations demo:

    /dashboard        operations - one collection route, live picker events
    /survey/*         survey management - the whole authority

They share ONE property master (`properties`) and one set of geometry
tables. What separates them is scope, not a duplicated schema: the
operations dashboard filters to settings.DEMO_ROUTE_ID, the survey module
spans every administrative unit.
"""

from fastapi import APIRouter

from . import actions, api, qa_checks  # noqa: F401

router = APIRouter()
router.include_router(api.router)
router.include_router(actions.router)

__all__ = ["router", "qa_checks"]
