# Deployment Guide (GitHub Actions)

This guide explains how to deploy the InstaAgent Repost pipeline to GitHub Actions for **$0/month**.

## How It Works
1. GitHub Actions triggers the script twice daily based on a cron schedule.
2. The script scrapes a post and generates the caption.
3. The image is uploaded to **Cloudinary** so Meta's API can access it.
4. The Meta Graph API publishes the post.
5. The post's ID is saved to `data/reposted_ids.txt`, and the changed file is committed back to the repository automatically.

---

## 1. Cloudinary Setup (Image Hosting)
Meta's Graph API requires a public URL to fetch images. `ngrok` doesn't work well for headless cloud runs. 
Cloudinary provides a permanent public URL, and the script auto-deletes the image from Cloudinary as soon as Meta publishes it.

1. Go to [Cloudinary](https://cloudinary.com/) and create a free account.
2. Go to Dashboard and copy:
   - **Cloud Name**
   - **API Key**
   - **API Secret**

---

## 2. GitHub Secrets Configuration
In your GitHub Repository, go to **Settings > Secrets and variables > Actions > New repository secret**.

Add the following secrets:

- `META_ACCESS_TOKEN` (Long-lived Graph API token)
- `IG_ACCOUNT_ID` (Your Instagram account ID)
- `IG_SESSION_ID` (Your session cookie from the browser)
- `IG_SCRAPE_USER` (Optional target scraping user)
- `CLOUDINARY_CLOUD_NAME` (From step 1)
- `CLOUDINARY_API_KEY` 
- `CLOUDINARY_API_SECRET`

---

## 3. GitHub Actions Configuration
The schedule is defined in `.github/workflows/repost.yml`.

- **Morning:** `30 1 * * *` (1:30 AM UTC = 7:00 AM IST)
- **Evening:** `30 13 * * *` (1:30 PM UTC = 7:00 PM IST)

**Humanization Delay:** The script automatically runs a `sleep $((RANDOM % 1800))` command which introduces a random delay of 0-30 minutes, so posts aren't made at the exact same minute every day.

---

## 4. Give GitHub Actions Write Permissions
Because the script tracks deduplication in `data/reposted_ids.txt` (and commits it), the Action needs permission to write to the repository.

1. In your GitHub repo, go to **Settings > Actions > General**.
2. Scroll down to **Workflow permissions**.
3. Select **Read and write permissions**.
4. Check **Allow GitHub Actions to create and approve pull requests** (just in case).
5. Save.

---

## 5. Test It
Go to the **Actions** tab in GitHub > **Repost Pipeline** > **Run workflow**.

Wait ~2 minutes and verify:
1. Did the post appear on Instagram?
2. Did the Action finish cleanly?
3. Was `data/reposted_ids.txt` updated in the code?

## Maintenance
The only maintenance required is updating `IG_SESSION_ID` in GitHub Secrets roughly every ~90 days when the Instagram browser session cookie expires.
