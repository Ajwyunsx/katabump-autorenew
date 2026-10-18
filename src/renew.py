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


def turnstile_token(page) -> str:
    """Read the Turnstile response token from the page."""
    try:
        return page.evaluate(
            """() => {
                const els = document.querySelectorAll('input[name="cf-turnstile-response"]');
                for (const el of els) {
                    if (el.value && el.value.length > 10) return el.value;
                }
                const w = document.querySelector('.cf-turnstile');
                if (w && w.getAttribute('data-response')) return w.getAttribute('data-response');
                return '';
            }"""
        )
    except Exception:  # noqa: BLE001
        return ""


def wait_turnstile(page, label: str, budget: int = 120) -> str:
    """Wait for the Turnstile widget to produce a token."""
    deadline = time.time() + budget
    clicked = False
    while time.time() < deadline:
        tok = turnstile_token(page)
        if tok:
            log(f"{label}: turnstile token ready (len={len(tok)})")
            return tok
        try:
            body = page.inner_text("body", timeout=3000)
        except Exception:  # noqa: BLE001
            body = ""
        if "Verifying" not in body and "Success" in body:
            time.sleep(2)
            continue
        if not clicked and "Verifying" not in body:
            # widget idle -> nudge the checkbox once
            for frame in page.frames:
                if "challenges.cloudflare.com" not in (frame.url or ""):
                    continue
                try:
                    box = frame.locator("input[type=checkbox]").first
                    if box.count() > 0:
                        box.click(timeout=3000)
                        clicked = True
                        log(f"{label}: clicked turnstile checkbox")
                        break
                except Exception:  # noqa: BLE001
                    continue
        time.sleep(2)
    log(f"{label}: no turnstile token after {budget}s")
    return ""


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
            wait_turnstile(page, "login-page", budget=90)
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

            wait_turnstile(page, "before-submit", budget=120)

            tok = turnstile_token(page)
            if not tok:
                log("ABORT: turnstile never produced a token; not submitting")
                shot(page, "02-no-token")
                result["details"].append("turnstile token missing")
                result["blocked"] = True
                return 3

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
