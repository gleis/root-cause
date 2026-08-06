"""
Vault-backed secrets access, with a clean fallback to plain env vars
(Settings) when VAULT_ADDR isn't configured -- so local dev / DEMO_MODE /
docker-compose continue to work with zero Vault setup.

See docs/vault-setup.md for the mounts/policies this expects:
  - KV v2 mount (default "secret") at path "rootcause" holding static
    secrets: gitlab_token, slack_bot_token, slack_signing_secret.
  - Database secrets engine mount (default "database") with a role
    (default "rootcause-app") that issues short-lived dynamic Postgres
    credentials, used instead of a static DATABASE_URL password when
    configured.

Usage:
    from .secrets import get_vault_client
    vc = get_vault_client()
    token = vc.get_gitlab_token()

Caching: static KV secrets are cached in-process for
`vault_secret_cache_seconds` (default 300s) to avoid a Vault round trip on
every use; dynamic DB credentials are cached until shortly before their
lease expires, then transparently refetched.
"""
import logging
import time
from typing import Optional

from .config import get_settings

logger = logging.getLogger("rootcause.secrets")


class VaultUnavailable(Exception):
    """Raised internally when Vault is configured but unreachable/misconfigured."""


class VaultClient:
    def __init__(self):
        self.settings = get_settings()
        self._hvac_client = None
        self._kv_cache: dict = {}
        self._kv_cache_at: float = 0.0
        self._db_creds_cache: Optional[dict] = None
        self._db_creds_expiry: float = 0.0

        self.enabled = bool(self.settings.vault_addr)
        if not self.enabled:
            logger.warning(
                "VAULT_ADDR is not set -- falling back to plain environment variables for all "
                "secrets (GitLab token, Slack tokens, database password). This is fine for local "
                "dev/DEMO_MODE but MUST NOT be the case in a real production deployment; see "
                "docs/vault-setup.md."
            )

    # -- client bootstrap ---------------------------------------------------

    def _client(self):
        if self._hvac_client is not None:
            return self._hvac_client
        import hvac  # local import: optional dependency, only needed if Vault is configured

        client = hvac.Client(url=self.settings.vault_addr, token=self.settings.vault_token or None)

        if not self.settings.vault_token and self.settings.vault_role_id and self.settings.vault_secret_id:
            # AppRole auth (preferred for services over a long-lived static token).
            resp = client.auth.approle.login(
                role_id=self.settings.vault_role_id,
                secret_id=self.settings.vault_secret_id,
            )
            client.token = resp["auth"]["client_token"]

        if not client.is_authenticated():
            raise VaultUnavailable("Vault client failed authentication")

        self._hvac_client = client
        return client

    # -- static KV v2 secrets -------------------------------------------------

    def _get_kv(self) -> dict:
        now = time.monotonic()
        if self._kv_cache and (now - self._kv_cache_at) < self.settings.vault_secret_cache_seconds:
            return self._kv_cache
        try:
            client = self._client()
            resp = client.secrets.kv.v2.read_secret_version(
                mount_point=self.settings.vault_kv_mount,
                path=self.settings.vault_kv_path,
            )
            data = resp["data"]["data"]
            self._kv_cache = data
            self._kv_cache_at = now
            return data
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to read secrets from Vault KV mount=%s path=%s: %s",
                         self.settings.vault_kv_mount, self.settings.vault_kv_path, exc)
            raise VaultUnavailable(str(exc)) from exc

    def get_gitlab_token(self) -> str:
        if not self.enabled:
            return self.settings.gitlab_token
        try:
            return self._get_kv().get("gitlab_token", self.settings.gitlab_token)
        except VaultUnavailable:
            logger.warning("Vault unreachable; falling back to env var GITLAB_TOKEN")
            return self.settings.gitlab_token

    def get_slack_bot_token(self) -> str:
        if not self.enabled:
            return self.settings.slack_bot_token
        try:
            return self._get_kv().get("slack_bot_token", self.settings.slack_bot_token)
        except VaultUnavailable:
            logger.warning("Vault unreachable; falling back to env var SLACK_BOT_TOKEN")
            return self.settings.slack_bot_token

    def get_slack_signing_secret(self) -> str:
        if not self.enabled:
            return self.settings.slack_signing_secret
        try:
            return self._get_kv().get("slack_signing_secret", self.settings.slack_signing_secret)
        except VaultUnavailable:
            logger.warning("Vault unreachable; falling back to env var SLACK_SIGNING_SECRET")
            return self.settings.slack_signing_secret

    # -- dynamic database credentials ----------------------------------------

    def get_database_url(self) -> str:
        """Returns a DATABASE_URL. If Vault's database secrets engine is
        configured, mints short-lived dynamic Postgres credentials (cached
        until shortly before lease expiry); otherwise returns the static
        `database_url` from Settings unchanged."""
        if not self.enabled or not self.settings.vault_database_role:
            return self.settings.database_url

        now = time.monotonic()
        if self._db_creds_cache and now < self._db_creds_expiry:
            return self._build_database_url(self._db_creds_cache)

        try:
            client = self._client()
            resp = client.secrets.database.generate_credentials(
                name=self.settings.vault_database_role,
                mount_point=self.settings.vault_database_mount,
            )
            creds = resp["data"]
            lease_duration = resp.get("lease_duration", 3600)
            # Refresh a bit before actual expiry to avoid using a credential
            # right as it's revoked.
            self._db_creds_cache = creds
            self._db_creds_expiry = now + max(lease_duration - 30, 30)
            return self._build_database_url(creds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vault dynamic DB credential generation failed (%s); falling back to static DATABASE_URL", exc)
            return self.settings.database_url

    def _build_database_url(self, creds: dict) -> str:
        # Vault's database secrets engine returns {"username": ..., "password": ...};
        # the host/port/db name come from the static DATABASE_URL template.
        base = self.settings.database_url
        try:
            from sqlalchemy.engine import make_url
            url = make_url(base)
            url = url.set(username=creds["username"], password=creds["password"])
            return str(url)
        except Exception:
            logger.warning("Could not merge Vault dynamic credentials into DATABASE_URL template; using static URL")
            return base


_vault_client: Optional[VaultClient] = None


def get_vault_client() -> VaultClient:
    global _vault_client
    if _vault_client is None:
        _vault_client = VaultClient()
    return _vault_client
