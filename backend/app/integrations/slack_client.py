"""
Slack client used by ReportingAgent to post the incident summary, and by the
/api/v1/slack/investigate endpoint to acknowledge a slash-command style
request. Real mode uses slack_sdk's WebClient. DEMO_MODE just logs/returns
the formatted message instead of calling the Slack API.
"""
import hashlib
import hmac
import logging
import time
from typing import Optional

from ..config import get_settings

logger = logging.getLogger("rootcause.integrations.slack_client")

# How old a request's X-Slack-Request-Timestamp is allowed to be before
# we reject it as a possible replay attack. Slack's own guidance is 5 minutes.
_MAX_REQUEST_AGE_SECONDS = 60 * 5


def verify_slack_signature(signing_secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """
    Verifies the `X-Slack-Signature` header per Slack's request-signing spec
    (https://api.slack.com/authentication/verifying-requests-from-slack):

        basestring = "v0:{timestamp}:{raw_body}"
        expected   = "v0=" + HMAC-SHA256(signing_secret, basestring).hexdigest()

    Returns False (never raises) for any malformed input -- callers should
    treat False as "reject the request", not "assume it's fine".
    """
    if not signing_secret or signing_secret == "changeme":
        # No secret configured -- fail closed rather than silently accepting
        # unverified requests. DEMO_MODE callers should not reach this path.
        return False
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > _MAX_REQUEST_AGE_SECONDS:
        return False

    basestring = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


class SlackClient:
    def __init__(self):
        self.settings = get_settings()
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        from slack_sdk import WebClient  # local import: optional dependency at import time
        from ..secrets import get_vault_client

        token = get_vault_client().get_slack_bot_token()
        self._client = WebClient(token=token)
        return self._client

    def post_message(self, text: str, channel: Optional[str] = None) -> dict:
        channel = channel or self.settings.slack_channel
        if self.settings.demo_mode:
            logger.info("[DEMO_MODE] Would post to Slack channel %s:\n%s", channel, text)
            return {"ok": True, "mode": "demo", "channel": channel, "text": text}

        client = self._get_client()
        try:
            resp = client.chat_postMessage(channel=channel, text=text)
            return {"ok": True, "mode": "live", "channel": channel, "ts": resp.get("ts")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack post failed: %s", exc)
            return {"ok": False, "mode": "live", "error": str(exc)}
