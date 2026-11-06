# Job Agent

A personal app that finds jobs matching your description and keeps track of your applications: who replied, who didn't, and your interviews. It runs on your own computer and only you can open it.

You apply yourself on each company's site. The app finds and ranks the jobs, then watches your Gmail so you never have to remember who you applied to or who got back to you. (It can also auto-fill some forms. That's switched off by default, see the end.)

## How you use it day to day

1. **Review** shows new jobs that match your description, best first, with the reason each one scored well.
2. Press **Open posting**, apply on the company's site, come back and press **I applied** (or **Dismiss** if it isn't for you).
3. From then on, replies in your Gmail are matched to that application and sorted into: receipt, reply, interview invitation, offer, rejection. After 14 days of silence it becomes **No reply**.
4. When an interview invitation arrives it is flagged on the dashboard; add the date and link on the application page.
5. Applied somewhere the app didn't find? **Applications → Add an application I made myself**.

## Where jobs come from

Only official public APIs and feeds. LinkedIn, Indeed and PNet are not used (no public API, and automated access is risky for your accounts).

| Source | Setting in config | Notes |
|---|---|---|
| Remotive, Himalayas, RemoteOK, We Work Remotely | `remotive`, `himalayas`, `remoteok`, `weworkremotely`: `true`/`false` | Remote jobs. Remotive and RemoteOK ask for a link back; the "Open posting" links count. |
| Arbeitnow | `arbeitnow` | Mostly Europe |
| Adzuna | `adzuna:` enabled, app_id, app_key, `country: za` | Best South African coverage. Free key at developer.adzuna.com |
| Greenhouse, Lever, Ashby, Workable, SmartRecruiters | lists of company names | Search specific companies' career pages (see below) |

**Adding companies.** Take the name from the company's job-page address:

```
boards.greenhouse.io/stripe        ->  greenhouse: [stripe]
jobs.lever.co/netlify              ->  lever: [netlify]
jobs.ashbyhq.com/linear            ->  ashby: [linear]
apply.workable.com/canonical       ->  workable: [canonical]
jobs.smartrecruiters.com/Visa      ->  smartrecruiters: [Visa]
```

A wrong name is logged under Settings → Activity log and doesn't stop the other companies.

**Remote jobs and your region.** A remote job counts as a location match only if it is open to your region: "Worldwide", "Anywhere", "Africa", "EMEA" and so on (`search.remote_regions`). A "Remote, USA only" job does not get the location points, so it ranks lower.

## Setup
1. Clone the repo and create a virtual environment:
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
2. Create a `data/` folder and copy `config.example.yaml` to `data/config.yaml`
3. Fill in your own details and credentials
4. Add your own resume as `data/resume.docx`
5. Create your own Gmail OAuth client (see Google Cloud Console) and save it as `data/gmail_client.json`

## Setup on Windows (PowerShell)

Needs Python 3.10+. Run each command on its own line, inside the project folder:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

You don't need to activate the venv: always call `.venv\Scripts\python.exe`.

**Keep your data out of OneDrive.** The app stores your CV, database and Gmail token in a data folder. Put it somewhere local:

```powershell
setx JOBAGENT_DATA "C:\JobAgentData"
$env:JOBAGENT_DATA = "C:\JobAgentData"
```

Start it:

```powershell
.venv\Scripts\python.exe -m jobagent
```

Open http://127.0.0.1:8787 and paste the access token printed in the terminal (also saved in `C:\JobAgentData\.access_token`).

Mac/Linux: use `.venv/bin/python` instead of `.venv\Scripts\python.exe`, and `export JOBAGENT_DATA=~/jobagent-data`.

Look around with sample data first: set `$env:JOBAGENT_DATA = "C:\JobAgentDemo"` and add `--demo` to the start command.

### First-time configuration (Settings page)

1. Upload your **CV** (optional if you only apply manually, but harmless).
2. In the config box, fill in your **search** description: `titles`, `skills`, `locations`, `exclude_title`.
3. Add companies you like to the `greenhouse` / `lever` / `ashby` / `workable` / `smartrecruiters` lists.
4. Make sure `apply: mode:` is `"off"`.
5. Click **Save**, then **Search for jobs now** on the dashboard.

### Connect Gmail (reply tracking)

1. console.cloud.google.com: create a project and enable the **Gmail API**.
2. **Google Auth Platform → Audience**: choose External, and add your own Gmail address under **Test users**.
3. **Clients → Create client → Desktop app**. Download the JSON and save it into the app's data folder as `gmail_client.json`. Google's own file name (`client_secret_....json`) also works. The data folder is printed as `Data folder:` when the app starts and shown at the bottom of the Gmail box in Settings. It must be the folder the **running** app uses.
4. In the app: **Settings → Connect Gmail**. Google shows an "unverified app" warning: **Advanced → Continue**.
5. Back on the Audience page, click **Publish app** so the connection doesn't expire every 7 days.

Access is read-only. The app reads sender, subject and a short preview, only to match replies to jobs you applied for.

### If Gmail tracking isn't working

Open **Settings → Gmail** and press **Check now**. The message at the top says what happened:

| Message | Meaning and fix |
|---|---|
| "No client file found. The app is looking in ..." | The file is in a different folder than the one the app is using. Move it into the folder named in the message. (If you meant to use `C:\JobAgentData`, start the app in a window where you ran `$env:JOBAGENT_DATA = "C:\JobAgentData"`.) |
| "... is not an OAuth client file" / "no client_secret" | Wrong kind of file. Create an **OAuth client ID** of type **Desktop app** and download its JSON right after creating it. |
| "no applications to track yet" | Gmail works. Tracking only starts for jobs you pressed **I applied** on (or added under Applications). |
| "permission to read your email was not granted" | On Google's permissions screen the Gmail box wasn't ticked. Connect again and tick it. |
| "Gmail would not answer ... SAME Google Cloud project" | The Gmail API is enabled in a different project than your OAuth client. Enable it in the one that owns the client. |
| "Gmail problem: ... 401 / refresh token" | The connection expired (common if the consent screen is still in *Testing*). Publish the app on the Audience page, then Disconnect and Connect again. |
| "Checked N new emails; 0 matched" | Working, but nothing from those companies yet. Replies are matched by the company name in the sender or subject. Check your Spam folder too, and use **Rescan last 45 days** after adding applications. |
| Google error `redirect_uri_mismatch` | The OAuth client must be of type **Desktop app**. |

A reply is only matched to a job you've marked as applied, so press **I applied** right after you apply.

## Scores

Each job is scored 0-100: title match 40, your skills 30, nice-to-have skills 15, location 15. Jobs below `min_score` (45) are kept on the **Jobs** page with the reason, so you can see why something was skipped and click **Queue anyway**. Changing the config re-scores jobs already found.

## Safety

- The server only listens on your own computer, sign-in needs a secret token, and cross-site requests are blocked.
- CV, database, Gmail token (encrypted) and access token live in the data folder with owner-only permissions.
- Nothing leaves your computer except requests to the job boards, Gmail and (if you use auto-apply) company application forms.

## Optional: auto-apply (off by default)

`apply: mode:` can be `approve_all` (you approve each job, the app fills the form) or `auto_strong` (high scores go straight through). It fills Greenhouse and Lever forms in a real browser (`.venv\Scripts\python.exe -m playwright install chromium` first), or emails the employer through Gmail if the posting lists an address. `dry_run: true` fills the form and waits for your click. It never solves CAPTCHAs or guesses answers to questions like salary; those go to "Apply manually".

## Project layout

```
jobagent/   main.py (web app) · sources.py (job boards) · matcher.py (scoring) · pipeline.py
            tracker.py (match replies) · gmail.py · applier.py (optional auto-apply) · scheduler.py · db.py
tests/      .venv\Scripts\python.exe -m unittest discover -s tests
```

## Known limits

- Reply classification is keyword based: good at receipts, rejections and interview invitations, but it will occasionally misfile an email. Fix a status from the application page, or remove a wrongly matched email from its timeline.
- Emails are matched to jobs by company name. Very short or generic company names can be mismatched.
- Interview dates are not read out of emails; add them yourself when an invitation arrives.
- SmartRecruiters descriptions need one extra request per job, so only jobs whose title could plausibly match are fetched (max 25 per company per search).
- Job-board APIs change. A failing source is logged under Settings → Activity log and doesn't stop the others.
- Publishing this for other people would be a rebuild (accounts, a server database, Google's security review for Gmail access), not a switch.
