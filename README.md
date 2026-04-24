# InstaAgent — Cloud Repost Pipeline

A fully autonomous Instagram repost agent that scrapes public competitor content, rewrites captions using a rule-based engagement engine, and publishes automatically via the official Meta Graph API.

## Features
- **No Login Scraping:** Uses `instaloader` to pull content from public profiles securely.
- **Rule-based Captioning:** Categorizes content and weaves engaging hooks/CTAs automatically (no AI required).
- **Auto Image Optimization:** Filters out non-compliant aspect ratios and prepares assets for Instagram.
- **Cloud-native Uploads:** Uses Cloudinary to host images publicly so the Meta Graph API can pull them.
- **Deduplication:** Lightweight text-based tracker to ensure content is never reposted twice.
- **Headless Execution:** Designed to run in CI/CD (GitHub Actions) with randomized cron scheduling.

## Local Setup

1. Copy `.env.example` (or the format mentioned in deployment) to `.env`.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the repost pipeline once:
   ```bash
   python main.py --repost
   ```

## Cloud Deployment
See [DEPLOYMENT.md](DEPLOYMENT.md) for full instructions on running this on GitHub Actions for free.
