#!/usr/bin/env python3
"""
Scripted Workday internship application driver (no browser-agent harness,
no approval cards). Creates or reuses a tenant candidate account, applies
with Autofill-with-Resume, walks the application steps under First's
standing rules, submits, and prints a JSON result line.

Passwords for created accounts are stored in a local vault file (0600)
keyed by tenant host so later runs sign in directly instead of doing
forgot-password email detours. Passwords are never printed.

Usage:
    xvfb-run -a .venv/bin/python apply_workday.py \
        --url "https://tenant.wd5.myworkdayjobs.com/.../job/..." \
        [--country CA|US] [--term winter|summer]

Exit codes: 0 submitted, 2 posting unavailable, 3 blocked (see JSON line),
4 needs-human (unhandled required question/field), 5 error.
"""
import argparse
import json
import os
import re
import secrets
import string
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(BASE, "shots")
os.makedirs(SHOTS, exist_ok=True)


def log(msg):
    print(f"[apply] {msg}", flush=True)


def ensure_relay():
    """Chromium cannot traverse the VM egress proxy directly (tunnel
    failures); a local relay on 127.0.0.1:18080 injects the proxy auth.
    Start it if it is not already listening."""
    import socket
    import subprocess
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", 18080))
        s.close()
        return
    except Exception:
        pass
    subprocess.Popen(
        [sys.executable, os.path.join(BASE, "relay.py")],
        stdout=open("/tmp/relay.log", "ab"), stderr=subprocess.STDOUT,
        start_new_session=True)
    time.sleep(2)
    log("relay started")


# --------------------------------------------------------------------------- #
# Vault (tenant passwords, local only)
# --------------------------------------------------------------------------- #
def vault_load(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def vault_save(path, vault):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(vault, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def gen_password(length=20):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in "!@#$%^&*" for c in pw)):
            return pw


# --------------------------------------------------------------------------- #
# Small page helpers
# --------------------------------------------------------------------------- #
def shot(page, name):
    try:
        page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))
    except Exception:
        pass


def body_text(page):
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def first_visible(page, selectors, timeout=6000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout // max(1, len(selectors))):
                return loc
        except Exception:
            continue
    return None


def click_first(page, selectors, label, timeout=8000):
    loc = first_visible(page, selectors, timeout)
    if loc is None:
        raise RuntimeError(f"click target not found: {label}")
    loc.click()
    log(f"clicked: {label}")
    page.wait_for_timeout(1200)
    return loc


def fill_first(page, selectors, value, label, timeout=8000):
    loc = first_visible(page, selectors, timeout)
    if loc is None:
        raise RuntimeError(f"field not found: {label}")
    loc.click()
    loc.fill(value)
    log(f"filled: {label}")
    return loc


NEXT_BTN = ['[data-automation-id="pageFooterNextButton"]',
            '[data-automation-id="bottom-navigation-next-button"]',
            'button:has-text("Save and Continue")',
            'button:has-text("Continue")', 'button:has-text("Next")']


def click_next(page):
    click_first(page, NEXT_BTN, "next/continue", timeout=10000)
    page.wait_for_timeout(1500)


def wait_ready(page, timeout_s=100):
    """Wait until the Workday SPA has rendered real content and its
    loading spinner is gone (the relay path is slow)."""
    deadline = time.time() + timeout_s
    stable = 0
    while time.time() < deadline:
        try:
            loading = page.locator(
                '[data-automation-id="loadingAnimation"], '
                '.wd-loading-animation').first.is_visible(timeout=400)
        except Exception:
            loading = False
        txt = body_text(page)
        if len(txt) > 300 and not loading:
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        page.wait_for_timeout(2500)
    return False


def dismiss_cookies(page):
    btn = first_visible(page, ['button:has-text("Accept Cookies")',
                               'button:has-text("Accept")'], 2500)
    if btn is not None:
        try:
            btn.click()
            log("cookie banner accepted")
            page.wait_for_timeout(800)
        except Exception:
            pass


def page_errors(page):
    out = []
    try:
        errs = page.locator('[data-automation-id="errorMessage"], '
                            '.WD-Form_Error, [role="alert"]').all_inner_texts()
        out += [e.strip() for e in errs if e.strip()]
    except Exception:
        pass
    try:
        req = page.locator(
            'text=/The field .* is required/').all_inner_texts()
        out += [e.strip() for e in req if e.strip()]
    except Exception:
        pass
    # Dedupe, keep order; drop success toasts caught by the alert role.
    seen = set()
    return [e for e in out
            if "successfully" not in e.lower()
            and not (e in seen or seen.add(e))]


# --------------------------------------------------------------------------- #
# Dropdowns (Workday prompt/select widgets)
# --------------------------------------------------------------------------- #
def open_prompt(page, container):
    """Click a Workday prompt button inside container; return options locator."""
    btn = container.locator(
        '[data-automation-id="promptContainer"] button, '
        '[data-automation-id="selectShowAll"], button[aria-haspopup="listbox"], '
        '[data-automation-id="promptContainer"]').first
    btn.click()
    page.wait_for_timeout(700)


def choose_option(page, texts):
    """Click the first visible list option whose text matches any of texts."""
    for t in texts:
        try:
            opt = page.locator(
                '[data-automation-id="promptOption"], [role="option"], '
                '[data-automation-id="menuItem"]', has_text=t).first
            if opt.is_visible(timeout=1200):
                opt.click()
                page.wait_for_timeout(500)
                return t
        except Exception:
            continue
    return None


def set_dropdown_by_label(page, label_substr, choices):
    """Find a form row whose label contains label_substr; choose from choices."""
    row = page.locator(
        '[data-automation-id="formRow"], li, div',
        has_text=label_substr).first
    try:
        if not row.is_visible(timeout=2500):
            return False
    except Exception:
        return False
    open_prompt(page, row)
    picked = choose_option(page, choices)
    log(f"dropdown '{label_substr}': {picked}")
    return picked is not None


DECLINE_CHOICES = ["I do not wish to answer", "I do not wish to self-identify",
                   "I do not want to answer", "Decline to self-identify",
                   "Decline", "Prefer not to say", "Prefer not to disclose",
                   "Choose not to disclose", "Not disclosed", "Not Specified",
                   "Undeclared"]


# --------------------------------------------------------------------------- #
# Account: create or sign in
# --------------------------------------------------------------------------- #
def handle_auth_step(page, host, profile, vault, vault_path):
    """Handle a combined Create Account / Sign In step inside the apply
    flow. Returns 'handled', 'not_auth', or a blocked-reason string."""
    email_in = first_visible(
        page, ['[data-automation-id="email"]', 'input[type="email"]'], 2500)
    pw_in = first_visible(
        page, ['[data-automation-id="password"]', 'input[type="password"]'],
        2500)
    if email_in is None or pw_in is None:
        return "not_auth"
    email = profile["email"]
    entry = vault.get(host)
    # Structural detection ONLY: a verify-password field means this is a
    # create-account form; email+password without it is a sign-in form.
    # (Page headings/nav are polluted by "Create an Account" links and
    # caused a create/sign-in thrash loop on Ciena, 2026-11-08.)
    verify_in = first_visible(page, ['[data-automation-id="verifyPassword"]'],
                              1500)
    if verify_in is not None:
        # ---- create account ----
        if entry:
            # Account already exists in vault: switch to the Sign In tab.
            tab = first_visible(
                page, ['[data-automation-id="signInTab"]',
                       'button:has-text("Sign In")'], 2500)
            if tab is not None:
                tab.click()
                page.wait_for_timeout(1500)
                return handle_auth_step(page, host, profile, vault,
                                        vault_path)
        pw = gen_password()
        try:
            email_in.click(); email_in.fill(email); log("filled: create email")
            pw_in.click(); pw_in.fill(pw); log("filled: create password")
            vf = first_visible(page, ['[data-automation-id="verifyPassword"]'],
                               2000)
            if vf is not None:
                vf.click(); vf.fill(pw); log("filled: verify password")
            cb = first_visible(
                page, ['[data-automation-id="privacyConsentCheckbox"]',
                       'input[type="checkbox"]'], 2000)
            if cb is not None and not cb.is_checked():
                cb.check(); log("checked privacy consent")
            click_first(
                page, ['div[data-automation-id="click_filter"][aria-label="Create Account"]',
                       '[data-automation-id="createAccountSubmitButton"]',
                       '[data-automation-id="bottom-navigation-next-button"]'],
                "create account", timeout=10000)
        except Exception as e:
            return f"account_create_failed: {e}"
        # Verify the step actually advanced BEFORE trusting/saving the
        # password: poll until the password field disappears.
        advanced = False
        for _ in range(20):
            page.wait_for_timeout(3000)
            if first_visible(page, ['[data-automation-id="password"]',
                                    'input[type="password"]'], 1500) is None:
                advanced = True
                break
            txt_now = body_text(page).lower()
            if "already" in txt_now and "exist" in txt_now:
                return "account_exists_password_unknown"
            if "verify your email" in txt_now or \
                    "verify your account" in txt_now:
                return "email_verification_required"
        if not advanced:
            errs = page_errors(page)
            return f"account_create_no_advance: {errs}"
        vault[host] = {"email": email, "password": pw,
                       "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        vault_save(vault_path, vault)
        log("account created (advance verified); password saved to vault")
        return "handled"
    # ---- sign in ----
    if not entry:
        # Switch to the create-account form via the page's link, if any.
        link = first_visible(page, ['a:has-text("Create Account")'], 2500)
        if link is not None:
            link.click()
            page.wait_for_timeout(2500)
            wait_ready(page)
            return handle_auth_step(page, host, profile, vault, vault_path)
        return "account_required_no_vault_password"
    try:
        email_in.click(); email_in.fill(email); log("filled: sign-in email")
        pw_in.click(); pw_in.fill(entry["password"])
        log("filled: sign-in password")
        click_first(page, ['[data-automation-id="signInButton"]',
                           'button:has-text("Sign In")',
                           '[data-automation-id="bottom-navigation-next-button"]'],
                    "sign in", timeout=10000)
    except Exception as e:
        return f"sign_in_failed: {e}"
    for _ in range(16):
        page.wait_for_timeout(3000)
        if first_visible(page, ['[data-automation-id="password"]',
                                'input[type="password"]'], 1500) is None:
            log("signed in with vault password")
            return "handled"
    return "vault_password_rejected"


# --------------------------------------------------------------------------- #
# Step fillers
# --------------------------------------------------------------------------- #
def strip_illegal_quotes(page):
    """Workday rejects '"' characters in textareas (learned 2026-11-07)."""
    try:
        tas = page.locator("textarea")
        for i in range(tas.count()):
            ta = tas.nth(i)
            try:
                v = ta.input_value()
                if '"' in v:
                    ta.fill(v.replace('"', "'"))
            except Exception:
                continue
    except Exception:
        pass


def answer_questions(page, profile, country, term, unhandled):
    """Rules engine over visible question rows on the current step."""
    txt = body_text(page)
    low = txt.lower()
    if "arbitration" in low and ("agree" in low or "accept" in low):
        raise RuntimeError("arbitration_agreement_present")
    rows = field_containers(page)
    n = len(rows)
    for i in range(n):
        row = rows[i]
        label = container_label(row)
        if not label:
            try:
                label = row.inner_text(timeout=800).split("\n")[0].strip()
            except Exception:
                continue
        ll = label.lower()
        if not ll:
            continue
        # File-upload fields are document uploads, not questions.
        try:
            if row.locator('input[type="file"]').count() > 0:
                continue
        except Exception:
            pass
        # Pure instruction/prose blocks have no fillable control; skip.
        try:
            controls = row.locator(
                'input, textarea, select, button[aria-haspopup], '
                '[role="radio"], [role="checkbox"]').count()
            if controls == 0:
                continue
        except Exception:
            pass
        # Experience/education entry sub-fields are fill_experience's
        # job (the resume parse populates them); never questions.
        EXPERIENCE_FIELDS = ["job title", "company", "school or university",
                             "degree", "field of study", "from", "to",
                             "i currently work", "description",
                             "responsibilities", "location"]
        if any(ll.rstrip("*").strip().startswith(k)
               for k in EXPERIENCE_FIELDS):
            continue
        # My Information / disclosure fields are other fillers' jobs;
        # never report them as unhandled questions.
        NONQUESTION = ["first name", "last name", "address", "city",
                       "postal", "zip", "phone", "country", "province",
                       "state", "how did you hear", "email",
                       "preferred name", "gender", "race", "ethnic",
                       "veteran", "disability", "sex", "orientation",
                       "hispanic", "latino"]
        if len(ll.rstrip("?").split()) <= 7 and \
                any(ll.rstrip("?").startswith(k) for k in NONQUESTION):
            continue
        # text inputs
        tin = row.locator('input[type="text"], input:not([type])').first
        has_text = False
        try:
            has_text = tin.is_visible(timeout=300)
        except Exception:
            pass
        # Sponsorship-locations free text ("indicate all locations or
        # countries for which you require sponsorship ... or N/A"):
        # the label alone does not carry the cue, so check the whole
        # container text.
        try:
            row_text = row.inner_text(timeout=3000).lower()
        except Exception:
            row_text = ""
        if "require sponsorship" in row_text and "locations" in row_text:
            ta = row.locator("textarea").first
            try:
                if ta.count() > 0 and not ta.input_value().strip():
                    ta.fill("United States" if country == "US" else "N/A")
                    log(f"answered sponsorship-locations: {label[:50]}")
                continue
            except Exception:
                pass
        # Export-control citizenship screen ("For any of the countries
        # listed below: citizen / dual citizenship / permanent resident
        # / refugee status?") — his only citizenship is Canada, which is
        # never on these restricted lists; answer No.
        if "listed below" in ll and "dual citizenship" in row_text:
            done = False
            try:
                btn = row.locator('button[aria-haspopup="listbox"]').first
                nbtn = btn.count()
                cur = ""
                if nbtn > 0:
                    cur = btn.inner_text(timeout=600).strip()
                    if cur.lower() == "no":
                        done = True
                    elif cur in ("", "Select One"):
                        picked = set_prompt(row, page, ["No"])
                        log(f"export-control set_prompt: {picked}")
                        done = bool(picked)
                log(f"export-control probe: buttons={nbtn} cur={cur!r} "
                    f"done={done}")
                if not done:
                    # Radio fallback: click the "No" option directly.
                    opt = row.locator('[role="radio"], label',
                                      has_text="No").first
                    if opt.count() > 0 and opt.is_visible(timeout=600):
                        opt.click()
                        log("export-control: clicked No radio/label")
                        done = True
                if not done:
                    with open(os.path.join(BASE, "export_row.html"),
                              "w") as f:
                        f.write(row.evaluate(
                            "el => el.outerHTML")[:6000])
            except Exception as e:
                log(f"export-control probe error: {e}")
            if done:
                continue
        elif "listed below" in ll:
            log(f"export-control MISS: row_text has dual="
                f"{'dual citizenship' in row_text} len={len(row_text)}")
        # yes/no style prompt or radio
        def pick(choices):
            picked = set_prompt(row, page, choices)
            if picked:
                return picked
            # Radio group fallback: click the option with matching text.
            for c in choices:
                try:
                    opt = row.locator('[role="radio"], label',
                                      has_text=c).first
                    if opt.is_visible(timeout=600):
                        opt.click()
                        return c
                except Exception:
                    continue
            return None
        handled = True
        if "relocat" in ll:
            pick(["Yes"])
        elif "sponsor" in ll:
            pick(["Yes"] if country == "US" else ["No"])
        elif "authorized" in ll or "eligible to work" in ll or \
                "legally" in ll and "work" in ll:
            pick(["Yes"] if country == "CA" else ["No"])
        elif "co-op" in ll or "coop" in ll or "co op" in ll:
            pick(["Yes"])
        elif "work permit" in ll or "residency permit" in ll:
            pick(["No"] if country == "US" else ["Yes"])
        elif "government" in ll and ("employment" in ll or "worked" in ll
                                     or "public institution" in ll
                                     or "official" in ll):
            pick(["No"])
        elif any(k in ll for k in ["conflict", "non-compete", "noncompete",
                                   "non-solicit", "relative", "family member",
                                   "previously employed", "worked at",
                                   "worked for", "former employee"]):
            pick(["No"])
        elif "18 years" in ll or "at least 18" in ll:
            pick(["Yes"])
        elif "salary" in ll or "compensation" in ll or "pay expectation" in ll:
            if has_text:
                try:
                    tin.fill(profile["salary_hourly_default"])
                except Exception:
                    handled = False
            else:
                handled = False
        elif "enrolled" in ll or "student" in ll:
            pick(["Yes"])
        elif "consent" in ll or "privacy" in ll or "i agree" in ll:
            cb = row.locator('input[type="checkbox"]').first
            try:
                if not cb.is_checked():
                    cb.check()
            except Exception:
                handled = False
        else:
            handled = False
        if not handled:
            # required?
            try:
                star = row.locator('text="*"').first.is_visible(timeout=300)
            except Exception:
                star = False
            if star:
                try:
                    full = " ".join(
                        row.inner_text(timeout=600).split())[:300]
                except Exception:
                    full = label
                unhandled.append(full)
        else:
            log(f"answered: {label[:60]}")
    strip_illegal_quotes(page)


def fill_disclosures(page, profile):
    txt = body_text(page)
    if "arbitration" in txt.lower() and "agree" in txt.lower():
        raise RuntimeError("arbitration_agreement_present")
    rows = field_containers(page)
    n = len(rows)
    for i in range(n):
        row = rows[i]
        label = container_label(row)
        if not label:
            continue
        ll = label.lower()
        if any(k in ll for k in ["gender", "race", "ethnic", "veteran",
                                 "disability", "sex", "orientation",
                                 "hispanic", "latino"]):
            picked = set_prompt(row, page, DECLINE_CHOICES)
            if picked is None and "sex" in ll:
                # RBC precedent: required Sex field with no decline option.
                picked = set_prompt(row, page, ["Male"])
            if picked is None and ("hispanic" in ll or "latino" in ll):
                # HPE: this field is a strict Yes/No with no decline
                # option. Use the applicant's stated fact from profile.
                hl = profile.get("hispanic_latino")
                if hl in ("Yes", "No"):
                    picked = set_prompt(row, page, [hl])
            log(f"disclosure '{label[:40]}': {picked}")
        cb = row.locator('input[type="checkbox"]').first
        try:
            if cb.is_visible(timeout=300) and not cb.is_checked():
                cb.check()
        except Exception:
            pass


def field_containers(page):
    """Field containers across both Workday UI generations: the new
    candidate UI uses formField-* containers; the classic UI uses
    formRow. Returns a list of locators."""
    out = []
    for sel in ['[data-automation-id^="formField-"]',
                '[data-automation-id="formRow"]']:
        try:
            locs = page.locator(sel)
            for i in range(locs.count()):
                out.append(locs.nth(i))
        except Exception:
            continue
    return out


def container_label(container):
    try:
        return container.locator("label").first.inner_text(
            timeout=800).strip().rstrip("*").strip()
    except Exception:
        return ""


def set_prompt(container, page, choices):
    """Set a Workday prompt/select inside a field container (either UI
    generation). Returns the picked choice or None."""
    try:
        if container.locator(
                '[data-automation-id="selectedItem"]').count() > 0:
            return "already-set"
    except Exception:
        pass
    btn = container.locator('button[aria-haspopup="listbox"]').first
    has_btn = False
    try:
        has_btn = btn.count() > 0
        if has_btn:
            cur = btn.inner_text(timeout=600).strip()
            if cur and cur.lower() not in ("select one", ""):
                if any(cur.lower() == c.lower() for c in choices):
                    return cur
    except Exception:
        has_btn = False
    # Multiselect prompt. Workday source prompts are HIERARCHICAL:
    # top-level entries are categories; clicking one expands leaf
    # options ("Job Board" -> "Job Board - CareerBuilder"). A pick only
    # counts when a selectedItem chip appears in the container.
    try:
        ms_wrap = container.locator(
            '[data-automation-id="multiselectInputContainer"]').first
        if ms_wrap.count() > 0:
            ms_wrap.click()
            page.wait_for_timeout(1500)

            def visible_options():
                opts = page.locator('[data-automation-id="promptOption"]')
                out = []
                for i in range(opts.count()):
                    o = opts.nth(i)
                    try:
                        if o.is_visible(timeout=300):
                            out.append((o, o.inner_text(
                                timeout=400).strip()))
                    except Exception:
                        continue
                return out

            def chipped():
                try:
                    return container.locator(
                        '[data-automation-id="selectedItem"]').count() > 0
                except Exception:
                    return False

            top = visible_options()
            log(f"multiselect top-level: {[t for _, t in top][:12]}")

            def try_option(o, t):
                o.click()
                page.wait_for_timeout(1000)
                if chipped():
                    return t
                # Expanded? Click the first leaf under it.
                for o2, t2 in visible_options():
                    if t2.startswith(t) and t2 != t:
                        o2.click()
                        page.wait_for_timeout(1000)
                        if chipped():
                            return t2
                return None

            for c in choices:
                for o, t in visible_options():
                    if t == c:
                        r = try_option(o, t)
                        if r:
                            return r
                        break
            # Fallback 1: a career-site leaf is the truthful generic
            # pick for "how did you hear" style prompts.
            for o, t in visible_options():
                if "career" in t.lower() or "website" in t.lower():
                    r = try_option(o, t)
                    if r:
                        return r
            # Fallback 2: long lists (e.g. Field of Study) only surface
            # matches through the search box. Type the choice, click the
            # exact match, verify the chip.
            try:
                ms = ms_wrap.locator("input").first
                for c in choices:
                    ms.fill(c)
                    page.wait_for_timeout(1600)
                    for o, t in visible_options():
                        if t == c:
                            o.click()
                            page.wait_for_timeout(900)
                            if chipped():
                                return c
            except Exception:
                pass
            # Fallback 3 (LAST resort, source prompts only): walk the
            # top-level options until one sticks. This is only safe for
            # short "how did you hear" category lists — on a long flat
            # list it would pick whatever comes first alphabetically.
            try:
                aid = (container.get_attribute("data-automation-id")
                       or "").lower()
                lbl = container_label(container).lower()
            except Exception:
                aid, lbl = "", ""
            if "source" in aid or "hear" in lbl:
                tried = {t for _, t in visible_options()}
                for t in list(tried)[:8]:
                    for o, t2 in visible_options():
                        if t2 == t:
                            r = try_option(o, t2)
                            if r:
                                return r
                            break
            return None
    except Exception:
        pass
    # Select-one: open the listbox and click the option.
    try:
        if has_btn:
            try:
                btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            btn.click()
        else:
            open_prompt(page, container)
        page.wait_for_timeout(1200)
        picked = choose_option(page, choices)
        if not picked:
            # The click may have raced a re-render and never opened the
            # list. Reopen once and retry before falling to search.
            try:
                n_vis = 0
                opts = page.locator('[data-automation-id="promptOption"]')
                for i in range(opts.count()):
                    if opts.nth(i).is_visible(timeout=200):
                        n_vis += 1
                if n_vis <= 2:
                    if has_btn:
                        btn.click()
                    else:
                        open_prompt(page, container)
                    page.wait_for_timeout(1200)
                    picked = choose_option(page, choices)
            except Exception:
                pass
        if picked:
            return picked
        search = container.locator('input[type="text"]').first
        if search.count() > 0:
            for c in choices:
                try:
                    search.fill(c)
                except Exception:
                    break
                page.wait_for_timeout(1200)
                picked = choose_option(page, [c])
                if picked:
                    return picked
    except Exception:
        pass
    return None


def fill_my_information(page, profile):
    """Fill My Information basics by field label. Needed whenever the
    resume parse did not populate the step (or autofill was skipped)."""
    text_map = {
        "first name": profile["first_name"],
        "last name": profile["last_name"],
        "address line 1": profile["address"],
        "city": profile["city"],
        "postal code": profile["postal_code"],
        "zip code": profile["postal_code"],
        "phone number": profile["phone"],
        "email": profile["email"],
    }
    prompt_map = {
        "phone device type": ["Mobile"],
        "how did you hear": ["Company Website", "Corporate Website",
                             "Job Board", "Internet", "Other"],
        "country phone code": ["Canada (+1)", "Canada", "+1"],
        "province": [profile["province"]],
        "state": [profile["province"]],
    }
    # Country first: changing it re-renders the region prompt.
    for container in field_containers(page):
        label = container_label(container)
        if label.lower().rstrip("?").strip() == "country":
            picked = set_prompt(container, page, [profile["country"]])
            if picked:
                log(f"my-info prompt 'Country': {picked}")
            page.wait_for_timeout(1500)
            break
    containers = field_containers(page)
    if containers:
        log(f"my-info: {len(containers)} field containers")
    for container in containers:
        label = container_label(container)
        if not label:
            continue
        ll = label.lower().rstrip("?").strip()
        if ll == "country":
            continue
        tkey = next((k for k in text_map if ll.startswith(k)), None)
        pkey = next((k for k in prompt_map if ll.startswith(k)), None)
        if tkey:
            inp = container.locator(
                'input[type="text"], input[type="tel"], '
                'input:not([type])').first
            try:
                if inp.count() > 0 and not inp.input_value().strip():
                    inp.fill(text_map[tkey])
                    log(f"my-info filled: {label}")
            except Exception:
                pass
        elif pkey:
            picked = set_prompt(container, page, prompt_map[pkey])
            if picked:
                log(f"my-info prompt '{label}': {picked}")


def fill_experience(page, profile):
    strip_illegal_quotes(page)
    # Blank company on the first job entry -> example.dev (resume
    # parse quirk: his founder role parses with an empty company).
    # New UI: formField-companyName containers; classic: company inputs.
    try:
        empties = []
        for container in field_containers(page):
            try:
                aid = container.get_attribute("data-automation-id") or ""
            except Exception:
                continue
            if aid.replace("formField-", "") == "companyName":
                inp = container.locator('input[type="text"]').first
                try:
                    if inp.count() > 0 and not inp.input_value().strip():
                        empties.append(inp)
                except Exception:
                    pass
        legacy = page.locator('input[data-automation-id="company"]')
        for i in range(legacy.count()):
            try:
                if not legacy.nth(i).input_value().strip():
                    empties.append(legacy.nth(i))
            except Exception:
                pass
        if len(empties) == 1:
            empties[0].fill("example.dev")
            log("fixed blank company -> example.dev")
        elif len(empties) > 1:
            log(f"WARNING: {len(empties)} blank company fields; "
                "not guessing")
    except Exception:
        pass
    # GPA if a field exists
    gpa = first_visible(page, ['input[data-automation-id="gpa"]',
                               'input[aria-label*="GPA" i]'], 1500)
    if gpa is not None:
        try:
            if not gpa.input_value().strip():
                gpa.fill(profile["gpa"])
        except Exception:
            pass
    # Field of Study: the resume parse often leaves it unselected.
    for container in field_containers(page):
        label = container_label(container)
        if label and label.lower().startswith("field of study"):
            try:
                txt = container.inner_text(timeout=800)
            except Exception:
                txt = ""
            if "0 items selected" in txt:
                picked = set_prompt(container, page,
                                    ["Computer Science"])
                log(f"field of study: {picked}")
    # Education entries: fill whatever the parse left empty, and repair
    # a wrong Field of Study chip. Dispatched by field automation id.
    for container in field_containers(page):
        try:
            aid = container.get_attribute("data-automation-id") or ""
        except Exception:
            continue
        suffix = aid.replace("formField-", "")
        try:
            if suffix == "schoolName":
                inp = container.locator('input[type="text"]').first
                if inp.count() > 0 and not inp.input_value().strip():
                    inp.fill(profile["school"])
                    log("education: school filled")
            elif suffix == "degree":
                btn = container.locator(
                    'button[aria-haspopup="listbox"]').first
                if btn.count() > 0 and btn.inner_text(
                        timeout=500).strip() in ("", "Select One"):
                    picked = set_prompt(
                        container, page,
                        [profile["degree"], "Bachelor's", "Bachelors"])
                    log(f"education: degree {picked}")
            elif suffix == "fieldOfStudy":
                target = profile["field_of_study"]
                chips = container.locator(
                    '[data-automation-id="selectedItem"]')
                texts = []
                for ci in range(chips.count()):
                    try:
                        texts.append(
                            chips.nth(ci).inner_text(timeout=400).strip())
                    except Exception:
                        pass
                if texts and not any(target.lower() in t.lower()
                                      for t in texts):
                    # Wrong chip(s) saved earlier: remove and re-pick.
                    for ci in range(chips.count()):
                        try:
                            rm = chips.nth(ci).locator(
                                'button, [role="button"]').first
                            rm.click()
                            page.wait_for_timeout(500)
                        except Exception:
                            pass
                    picked = set_prompt(container, page, [target])
                    log(f"education: field of study repaired -> {picked}")
            elif suffix == "gradeAverage":
                inp = container.locator('input[type="text"]').first
                if inp.count() > 0 and not inp.input_value().strip():
                    inp.fill(profile["gpa"])
                    log("education: GPA filled")
            elif suffix == "firstYearAttended":
                inp = container.locator('input[type="text"]').first
                if inp.count() > 0 and not inp.input_value().strip():
                    inp.fill(profile["school_start"])
                    log("education: first year filled")
            elif suffix == "lastYearAttended":
                inp = container.locator('input[type="text"]').first
                if inp.count() > 0 and not inp.input_value().strip():
                    inp.fill(profile["graduation_year"])
                    log("education: last year filled")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #
def result(status, **kw):
    out = {"status": status}
    out.update(kw)
    print("RESULT " + json.dumps(out), flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--country", choices=["CA", "US"], default="CA")
    ap.add_argument("--term", choices=["winter", "summer"], default="summer")
    ap.add_argument("--profile", default=os.path.join(BASE, "profile.json"))
    ap.add_argument("--vault", default=os.path.join(BASE, "vault.json"))
    args = ap.parse_args()

    with open(args.profile) as f:
        profile = json.load(f)
    vault = vault_load(args.vault)
    host = urlparse(args.url).hostname or ""

    ensure_relay()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            os.path.join(BASE, ".pwprofile"), headless=False,
            viewport={"width": 1440, "height": 900},
            proxy={"server": "http://127.0.0.1:18080"},
            ignore_https_errors=True,
            args=["--no-sandbox"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
            wait_ready(page)
            dismiss_cookies(page)
            log(f"posting page rendered ({len(body_text(page))} chars)")
            txt = body_text(page)
            if "doesn't exist" in txt or "does not exist" in txt or \
                    "no longer" in txt.lower() and "available" in txt.lower():
                shot(page, "unavailable")
                result("unavailable", url=args.url)
                return 2
            shot(page, "step0_posting")
            # Apply button
            apply_btn = first_visible(
                page, ['[data-automation-id="applyButton"]',
                       'a:has-text("Continue Application")',
                       'button:has-text("Continue Application")',
                       'a:has-text("Apply")', 'button:has-text("Apply")'],
                15000)
            if apply_btn is None:
                shot(page, "no_apply")
                result("blocked", reason="no_apply_button", url=args.url)
                return 3
            apply_btn.click()
            page.wait_for_timeout(3000)
            wait_ready(page)
            dismiss_cookies(page)
            # Some postings show an "Autofill with Resume" choice first.
            af = first_visible(
                page, ['[data-automation-id="autofillWithResume"]',
                       'a:has-text("Autofill with Resume")',
                       'button:has-text("Autofill with Resume")'], 5000)
            if af is not None:
                af.click()
                page.wait_for_timeout(2500)
                wait_ready(page)
            # Step loop. Auth and resume-upload steps are handled inline:
            # on many tenants step 1 IS Create Account/Sign In and step 2
            # is Autofill with Resume.
            unhandled = []
            resume_done = False
            last_fp = None
            stuck = 0
            for step in range(16):
                wait_ready(page)
                dismiss_cookies(page)
                shot(page, f"step{step + 1}")
                txt = body_text(page)
                low = txt.lower()
                auth = handle_auth_step(page, host, profile, vault,
                                        args.vault)
                if auth == "handled":
                    continue
                if auth != "not_auth":
                    shot(page, "auth_blocked")
                    result("blocked", reason=auth, url=args.url)
                    return 3
                if not resume_done:
                    # Workday's autofill file input is usually hidden;
                    # visibility checks miss it. Attach-state is enough.
                    try:
                        fi = page.locator(
                            'input[type="file"]').first
                        has_fi = fi.count() > 0
                    except Exception:
                        has_fi = False
                    if has_fi:
                        fi.set_input_files(profile["resume_path"])
                        resume_done = True
                        log("resume uploaded")
                        # Wait for Workday's resume parse to actually
                        # finish (file chip appears) before advancing;
                        # advancing early leaves later steps unpopulated.
                        fname = os.path.basename(profile["resume_path"])
                        for _ in range(20):
                            page.wait_for_timeout(3000)
                            if fname.split(".")[0] in body_text(page):
                                log("resume parse visible on page")
                                break
                        wait_ready(page)
                        nxt = first_visible(page, NEXT_BTN, 5000)
                        if nxt is not None:
                            nxt.click()
                            page.wait_for_timeout(2500)
                        continue
                if "application submitted" in low or \
                        "successfully submitted" in low or \
                        "we have received your application" in low or \
                        "successfully received your application" in low or \
                        "thank you for applying" in low:
                    result("submitted", url=args.url,
                           confirmation=txt[:300].replace("\n", " "))
                    return 0
                # NOTE: whole-page keyword dispatch is unreliable — the
                # progress bar prints every step name on every page. Run
                # all label-driven fillers each iteration; they no-op
                # when their fields are absent.
                fill_my_information(page, profile)
                fill_experience(page, profile)
                answer_questions(page, profile, args.country,
                                 args.term, unhandled)
                if unhandled:
                    # One retry pass: a click can race a page re-render
                    # and silently miss; only report what stays unhandled.
                    page.wait_for_timeout(2000)
                    retry_unhandled = []
                    answer_questions(page, profile, args.country,
                                     args.term, retry_unhandled)
                    unhandled[:] = retry_unhandled
                fill_disclosures(page, profile)
                if step > 1:
                    submit = first_visible(
                        page, ['[data-automation-id="submit"]',
                               'button:has-text("Submit")'], 2500)
                    if submit is not None:
                        submit.click()
                        page.wait_for_timeout(4000)
                        wait_ready(page)
                        continue
                if unhandled:
                    try:
                        page.evaluate("window.scrollTo(0, 0)")
                        page.wait_for_timeout(600)
                    except Exception:
                        pass
                    shot(page, "unhandled")
                    result("needs_human", unhandled=unhandled, url=args.url)
                    return 4
                errs = page_errors(page)
                nxt = first_visible(page, NEXT_BTN, 5000)
                if nxt is None:
                    shot(page, "no_next")
                    result("blocked", reason="no_next_button",
                           errors=errs, url=args.url)
                    return 3
                nxt.click()
                page.wait_for_timeout(2500)
                wait_ready(page)
                new_errs = page_errors(page)
                if new_errs:
                    log(f"errors after next: {new_errs}")
                    fp = (txt[:80], tuple(sorted(new_errs)))
                    if fp == last_fp:
                        stuck += 1
                    else:
                        stuck = 0
                        last_fp = fp
                    if stuck >= 1:
                        shot(page, "stuck_errors")
                        try:
                            with open(os.path.join(
                                    BASE, "stuck_page.html"), "w") as f:
                                f.write(page.content())
                        except Exception:
                            pass
                        result("blocked", reason="validation_stuck",
                               errors=new_errs, url=args.url)
                        return 3
            shot(page, "loop_exhausted")
            result("blocked", reason="step_loop_exhausted", url=args.url)
            return 3
        except RuntimeError as e:
            shot(page, "runtime_error")
            result("blocked", reason=str(e), url=args.url)
            return 3
        except Exception as e:
            shot(page, "exception")
            result("error", reason=f"{type(e).__name__}: {e}", url=args.url)
            return 5
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
