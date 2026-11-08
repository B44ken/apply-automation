# Account Creator (Workday + Dayforce + iCIMS)

Creates candidate accounts on **Workday** (`myworkdayjobs.com`), **Dayforce Identity**,
and **iCIMS** (`*.icims.com`) portals with Playwright. Built because password-only
portals were blocking ~90% of pending internship applications.

## Setup

```bash
cd workday-account-creator
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

On a headless server, run under `xvfb-run` so the browser window exists for
manual CAPTCHA solving:

```bash
xvfb-run -a .venv/bin/python creator.py ...
```

## Usage

```bash
# Workday — point it at the posting's "Create Account" step
.venv/bin/python creator.py --portal workday \
  --apply-url "https://xyz.wd3.myworkdayjobs.com/en-US/TENANT/.../apply/applyManually" \
  --email you@example.com

# Dayforce Identity — goes to the login page, clicks "Create one now."
.venv/bin/python creator.py --portal dayforce \
  --email you@example.com --first-name First --last-name Last

# iCIMS — point it at the company's iCIMS job/apply page
.venv/bin/python creator.py --portal icims \
  --apply-url "https://careers-garmin.icims.com/jobs/20135/software-engineer-intern/job" \
  --email you@example.com --first-name First --last-name Last \
  --generate-password --gmail-poll
```

The password is **never** written to disk. Provide it via (in order of precedence):

1. `--password` (discouraged — visible in shell history),
2. `WORKDAY_PASSWORD` / `DAYFORCE_PASSWORD` / `ICIMS_PASSWORD` environment variable,
3. `--generate-password` — generates a strong random password and prints it in a
   SAVE-THIS banner at the end (never stored; copy it into your password manager),
4. the secure interactive prompt (default).

## How it works

1. Opens the portal's create-account page in a persistent browser profile
   (`~/.cache/acct-creator-profile`, so cookies survive between runs).
2. Fills email / password / confirmation (plus name fields on Dayforce).
3. **Consent checkboxes are never ticked automatically.** Dayforce's
   registration asks you to attest you've reviewed its Privacy Statement and
   Terms of Use — the script pauses and asks you to review and tick it
   yourself. (Workday's create-account form has no checkbox.)
4. **CAPTCHA:** if a challenge appears, the script pauses — solve it in the
   browser window, then press Enter to continue.
5. Submits. Detects "account already exists" and reports it instead of failing.
6. **Email verification (Workday):** with `--gmail-poll` (only works where the
   `hatch_gws_cli` Gmail tool is installed) it polls your inbox for the code;
   otherwise you're prompted to paste it. Most Workday tenants proceed
   straight to the application with no code step. Dayforce emails a
   verification link — the script stops after submitting and tells you to
   check your inbox.
7. **iCIMS flow:** the script handles the whole pipeline — clicks Apply if
   needed, fills the email-first step (solving/pausing for the hCaptcha),
   then handles the verification link: with `--gmail-poll` it polls Gmail for
   the iCIMS email and opens the link automatically (`--gmail-query` /
   `--gmail-timeout` tune the search); otherwise you're prompted to paste the
   link. It then fills the Candidate Profile's **Create a login** section
   (Login, Password, Password Re-enter) plus first/last name, and submits.

## Notes

- Form selectors were verified against the live RBC Workday and Dayforce
  Identity portals on 2026-09-28. Workday's create-account form is the same
  product across tenants, so it works for any `myworkdayjobs.com` posting —
  just swap `--apply-url`.
- Run this on a machine with direct internet access. (It was developed in a
  sandboxed VM where scripts can't reach the public web directly — the
  selectors were verified through a managed browser instead.)
- If the proxy environment variables (`https_proxy` etc.) are set, they're
  passed through to the browser automatically.
- Screenshots are saved next to the script (`workday_after_submit.png`,
  `dayforce_after_submit.png`) whenever the post-submit state is ambiguous.

---

# apply_workday.py — scripted Workday application driver

End-to-end Workday candidate-flow automation (added 2026-11-08). Creates the
tenant account itself, uploads and parses the resume, and walks My
Information → My Experience → Application Questions → Voluntary Disclosures →
Review → Submit with a label-driven rules engine encoding the applicant's
standing answers (relocation Yes, co-op Yes, no-self-reject, decline self-ID
where offered, J-1 sponsorship declared truthfully for US roles).

```bash
xvfb-run -a .venv/bin/python apply_workday.py \
  --url "https://tenant.wd5.myworkdayjobs.com/Site/job/.../Role_REQ123" \
  --country CA|US --term winter|summer
```

Exit codes: 0 submitted · 2 posting unavailable · 3 blocked · 4 needs-human
(unhandled required question) · 5 error. Prints a final `RESULT {json}` line.

## Pieces

- `relay.py` — local CONNECT relay (127.0.0.1:18080). The sandbox egress
  proxy MITMs TLS and Chromium can't use it directly; the relay re-issues
  CONNECTs upstream with proxy auth, and the driver launches with
  `ignore_https_errors`. Auto-started by the driver.
- `vault.json` (gitignored, mode 0600) — per-tenant generated account
  passwords, `{host: {email, password, created}}`. A password is saved ONLY
  after the create-account step is verified to have advanced, so the vault
  never holds bogus credentials. This is what lets repeat runs sign in
  instead of dying on forgot-password detours.
- `profile.json` (gitignored) — applicant facts + standing rules. See
  `profile.example.json` for the schema.
- Field model: the driver handles both Workday UI generations — new
  candidate UI (`formField-*` containers) and classic (`formRow`) — with
  label-driven fillers, hierarchical multiselect handling (category →
  leaf, chip-verified), and structural auth detection (verify-password
  field present = create form).

First verified end-to-end submission: HPE Systems Software Engineer Intern
(req 1213401), 2026-11-08.
