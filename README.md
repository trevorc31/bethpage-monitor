# Bethpage Black/Red Tee-Time Monitor

Read-only availability monitor. Polls ForeUp's public JSON, alerts the moment a
Black or Red slot opens in your windows. **You** book by hand from the link —
this never logs in, never holds, never books, so your account is never at risk.

**Alert rules (as configured):**
- Fri 3:00pm–twilight · Sat any time · Sun before 9:00am (edit `config.yaml`)
- Black openings → **text + email** · Red openings → **email only**
- 2–4 spot openings are flagged so you can bring a group
- Fri/Sat alerts carry a `*` priority marker

**Two engines:**
1. **Drop race** — starts ~6:40pm ET daily, sleeps to 6:59:30, then polls every
   5 s through 7:06pm to catch the new day (resident +7) the instant it drops.
2. **Cancellation watch** — every 15 min, 6am–10pm ET.

---

## Setup (one time, ~10 minutes)

### 1. Create the repo
1. github.com → New repository → name it `bethpage-monitor` → **Public**
   (public = unlimited free Actions minutes; nothing sensitive lives in the repo).
2. Upload every file in this folder, keeping the structure — the
   `.github/workflows/` folder must be exactly that path. Easiest way:
   on the new repo page click **uploading an existing file**, drag the whole
   folder contents in, commit.

### 2. Gmail app password (for sending alerts)
1. Google Account → Security → turn on **2-Step Verification** (if not already).
2. Go to myaccount.google.com/apppasswords → create one named `bethpage` →
   copy the 16-character password.

### 3. Add repo secrets
Repo → Settings → Secrets and variables → Actions → **New repository secret**, four times:

| Name | Value |
|---|---|
| `GMAIL_ADDRESS` | your full gmail address |
| `GMAIL_APP_PASSWORD` | the 16-char app password |
| `ALERT_EMAIL` | where email alerts go (can be the same gmail) |
| `SMS_EMAIL` | `8148532805@vtext.com` |

### 4. Verify the course map (30 seconds)
Repo → **Actions** tab → enable workflows if prompted → select **probe** →
**Run workflow**. Open the run → `discover courses` step. You should see
Black and Red with their schedule IDs. If the names look right, you're done —
the monitor caches this and re-checks weekly.

### 5. Test an alert
Actions → **cancellation-watch** → Run workflow. First run alerts on *every*
currently-open slot in your windows (baseline), which doubles as an
end-to-end test of email + text. After that, only *new* openings alert.

That's it. Both schedules run themselves.

---

## Tuning

- **Change windows/days:** edit `config.yaml`, commit. Next run picks it up.
- **Texts not arriving?** Verizon's vtext gateway is being phased out for some
  senders. Fallback: install the free **ntfy** app, subscribe to a topic, and
  ask for the one-line patch — or just rely on email (always sent).
- **Pause everything:** Actions tab → select workflow → `···` → Disable.
- **Booking-limit reminder:** Black bookable 1×/28 days, Red 1×/14 days,
  cancel ≥48h out, max 6 cancels/month, $5 fee — the monitor doesn't track
  your personal limits, so keep them in mind before booking.

## Files
- `monitor.py` — all logic (discovery, polling, filtering, diffing, alerting)
- `config.yaml` — your rules
- `state.json` — bot memory (auto-committed; don't edit)
- `.github/workflows/` — drop-race, cancellation-watch, probe
