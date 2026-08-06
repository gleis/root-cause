# Auth setup: OIDC + API keys

This app never bundles or fakes an identity provider. In production you
register it as a client/application in a real IdP and point the app at that
IdP's issuer/JWKS via env vars. This doc walks through **Keycloak** as the
worked example (free, self-hostable, and structurally representative of
Okta/Azure AD), with notes on the other two at the end.

## App-side configuration

```
OIDC_ISSUER_URL=https://keycloak.example.com/realms/rootcause
OIDC_AUDIENCE=rootcause-api
OIDC_JWKS_URL=                      # blank -> defaults to {issuer}/.well-known/jwks.json
OIDC_ROLES_CLAIM=roles
```

`backend/app/auth.py` verifies the JWT's RS256 signature against the JWKS,
checks `iss`/`aud`, then reads `OIDC_ROLES_CLAIM` (default `roles`) off the
decoded token. The claim value may be a single string or a list; each
value is compared case-insensitively against `viewer`/`engineer`/`admin`,
or a `rootcause-<role>` prefixed form (e.g. `rootcause-admin`) for IdPs
where the same group is shared across multiple apps. If several app roles
are present, the highest-privilege one wins.

## Keycloak setup (worked example)

1. **Create a realm** (or reuse one) -- e.g. `rootcause`.
2. **Create a client**: Clients -> Create client.
   - Client ID: `rootcause-api` (this becomes `OIDC_AUDIENCE`... see the
     audience-mapper note below -- Keycloak does NOT put the client ID in
     `aud` by default).
   - Client authentication: on for a confidential client (server-rendered
     login), off for a public SPA client if the frontend is a pure SPA
     doing the OIDC code flow itself.
   - Standard flow: enabled.
3. **Add an audience mapper** so `OIDC_AUDIENCE` actually appears in the
   token's `aud` claim (Keycloak's default access tokens use `aud: account`
   otherwise):
   - Client -> Client scopes -> `rootcause-api-dedicated` -> Add mapper ->
     By configuration -> Audience.
   - Included Client Audience: `rootcause-api`.
   - Add to access token: on.
4. **Create realm roles**: Realm roles -> Create role -> `rootcause-viewer`,
   `rootcause-engineer`, `rootcause-admin` (or plain `viewer`/`engineer`/
   `admin` if you don't need namespacing against other apps' roles).
5. **Add a roles mapper** so realm roles show up in the claim the app reads
   (`OIDC_ROLES_CLAIM`, default `roles` -- NOT Keycloak's default nested
   `realm_access.roles` path):
   - Client scopes -> `rootcause-api-dedicated` -> Add mapper -> By
     configuration -> User Realm Role.
   - Token Claim Name: `roles`.
   - Add to access token: on.
   - Multivalued: on.
6. **Assign roles to users/groups**: Users -> (user) -> Role mapping ->
   Assign role -> pick one or more of the `rootcause-*` roles. In practice
   you'd assign roles to a **group** (e.g. `sre-team` -> `rootcause-engineer`)
   rather than individual users.
7. **Point the app at it**:
   ```
   OIDC_ISSUER_URL=https://keycloak.example.com/realms/rootcause
   OIDC_AUDIENCE=rootcause-api
   OIDC_ROLES_CLAIM=roles
   ```
8. **Frontend**: the SPA (`frontend/index.html` today is a plain static
   page with no auth -- adding a real OIDC code-flow login is out of scope
   for this backend-focused phase; wire in `oidc-client-ts` or similar and
   send the resulting access token as `Authorization: Bearer <token>` to
   every API call).

## Okta notes

Structurally the same three pieces:
- **Application**: create an OIDC application (Web or SPA depending on your
  frontend), note the Client ID.
- **Authorization Server**: Okta's default `/oauth2/default` (or a custom
  one) issues the JWT; its issuer URI is `OIDC_ISSUER_URL`, its
  `/v1/keys` endpoint is the JWKS.
- **Audience**: set in the Authorization Server's Access Policy /
  the `aud` claim is controlled by the Authorization Server's audience
  setting -- set it to `rootcause-api` (or whatever `OIDC_AUDIENCE` is).
- **Roles claim**: Okta doesn't have a first-class "roles" concept on
  free/dev tiers by default -- use **Groups** + a custom **Claim** on the
  Authorization Server (Security -> API -> Authorization Servers -> your
  server -> Claims -> Add Claim) that maps `user.groups` (filtered to
  `rootcause-.*`) into a token claim named `roles`.

## Azure AD (Entra ID) notes

- **App registration**: register the app, note the Application (client) ID
  -- this becomes (part of) `aud` (Azure AD's `aud` is typically
  `api://<client-id>` or the client ID itself depending on the API's
  "Expose an API" configuration; set `OIDC_AUDIENCE` to match exactly what
  shows up in issued tokens).
- **Issuer**: `https://login.microsoftonline.com/<tenant-id>/v2.0`; JWKS at
  `https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys`.
- **Roles claim**: App registration -> App roles -> define
  `rootcause.viewer` / `rootcause.engineer` / `rootcause.admin` App Roles,
  assign them to users/groups under Enterprise Applications -> Users and
  groups. Azure AD emits assigned app roles in the `roles` claim natively
  (matches `OIDC_ROLES_CLAIM=roles` default) -- no mapper needed, unlike
  Keycloak/Okta.

## API keys (service-to-service)

Not part of OIDC at all -- see `backend/app/auth.py` (`verify_api_key`) and
`backend/scripts/create_api_key.py`. Mint one per machine caller
(Alertmanager, a CI pipeline, a script) with the minimum role it needs:

```bash
cd backend
python scripts/create_api_key.py --role engineer --description "Alertmanager webhook"
```

This prints the plaintext key once. Configure the caller to send it as
either `Authorization: Bearer <key>` or the `X-API-Key` header (see
`API_KEY_HEADER` in `.env.example`). Revoke a compromised key via
`POST /api/v1/api-keys/{id}/revoke` (admin-only) -- there is no un-revoke;
mint a new one.

## What's NOT been tested here

No real Keycloak/Okta/Azure AD instance was actually stood up and exercised
end-to-end as part of this work -- `backend/tests/test_auth.py` verifies
the JWT-verification code path against a locally-generated RSA keypair and
JWKS document (i.e. it proves the *verification logic* is correct: signature
checking, issuer/audience checking, role-claim mapping, rejection of
expired/forged/wrong-audience tokens), not that a specific IdP's actual
token shape matches what's documented above. Before going live, mint one
real token from your chosen IdP and confirm `roles`/`aud`/`iss` come through
exactly as configured.
