# katabump-autorenew

Automates the free-plan renewal on [KataBump](https://katabump.com) with a headless browser.

## How it works

`src/renew.py` drives Chromium through Playwright:

1. Opens the billing login page and waits for the Cloudflare Turnstile challenge to solve itself
2. Fills the credentials and submits
3. Looks for a renewal control on the dashboard and clicks it
4. Saves screenshots + a JSON result, uploaded as a workflow artifact

The workflow runs daily at 06:23 UTC and can also be triggered manually.

## Setup

Add two repository secrets under **Settings → Secrets and variables → Actions**:

| Name | Value |
| --- | --- |
| `KB_EMAIL` | your KataBump account email |
| `KB_PASSWORD` | your KataBump password |

Then run the workflow once via **Actions → KataBump auto renew → Run workflow** and inspect the artifacts.

## Important caveats

- **Turnstile may still block the login.** If the server answers with `?error=captcha` the run fails and the script records `blocked: true`. This is a deliberate anti-automation control; no bypass is attempted.
- **Sessions are short-lived and IP-bound.** Cookie replay does not work from a different network.
- **The renewal window is narrow.** Free servers can only be renewed within the 2 days before expiry, so most runs will find no button — that is expected.
- **On failure a GitHub issue is opened**, so a broken login is never silent.

## Local usage

```bash
pip install playwright
python -m playwright install chromium
KB_EMAIL=you@example.com KB_PASSWORD=secret KB_HEADLESS=0 python src/renew.py
```
