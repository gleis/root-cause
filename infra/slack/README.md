# Slack app setup

This tool never does OAuth on your behalf -- you create the Slack app in
your own workspace by hand, once, following these steps.

## 1. Create the app from the manifest
1. Go to https://api.slack.com/apps -> **Create New App** -> **From an app
   manifest**.
2. Pick your workspace.
3. Paste the contents of `infra/slack/app-manifest.yaml`, but first edit the
   `slash_commands[0].url` field to point at your backend's public URL
   (see step 3 below) -- you can also edit this later in **Slash Commands**
   under **Features** in the app config UI.
4. Click **Create**.

## 2. Install the app to your workspace
In the app's **OAuth & Permissions** page, click **Install to Workspace**,
approve the `chat:write` / `chat:write.public` / `commands` scopes.

Copy the **Bot User OAuth Token** (`xoxb-...`) -> this is `SLACK_BOT_TOKEN`.

## 3. Expose your backend publicly (for the slash command + for local dev)
Slack slash commands must POST to a public HTTPS URL -- `localhost` won't
work. For local development, use a tunnel:
```bash
ngrok http 8000
```
Take the `https://xxxx.ngrok.io` URL, go back to your app's **Slash
Commands** page, edit `/investigate`, and set the Request URL to:
```
https://xxxx.ngrok.io/api/v1/slack/investigate
```
(If you're running the backend as an in-cluster Deployment behind a real
Ingress with a stable domain, use that domain instead and skip ngrok.)

## 4. Get the signing secret
In **Basic Information** -> **App Credentials**, copy **Signing Secret**
-> this is `SLACK_SIGNING_SECRET`. The backend uses this to verify the
`X-Slack-Signature` / `X-Slack-Request-Timestamp` headers on every request
to `/api/v1/slack/investigate` before trusting the payload (see
`backend/app/integrations/slack_client.py:verify_slack_signature` and
`backend/app/main.py`).

## 5. Set env vars
Add to your `.env` (see `.env.example`):
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_CHANNEL=#incidents
DEMO_MODE=false
```

## 6. Try it
In any channel the bot has been invited to (`/invite @rootcause-bot`):
```
/investigate customer-api
```
Slack requires an ack within 3 seconds -- the endpoint immediately returns
a "investigation started" message, runs the pipeline in the background, and
posts a follow-up `chat.postMessage` with the ranked hypotheses once done.
