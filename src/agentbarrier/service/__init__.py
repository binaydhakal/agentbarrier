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
from agentbarrier.service.webhooks import (
    WebhookConfig,
    WebhookDelivery,
    WebhookDeliverySnapshot,
    WebhookDeliveryStore,
    WebhookEndpoint,
    WebhookSender,
    WebhookWorker,
    build_webhook_body,
    signature_headers,
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
    "WebhookConfig",
    "WebhookDelivery",
    "WebhookDeliverySnapshot",
    "WebhookDeliveryStore",
    "WebhookEndpoint",
    "WebhookSender",
    "WebhookWorker",
    "build_webhook_body",
    "create_approval_app",
    "create_dashboard_app",
    "create_slack_app",
    "hash_bearer_token",
    "signature_headers",
]
