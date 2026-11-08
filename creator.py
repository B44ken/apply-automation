#!/usr/bin/env python3
"""
Create candidate accounts on Workday (myworkdayjobs.com) and Dayforce Identity
portals using Playwright.

The password is NEVER written to disk: pass --password (discouraged), set the
WORKDAY_PASSWORD / DAYFORCE_PASSWORD environment variable, or you will be
prompted securely via getpass.

Examples:
    python3 creator.py --portal workday --apply-url "https://xyz.wd3.myworkdayjobs.com/.../apply/applyManually" --email you@example.com
    python3 creator.py --portal dayforce --email you@example.com --first-name First --last-name Last

CAPTCHAs: if a challenge appears, the script pauses and asks you to solve it in
the browser window, then press Enter to continue.

Email verification: by default you are prompted to paste the code from your
inbox. With --gmail-poll (only works where the `hatch_gws_cli` Gmail tool is
installed), the script polls your Gmail for the verification email instead.
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# Selectors (verified against live portals 2026-09-28; Workday's are stable
# across tenants since it is the same product).
# --------------------------------------------------------------------------- #

WORKDAY = {
    # Tab/button that opens the create-account form.
    "create_account_tab": [
        '[data-automation-id="createAccountTab"]',
        'button:has-text("Create Account")',
    ],
    "email": ['[data-automation-id="email"]', 'input[type="email"]'],
    "password": ['[data-automation-id="password"]', 'input[type="password"]'],
    # Workday labels the confirmation field "verifyPassword".
    "verify_password": [
        '[data-automation-id="verifyPassword"]',
        'input[type="password"]',
    ],
    # Privacy/data-consent checkbox on the create-account step.
    "privacy_checkbox": [
        '[data-automation-id="privacyConsentCheckbox"]',
        'input[type="checkbox"]',
    ],
    "submit": [
        # The visible click target is a div; the native button is aria-hidden.
        'div[data-automation-id="click_filter"][aria-label="Create Account"]',
        '[role="button"][aria-label="Create Account"]',
        '[data-automation-id="createAccountSubmitButton"]',
    ],
    # Post-submit: email verification code field.
    "verification_code": [
        '[data-automation-id="verificationCode"]',
        '[data-automation-id="verifyCode"]',
        'input[name*="erification" i]',
    ],
    "verify_submit": [
        '[data-automation-id="verifyButton"]',
        'button:has-text("Verify")',
    ],
    "already_exists_markers": [
        "already exists",
        "already registered",
        "account exists",
    ],
}

DAYFORCE = {
    # The register page is reachable directly (verified 2026-09-28).
    "register_url": "https://dfid.dayforcehcm.com/globalidentity/account/register",
    "register_link": ['a:has-text("Create one now")', 'a:has-text("Create an account")'],
    "first_name": ['#firstName', '[aria-label="First Name"]'],
    "last_name": ['#lastName', '[aria-label="Last Name"]'],
    "email": ['#emailAddress', '[aria-label^="Email Address"]'],
    "confirm_email": ['#confirmEmailAddress', '[aria-label="Confirm Email Address"]'],
    "password": ['#password', '[aria-label="Password"]'],
    "confirm_password": ['#confirmPassword', '[aria-label="Confirm Password"]'],
    # Custom element, NOT a native checkbox. Its label attests the user
    # reviewed the privacy statement and terms, so we never tick it for
    # him — the script pauses and asks the operator to do it.
    "consent_checkbox": ['evr-checkbox', '[arialabelcheckbox]'],
    "submit": ['evr-button[label="Next"]', 'button:has-text("Next")'],
}

DAYFORCE_LOGIN_URL = "https://dfid.dayforcehcm.com/globalidentity/account/login"

ICIMS = {
    # iCIMS candidate portals (e.g. careers-garmin.icims.com).
    # Email-step selectors verified against the live Garmin portal 2026-09-28.
    # iCIMS renders its forms inside #icims_content_iframe (see icims_scope).
    # Profile-page fields are filled by accessible label first — iCIMS's
    # labels ("Login", "Password", "Password (Re-enter)") are standard.
    "apply_button": [
        'a.iCIMS_ApplyButton',
        'a:has-text("Apply for this job")',
        'a:has-text("Apply Now")',
    ],
    "email": ['input#email', 'input[name="css_loginName"]', 'input[type="email"]'],
    "continue": [
        'input#enterEmailSubmitButton',
        '.iCIMS_PrimaryButton',
        'input[type="submit"]',
    ],
    "login": ['input#login', 'input[name="login"]', 'input[name*="login" i]'],
    "password": ['input#password', 'input[name="password"]'],
    "password_confirm": ['input#password2', 'input[name="password2"]',
                         'input[name*="confirm" i]'],
    "first_name": ['input#firstName', 'input[name="firstName"]'],
    "last_name": ['input#lastName', 'input[name="lastName"]'],
    "submit": [
        'input[type="submit"].iCIMS_PrimaryButton',
        '.iCIMS_PrimaryButton',
        'input[type="submit"]',
        'button:has-text("Submit")',
    ],
}


def icims_scope(page):
    """Return the iCIMS content iframe when present, else the page itself."""
    try:
        if page.locator("#icims_content_iframe").count() > 0:
            return page.frame_locator("#icims_content_iframe")
    except Exception:
        pass
    return page


def fill_labeled(scope, label, value, fallbacks, label_exact=True):
    """Fill a field by its accessible label, falling back to selectors."""
    try:
        loc = scope.get_by_label(label, exact=label_exact)
        if loc.count() > 0 and loc.first.is_visible():
            loc.first.fill(value)
            return True
    except Exception:
        pass
    sel = first_visible(scope, fallbacks, timeout=3000)
    if sel:
        scope.locator(sel).first.fill(value)
        return True
    return False

CAPTCHA_IFRAME_SELECTORS = [
    'iframe[src*="hcaptcha.com"]',
    'iframe[src*="recaptcha"]',
    'iframe[title*="captcha" i]',
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def log(msg):
    print(f"[creator] {msg}", flush=True)


def first_visible(page, selectors, timeout=8000):
    """Return the first selector that matches a visible element, else None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return sel
        except PWTimeout:
            continue
    return None


def fill_first(page, selectors, value, label, secret=False):
    sel = first_visible(page, selectors, timeout=12000)
    if not sel:
        raise RuntimeError(f"Could not find the {label} field on the page.")
    page.locator(sel).first.fill(value)
    log(f"Filled {label} ({sel})" if not secret else f"Filled {label}.")
    return sel


def maybe_solve_captcha(page):
    """Pause for manual CAPTCHA solving when a challenge iframe is present."""
    for sel in CAPTCHA_IFRAME_SELECTORS:
        try:
            if page.locator(sel).first.is_visible(timeout=3000):
                log("A CAPTCHA challenge is showing in the browser window.")
                input(
                    "[creator] Solve the CAPTCHA in the browser window, "
                    "then press Enter here to continue... "
                )
                return True
        except PWTimeout:
            continue
    return False


def text_shows(page, markers):
    body = page.locator("body").inner_text().lower()
    return [m for m in markers if m.lower() in body]


def gmail_poll_code(query, sender_hint, timeout_s=300, poll_s=15):
    """Poll Gmail via hatch_gws_cli for a verification email; return the code.

    Returns None if the CLI is unavailable or no code is found in time.
    """
    log(f"Polling Gmail for the verification email ({query}) ...")
    deadline = time.time() + timeout_s
    code_re = re.compile(r"\b(\d{6})\b")
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ["hatch_gws_cli", "gmail", "+triage", "--query", query,
                 "--max", "10", "--format", "json"],
                capture_output=True, text=True, timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            log("hatch_gws_cli not available; falling back to manual entry.")
            return None
        if out.returncode != 0:
            log("Gmail search failed; falling back to manual entry.")
            return None
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            time.sleep(poll_s)
            continue
        msgs = data if isinstance(data, list) else data.get("messages", data.get("results", []))
        for m in msgs if isinstance(msgs, list) else []:
            blob = json.dumps(m)
            if sender_hint.lower() not in blob.lower():
                continue
            mid = m.get("id") or m.get("message_id")
            if not mid:
                continue
            rd = subprocess.run(
                ["hatch_gws_cli", "gmail", "+read", "--id", mid, "--format", "json"],
                capture_output=True, text=True, timeout=60,
            )
            if rd.returncode == 0:
                match = code_re.search(rd.stdout)
                if match:
                    log("Found the verification code in your email.")
                    return match.group(1)
        time.sleep(poll_s)
    log("Timed out waiting for the verification email.")
    return None


def gmail_poll_link(query, sender_hint, link_hint, timeout_s=300, poll_s=15):
    """Poll Gmail for a verification email; return the first URL containing link_hint.

    Returns None if the CLI is unavailable or no link is found in time.
    """
    log(f"Polling Gmail for the verification email ({query}) ...")
    url_re = re.compile(r"https?://[^\s\"'<>]+")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ["hatch_gws_cli", "gmail", "+triage", "--query", query,
                 "--max", "10", "--format", "json"],
                capture_output=True, text=True, timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            log("hatch_gws_cli not available; falling back to manual entry.")
            return None
        if out.returncode != 0:
            log("Gmail search failed; falling back to manual entry.")
            return None
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            time.sleep(poll_s)
            continue
        msgs = data if isinstance(data, list) else data.get("messages", data.get("results", []))
        for m in msgs if isinstance(msgs, list) else []:
            blob = json.dumps(m)
            if sender_hint.lower() not in blob.lower():
                continue
            mid = m.get("id") or m.get("message_id")
            if not mid:
                continue
            rd = subprocess.run(
                ["hatch_gws_cli", "gmail", "+read", "--id", mid, "--format", "json"],
                capture_output=True, text=True, timeout=60,
            )
            if rd.returncode == 0:
                for url in url_re.findall(rd.stdout):
                    if link_hint.lower() in url.lower():
                        log("Found the verification link in your email.")
                        return url.rstrip(").,;")
        time.sleep(poll_s)
    log("Timed out waiting for the verification email.")
    return None


def generate_password(length=16):
    """Generate a strong random password meeting typical portal requirements."""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in "!@#$%^&*()-_=+" for c in pw)):
            return pw


def get_password(args):
    if args.password:
        return args.password
    if args.generate_password:
        return generate_password()
    env_var = {"workday": "WORKDAY_PASSWORD",
               "dayforce": "DAYFORCE_PASSWORD",
               "icims": "ICIMS_PASSWORD"}[args.portal]
    pw = os.environ.get(env_var)
    if pw:
        return pw
    return getpass.getpass(f"Password for the new {args.portal} account: ")


def launch_browser(p, args):
    proxy = None
    proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if proxy_url:
        u = urlparse(proxy_url)
        proxy = {
            "server": f"{u.scheme}://{u.hostname}:{u.port}",
            "username": u.username,
            "password": u.password,
        }
        log("Using proxy from environment.")
    profile_dir = args.profile_dir or os.path.expanduser("~/.cache/acct-creator-profile")
    os.makedirs(profile_dir, exist_ok=True)
    browser = p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=args.headless,
        proxy=proxy,
        args=["--disable-blink-features=AutomationControlled"],
    )
    return browser


# --------------------------------------------------------------------------- #
# Portal flows
# --------------------------------------------------------------------------- #

def create_workday_account(page, args, password):
    s = WORKDAY
    log(f"Opening {args.apply_url}")
    page.goto(args.apply_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)

    tab = first_visible(page, s["create_account_tab"], timeout=10000)
    if tab:
        page.locator(tab).first.click()
        page.wait_for_timeout(3000)
        log("Opened the Create Account tab.")

    fill_first(page, s["email"], args.email, "email")
    fill_first(page, s["password"], password, "password", secret=True)
    # The confirmation field is the *second* password input on the page.
    pw_inputs = page.locator('input[type="password"]')
    if pw_inputs.count() >= 2:
        pw_inputs.nth(1).fill(password)
        log("Filled password confirmation.")
    else:
        fill_first(page, s["verify_password"], password, "password confirmation", secret=True)

    cb = first_visible(page, s["privacy_checkbox"], timeout=5000)
    if cb:
        page.locator(cb).first.check()
        log("Accepted the privacy consent checkbox.")

    maybe_solve_captcha(page)

    sub = first_visible(page, s["submit"], timeout=10000)
    if not sub:
        raise RuntimeError("Could not find the Create Account submit button.")
    page.locator(sub).first.click()
    log("Submitted the create-account form.")
    page.wait_for_timeout(5000)
    maybe_solve_captcha(page)
    page.wait_for_timeout(3000)

    hits = text_shows(page, s["already_exists_markers"])
    if hits:
        log(f"RESULT: an account for {args.email} already exists ({hits[0]}). "
            "Sign in instead of creating a new one.")
        return "exists"

    code_sel = first_visible(page, s["verification_code"], timeout=15000)
    if not code_sel:
        # Workday usually proceeds straight to the application after account
        # creation (no code step on most tenants). Confirm it looks successful.
        page.wait_for_timeout(3000)
        body = page.locator("body").inner_text().lower()
        if "apply" in page.url and ("application" in body or "personal information" in body):
            log(f"RESULT: Workday account created for {args.email}; application form is showing.")
            return "created"
        page.screenshot(path="workday_after_submit.png")
        log("Saved workday_after_submit.png — please check whether the account was created.")
        log(f"RESULT: Workday account creation for {args.email} finished in an uncertain state.")
        return "unknown"

    log("Email verification step reached.")
    code = None
    if args.gmail_poll:
        code = gmail_poll_code("from:workday newer_than:20m", "workday")
    if not code:
        code = input("[creator] Paste the verification code from your email: ").strip()
    page.locator(code_sel).first.fill(code)
    vsub = first_visible(page, s["verify_submit"], timeout=8000)
    if vsub:
        page.locator(vsub).first.click()
        page.wait_for_timeout(5000)
    log(f"RESULT: Workday account creation flow completed for {args.email}.")
    return "created"


def create_dayforce_account(page, args, password):
    s = DAYFORCE
    log(f"Opening {s['register_url']}")
    page.goto(s["register_url"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)

    if not first_visible(page, s["first_name"], timeout=8000):
        # Fall back to the login page -> "Create one now." click flow.
        log("Direct register page did not load the form; trying via the login page.")
        page.goto(DAYFORCE_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        reg = first_visible(page, s["register_link"], timeout=10000)
        if not reg:
            raise RuntimeError("Could not reach the Dayforce registration form.")
        page.locator(reg).first.click()
        page.wait_for_timeout(4000)
    log(f"Registration page: {page.url}")

    if args.first_name:
        fill_first(page, s["first_name"], args.first_name, "first name")
    if args.last_name:
        fill_first(page, s["last_name"], args.last_name, "last name")
    fill_first(page, s["email"], args.email, "email")
    fill_first(page, s["confirm_email"], args.email, "email confirmation")
    fill_first(page, s["password"], password, "password", secret=True)
    fill_first(page, s["confirm_password"], password, "password confirmation", secret=True)

    # Consent checkbox: its label attests the user reviewed the privacy
    # statement and terms of use. Never tick that for someone else — the
    # operator reviews and ticks it manually.
    cb = first_visible(page, s["consent_checkbox"], timeout=8000)
    if cb:
        log("The form has a privacy/terms consent checkbox, which I will not tick for you.")
        input(
            "[creator] Please review the linked Privacy Statement and Terms of Use, "
            "tick the consent checkbox in the browser window, then press Enter here... "
        )
    else:
        log("WARNING: no consent checkbox found; the form may have changed.")

    maybe_solve_captcha(page)

    sub = first_visible(page, s["submit"], timeout=10000)
    if not sub:
        raise RuntimeError("Could not find the registration submit button.")
    page.locator(sub).first.click()
    log("Submitted the registration form.")
    page.wait_for_timeout(5000)
    maybe_solve_captcha(page)

    log(f"Registration submitted for {args.email}.")
    log("Dayforce usually emails a verification link next — check your inbox.")
    page.screenshot(path="dayforce_after_submit.png")
    return "submitted"


def create_icims_account(page, args, password):
    """Create an iCIMS candidate account: email-first step, verification link,
    then the Candidate Profile 'Create a login' section."""
    log(f"Opening {args.apply_url}")
    page.goto(args.apply_url, wait_until="domcontentloaded", timeout=60000)
    scope = icims_scope(page)

    # The URL may point at the job page; click Apply if we land there first.
    apply_btn = first_visible(scope, ICIMS["apply_button"], timeout=8000)
    if apply_btn:
        scope.locator(apply_btn).first.click()
        log("Clicked Apply.")
        page.wait_for_timeout(3000)
        scope = icims_scope(page)

    email_sel = first_visible(scope, ICIMS["email"], timeout=15000)
    if not email_sel:
        page.screenshot(path="icims_no_email_field.png")
        raise RuntimeError("Could not find the iCIMS email field.")
    scope.locator(email_sel).first.fill(args.email)
    log("Filled the email field.")
    page.screenshot(path="icims_email_step.png")

    maybe_solve_captcha(page)

    cont = first_visible(scope, ICIMS["continue"], timeout=10000)
    if not cont:
        raise RuntimeError("Could not find the iCIMS continue/submit button.")
    scope.locator(cont).first.click()
    log("Submitted the email step; iCIMS should now send a verification link.")
    page.wait_for_timeout(4000)
    maybe_solve_captcha(page)

    # --- email verification link -------------------------------------------
    link = None
    if args.gmail_poll:
        link = gmail_poll_link(args.gmail_query, args.email.split("@")[0],
                               "icims.com", timeout_s=args.gmail_timeout)
    if not link:
        link = input(
            "[creator] iCIMS emailed you a verification link. Paste it here "
            "(or press Enter to skip and finish this step manually)... "
        ).strip() or None
    if link:
        log("Opening the verification link.")
        page.goto(link, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        scope = icims_scope(page)
    else:
        log("No verification link provided; continuing on the current page.")

    # --- Candidate Profile: Create a login ---------------------------------
    if not fill_labeled(scope, "Login", args.email, ICIMS["login"]):
        page.screenshot(path="icims_no_login_section.png")
        raise RuntimeError("Could not find the 'Create a login' section.")
    fill_labeled(scope, "Password", password, ICIMS["password"])
    fill_labeled(scope, "Password (Re-enter)", password, ICIMS["password_confirm"])
    log("Filled the login credentials.")

    if args.first_name:
        fill_labeled(scope, "First Name", args.first_name, ICIMS["first_name"])
    if args.last_name:
        fill_labeled(scope, "Last Name", args.last_name, ICIMS["last_name"])

    page.screenshot(path="icims_before_submit.png")
    log("Review the profile in the browser window; complete any other required")
    log("fields (e.g. 'How did you hear about us?'), then come back here.")
    input("[creator] Press Enter here to submit the profile... ")

    maybe_solve_captcha(page)
    scope = icims_scope(page)
    sub = first_visible(scope, ICIMS["submit"], timeout=10000)
    if not sub:
        raise RuntimeError("Could not find the iCIMS profile submit button.")
    scope.locator(sub).first.click()
    log("Submitted the candidate profile.")
    page.wait_for_timeout(5000)
    maybe_solve_captcha(page)

    page.screenshot(path="icims_after_submit.png")
    log(f"iCIMS account creation flow finished for {args.email}.")
    return "submitted"


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Create Workday / Dayforce / iCIMS candidate accounts.")
    ap.add_argument("--portal", required=True, choices=["workday", "dayforce", "icims"])
    ap.add_argument("--apply-url",
                    help="Apply URL. Required for workday (.../apply/applyManually) "
                         "and icims (the iCIMS job/apply page).")
    ap.add_argument("--email", required=True, help="Email address for the new account.")
    ap.add_argument("--first-name", default="")
    ap.add_argument("--last-name", default="")
    ap.add_argument("--password", default="",
                    help="Account password (prefer the env var or the secure prompt).")
    ap.add_argument("--generate-password", action="store_true",
                    help="Generate a strong random password and print it at the end "
                         "(it is never stored; save it yourself).")
    ap.add_argument("--profile-dir", default="",
                    help="Persistent browser profile dir (default ~/.cache/acct-creator-profile).")
    ap.add_argument("--headless", action="store_true",
                    help="Run headless (default is a visible window so you can solve CAPTCHAs).")
    ap.add_argument("--gmail-poll", action="store_true",
                    help="Poll Gmail for the verification code/link (needs hatch_gws_cli).")
    ap.add_argument("--gmail-query", default="icims newer_than:1d",
                    help="Gmail search query used with --gmail-poll.")
    ap.add_argument("--gmail-timeout", type=int, default=300,
                    help="Seconds to poll Gmail for the verification email.")
    args = ap.parse_args()

    if args.portal in ("workday", "icims") and not args.apply_url:
        ap.error("--apply-url is required for the workday and icims portals.")

    generated = False
    if args.generate_password and not args.password and not os.environ.get(
            {"workday": "WORKDAY_PASSWORD", "dayforce": "DAYFORCE_PASSWORD",
             "icims": "ICIMS_PASSWORD"}[args.portal]):
        generated = True
    password = get_password(args)
    if len(password) < 8:
        log("WARNING: most portals require at least 8 characters.")

    with sync_playwright() as p:
        ctx = launch_browser(p, args)
        page = ctx.new_page()
        try:
            if args.portal == "workday":
                result = create_workday_account(page, args, password)
            elif args.portal == "dayforce":
                result = create_dayforce_account(page, args, password)
            else:
                result = create_icims_account(page, args, password)
        finally:
            input("[creator] Press Enter to close the browser... ")
            ctx.close()
    log(f"Done: {result}")
    if generated:
        print("\n" + "=" * 60)
        print("SAVE THIS PASSWORD — it is not stored anywhere:")
        print(f"  {password}")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
