"""Authenticated approval service.

Install ``agentbarrier[service]`` to use this optional module.
"""

from agentbarrier.service.api import ApprovalAPI, create_approval_app
from agentbarrier.service.auth import (
    ALL_SERVICE_SCOPES,
    Principal,
    StaticBearerAuth,
    hash_bearer_token,
)

__all__ = [
    "ALL_SERVICE_SCOPES",
    "ApprovalAPI",
    "Principal",
    "StaticBearerAuth",
    "create_approval_app",
    "hash_bearer_token",
]
