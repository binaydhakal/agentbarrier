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
from agentbarrier.service.dashboard import (
    ApprovalDashboard,
    DashboardSession,
    DashboardSessionStore,
    create_dashboard_app,
)
from agentbarrier.service.slack import (
    SlackConfig,
    SlackInteractionService,
    SlackNotificationSnapshot,
    SlackNotificationStore,
    SlackReviewer,
    SlackWorker,
    create_slack_app,
)

__all__ = [
    "ALL_SERVICE_SCOPES",
    "ApprovalAPI",
    "ApprovalDashboard",
    "DashboardSession",
    "DashboardSessionStore",
    "Principal",
    "SlackConfig",
    "SlackInteractionService",
    "SlackNotificationSnapshot",
    "SlackNotificationStore",
    "SlackReviewer",
    "SlackWorker",
    "StaticBearerAuth",
    "create_approval_app",
    "create_dashboard_app",
    "create_slack_app",
    "hash_bearer_token",
]
