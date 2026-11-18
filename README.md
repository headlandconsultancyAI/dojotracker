# Issue monitor

Watches Glassdoor reviews and LinkedIn posts for talk of redundancies, filters out anything off-topic, and publishes a dashboard on GitHub Pages.

Apify scrapes both sites every 30 minutes. When each scrape finishes, Apify pings GitHub, which pulls the new results, adds them to the archive in `data/`, re-runs the filter over everything and republishes the site. The dashboard checks for new data every 5 minutes while it's open.

## Why the data is encrypted

The repo has to be public so GitHub Actions stays free at this frequency (a private repo would use roughly 4,000 minutes a month, double the free allowance). So nothing in the repo or on the site names the client or holds readable data:

- The archive (`data/state.enc.json`) and the site's data (`data.enc.json`) are encrypted with AES-256 using `DASHBOARD_PASSWORD`. The page asks for it and decrypts in the browser.
- The client's name, people and look-alike organisations live in the `MONITOR_CONFIG` variable, which only people with access to the repo's settings can see.
- The run logs are public, so the script only ever prints counts.

Give the repo a neutral name (not the client's) and send the passphrase separately from the link.

## What's in the repo

```
.github/workflows/refresh.yml   runs on every Apify webhook, plus a backup every 30 minutes
monitor.py                      pulls from Apify, filters, builds the site
rules.py                        the redundancy language, themes and tone words (edit this to tune)
site/index.html                 the dashboard
data/                           the encrypted archive (created on the first run)
config.example.json             the shape of MONITOR_CONFIG
```

## One-time setup in Apify

**Glassdoor task.** You've already run it once for the full history (857 reviews). Leave it like that until the first GitHub run has pulled it in, then change the input to the most recent 50 reviews or so, sorted by date. The archive keeps everything, so there's no need to re-scrape the full history every half hour.

**LinkedIn task.** A single broad keyword search is mostly noise (the first test scrape for the company's name returned 270 posts, about 20 of them about the company and one about redundancies). Use several narrower searches instead, each sorted by **Latest** and limited to the **Past 24 hours** or **Past week**:
- Keyword searches pairing the name with redundancy words: `COMPANY redundancies`, `COMPANY layoffs`, `COMPANY restructure`, `laid off COMPANY`
- If your LinkedIn shows them under **All filters**, a search filtered to **Mentions company** and one filtered to **Author company** (posts by people who work there). Set the filters in LinkedIn, then paste the search URL into Apify.

The scraper's comments come through too. Comments on posts that name the company are checked as their own items, because that's often where criticism lands.

Keep the scrape to the account you'd be comfortable losing. Logged-in LinkedIn scraping every 30 minutes can trigger restrictions, so an hourly LinkedIn schedule is a safer middle ground if the account matters.

**Schedules.** On each task (the task, not the actor), add a schedule for every 30 minutes: `*/30 * * * *`.

**Webhooks.** On each task go to **Integrations → Webhook** and add one:
- Event: **Run succeeded**
- URL: `https://api.github.com/repos/YOUR-USERNAME/YOUR-REPO/dispatches`
- Headers:
  ```
  {"Authorization": "Bearer YOUR-GITHUB-TOKEN", "Accept": "application/vnd.github+json"}
  ```
- Payload template: `{"event_type": "apify-run"}`

The GitHub token is a fine-grained personal access token with **Contents: Read and write** on this repo only. Note when it expires. If it lapses, the backup schedule still runs, just with more lag.

## One-time setup in GitHub

1. Create a new **public** repository with a neutral name and upload these files. GitHub's drag-and-drop uploader skips the `.github` folder, so after uploading, go to **Add file → Create new file**, name it `.github/workflows/refresh.yml` and paste the workflow in.
2. **Settings → Secrets and variables → Actions → Secrets:**
   - `APIFY_TOKEN`
   - `DASHBOARD_PASSWORD`: a long passphrase. Three or four random words is good.
3. Same page, **Variables** tab:
   - `APIFY_GLASSDOOR_TASK_ID` and `APIFY_LINKEDIN_TASK_ID`: from the task URLs in Apify, after `/actors/tasks/`. You can also use `username~task-name`.
   - `MONITOR_CONFIG`: paste in the contents of the separate `MONITOR_CONFIG.json` file (not the example in this repo).
   - `MONITOR_OVERRIDES`: `{"include": [], "exclude": []}`
4. **Settings → Pages → Source:** GitHub Actions.
5. **Actions → Refresh monitor → Run workflow.** The link to the site appears on the deploy step.

## Day to day

**Correcting the filter.** Every card has a **Not relevant** button, and every item under **Filtered out** has **Actually relevant**. These change your own browser straight away. To apply them for everyone, click **Copy settings** in the banner that appears and paste it over the `MONITOR_OVERRIDES` variable. The next run picks it up.

**Tuning the rules.** If the same kind of false positive keeps coming up, add a phrase to `GUARDS` in `rules.py`. If genuine posts are being missed, add a pattern to `TOPIC_PATTERNS`. Look-alike organisations go in `exclude_terms` in `MONITOR_CONFIG`. Every run re-scores the whole archive, so changes apply backwards too.

**Changing the passphrase.** Add the current one as a secret called `DASHBOARD_PASSWORD_PREVIOUS`, then change `DASHBOARD_PASSWORD`. The next run re-encrypts everything with the new one. You can delete the previous one after that.

**Pulling in a specific scrape.** **Run workflow** has optional boxes for an Apify dataset ID, if you ever need to backfill a one-off run.

## How the filter decides

- **Confirmed:** clear redundancy language, such as redundancies, layoffs, made redundant, job cuts, consultation period, headcount reduction or severance. Spanish, Italian, Portuguese and French equivalents are included because Glassdoor pages often include reviews from European offices.
- **Possible:** only adjacent language, such as restructuring, reorgs, people being let go or "no job security".
- Phrases that only look relevant ("dismissive", "fired up", "redundant meetings", "price cuts") are removed before scoring.
- LinkedIn posts also have to be about this company. They count if the company's mentioned alongside payments language, a named founder, or by an author whose headline names it. Posts that only mention a look-alike are filtered out with the reason shown.
- **Tone:** Glassdoor uses the star rating (1 to 2 negative, 3 mixed, 4 to 5 positive). LinkedIn uses a word list, so treat it as a guide.
- **Post type** (LinkedIn): affected employee, recruiter or support offer, company or leadership, or commentary.

## Testing on your own machine

```bash
pip install cryptography
python monitor.py --glassdoor-file scrape.json --config MONITOR_CONFIG.json
python -m http.server -d out
```

Then open http://localhost:8000. Without `DASHBOARD_PASSWORD` set, the output isn't encrypted, so keep local test data out of the repo.

## When something goes wrong

- **Red cross in Actions:** open the failed step. "Couldn't unlock" means the passphrase changed without `DASHBOARD_PASSWORD_PREVIOUS` being set.
- **Header says a source is overdue:** its last successful scrape was over 90 minutes ago. Check the task's Runs tab in Apify.
- **Webhook says Succeeded but nothing runs:** Apify only shows that GitHub accepted the call. Check the token hasn't expired and that the event type is exactly `apify-run`.
