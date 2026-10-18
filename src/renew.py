#!/usr/bin/env python3
"""KataBump free-plan auto-renew via headless browser."""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LOGIN_URL = "https://dashboard.katabump.com/auth/login"
DASH_URL = "https://dashboard.katabump.com/dashboard"
STATE_DIR = Path("state")
STATE_DIR.mkdir(exist_ok=True)
SHOT_DIR = Path("screenshots")
SHOT_DIR.mkdir(exist_ok=True)

EMAIL = os.environ.get("KB_EMAIL", "")
PASSWORD = os.environ.get("KB_PASSWORD", "")
HEADLESS = os.environ.get("KB_HEADLESS", "1") != "0"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def shot(page, name: str) -> None:
    try:
        page.screenshot(path=str(SHOT_DIR / f"{name}.png"), full_page=True)
        log(f"screenshot saved: {name}.png")
    except Exception as exc:  # noqa: BLE001
        log(f"screenshot failed ({name}): {exc}")


def dismiss_consent(page) -> None:
    for sel in (
        "button:has-text('Accept')",
        "button:has-text('Accepter')",
        "button:has-text('Agree')",
        "button:has-text('Tout accepter')",
        "[aria-label='Accept']",
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=2000)
                log(f"dismissed consent: {sel}")
                return
        except Exception:  # noqa: BLE001
            continue


def wait_cloudflare(page, label: str) -> None:
    """Wait until the Turnstile challenge stops being evaluated."""
    for i in range(30):
        try:
            html = page.content()
        except Exception:  # noqa: BLE001
            time.sleep(2)
            continue
        if "Just a moment" not in html and "cf-challenge" not in html:
            # token present in DOM means Turnstile solved
            token = page.evaluate(
                """() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    return el ? el.value : '';
                }"""
            )
            if token:
                log(f"{label}: turnstile token acquired (len={len(token)})")
            else:
                log(f"{label}: page loaded, no turnstile token field found")
            return
        log(f"{label}: waiting for cloudflare challenge ({i + 1}/30)")
        time.sleep(2)
    log(f"{label}: cloudflare wait finished without confirmation")


def try_turnstile_click(page) -> None:
    """Turnstile in managed mode may need a click on the checkbox."""
    frames = []
    for frame in page.frames:
        if "challenges.cloudflare.com" in (frame.url or ""):
            frames.append(frame)
    for frame in frames:
        for sel in ("input[type=checkbox]", "label", "#challenge-stage", "body"):
            try:
                loc = frame.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.click(timeout=2000)
                log(f"clicked turnstile element: {sel}")
                time.sleep(2)
                return
            except Exception:  # noqa: BLE001
                continue


def is_logged_in(page) -> bool:
    try:
        if "auth/login" in page.url:
            return False
        body = page.inner_text("body", timeout=5000)
        return "Credits" in body or "Dashboard" in body or "Server" in body
    except Exception:  # noqa: BLE001
        return False


def find_renew_button(page):
    """Locate a renewal control on the dashboard."""
    patterns = [
        "a:has-text('Renew')",
        "button:has-text('Renew')",
        "a:has-text('Renouveler')",
        "button:has-text('Renouveler')",
        "a[href*='renew']",
        "button[href*='renew']",
        "a:has-text('Renew now')",
        "a:has-text('Extend')",
    ]
    for sel in patterns:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=1500):
                log(f"renew control found: {sel}")
                return loc.first
        except Exception:  # noqa: BLE001
            continue
    return None


def collect_dates(body: str) -> dict:
    out = {}
    for key in ("Expires on", "Expiration", "Expire le", "Next renewal", "Due date"):
        m = re.search(re.escape(key) + r"[^\n]{0,80}", body, re.IGNORECASE)
        if m:
            out[key] = m.group(0).strip()
    return out


def main() -> int:
    if not EMAIL or not PASSWORD:
        log("ERROR: KB_EMAIL / KB_PASSWORD secrets are not set")
        return 2

    log(f"starting (headless={HEADLESS})")
    result = {"ok": False, "renewed": False, "details": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="Europe/Paris",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.new_page()
        page.set_default_timeout(15000)

        try:
            log(f"navigating to {LOGIN_URL}")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
            dismiss_consent(page)
            wait_cloudflare(page, "login-page")
            shot(page, "01-login")

            # fill credentials
            page.fill("input[name='email']", EMAIL)
            page.fill("input[name='password']", PASSWORD)
            log("credentials filled")

            # remember me helps persist kata_t
            for sel in ("input[name='remember']", "#rememberMe"):
                try:
                    if page.locator(sel).count() > 0:
                        page.check(sel, timeout=2000)
                        log("checked remember-me")
                        break
                except Exception:  # noqa: BLE001
                    continue

            try_turnstile_click(page)
            wait_cloudflare(page, "before-submit")

            log("submitting login form")
            page.click("button[type='submit'], #submit, button:has-text('Log in')")
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            time.sleep(4)

            if "error=captcha" in page.url:
                log("LOGIN BLOCKED: server rejected turnstile token (error=captcha)")
                shot(page, "02-captcha-rejected")
                result["details"].append("captcha rejected by server")
                result["blocked"] = True
            elif is_logged_in(page):
                log("login succeeded")
                result["ok"] = True
                shot(page, "03-dashboard")

                body = page.inner_text("body")
                dates = collect_dates(body)
                if dates:
                    log(f"date info: {dates}")
                    result["details"].append(dates)

                btn = find_renew_button(page)
                if btn is None:
                    log("no renew button visible (may be outside renewal window)")
                    result["details"].append("no renew button in window")
                    shot(page, "04-no-renew-button")
                else:
                    log("clicking renew")
                    btn.click()
                    time.sleep(2)
                    shot(page, "05-renew-clicked")
                    # confirm dialogs
                    for sel in (
                        "button:has-text('Confirm')",
                        "button:has-text('Yes')",
                        "button:has-text('Renew now')",
                        "button:has-text('Confirmer')",
                    ):
                        try:
                            c = page.locator(sel).first
                            if c.is_visible(timeout=2000):
                                c.click()
                                log(f"confirmed via {sel}")
                                time.sleep(3)
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    shot(page, "06-after-confirm")
                    after = page.inner_text("body")
                    ok_marks = ("success", "renewed", "Renouvelé", "extended")
                    result["renewed"] = any(m.lower() in after.lower() for m in ok_marks)
                    log(f"renew result: {result['renewed']}")
                    result["details"].append(
                        "renewed" if result["renewed"] else "renew click sent, unconfirmed"
                    )
            else:
                log(f"login state unclear, url={page.url}")
                shot(page, "07-unknown")
                result["details"].append(f"unclear state at {page.url}")

        except PWTimeout as exc:
            log(f"timeout: {exc}")
            shot(page, "99-timeout")
            result["details"].append(f"timeout: {exc}")
        except Exception as exc:  # noqa: BLE001
            log(f"error: {exc}")
            shot(page, "99-error")
            result["details"].append(f"error: {exc}")
        finally:
            try:
                ctx.storage_state(path=str(STATE_DIR / "storage.json"))
            except Exception:  # noqa: BLE001
                pass
            browser.close()

    (STATE_DIR / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    log(f"done: {json.dumps(result)[:400]}")
    if result.get("blocked"):
        return 3
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
