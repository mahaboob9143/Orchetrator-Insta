# InstaAgent Architecture & Features Deep Dive

This document is intended for the engineering team to understand the technical implementation details, design decisions, and core features of the InstaAgent system. 

---

## 🏗 Core Infrastructure

### 1. Hot-Reloading Configuration (`core/flags.py`)
Instead of requiring system restarts to change behavior, the system reads `config.yaml` directly on every loop iteration. 
- **Benefit**: You can toggle `posting_enabled: false` (acting as a kill-switch) or change the AI prompt style while the script is running. The Orchestrator will seamlessly pick up the change on the next cycle, and will safely sleep in 60-second chunks to ensure rapid response to the kill-switch.

### 2. SQLite with WAL Mode (`core/db.py`)
The system uses a highly concurrent SQLite setup utilizing Write-Ahead Logging (WAL). 
- **Benefit**: Background threads (like the EngagementAgent) can write metrics to the database without locking out or blocking the main Orchestrator loop from reading queue counts or saving new posts.
- **Tables**: `images` (with pHash), `posts`, `trends`, `engagement`, `posting_windows`.

### 3. Circuit Breaker & Fallbacks (`agents/orchestrator.py`)
To ensure the bot doesn't spam errors and get rate-limited if a third-party API goes down:
- **Fallback Chain**: If `TrendAgent` fails, it falls back to hardcoded `_FALLBACK_TOPICS`. If `CaptionAgent` fails, it drops down to a standard `_FALLBACK_CAPTION` template.
- **Circuit Breaker**: If 3 consecutive cycles fail entirely (e.g., Unsplash is down AND no queued images exist), the Orchestrator pauses the entire system for 1 hour before retrying to prevent aggressive loop spin.

### 4. Image Tunnelling / Hosting (`core/tunnel.py` & `poster_agent.py`)
Meta's Graph API requires a publicly reachable URL to download images for publishing. Since most home networks use CGNAT or dynamic IPs, manually forwarding port `8765` is unreliable.
- **Ngrok Feature**: We integrated `pyngrok`. The PosterAgent spins up a local `http.server`, securely tunnels it to the internet via Ngrok, hands the HTTPS link to Meta, and drops the tunnel the moment publishing is complete.
- **ImgBB Alternative**: If `use_imgbb: true` is set, the system bypasses local hosting entirely and uploads the media safely to the public ImgBB API, pushing that URL to Meta instead.

---

## 🤖 The 6 Core Agents

### 1. TrendAgent (`agents/trend_agent.py`)
*Determines what we should post about today.*
- Queries **Google Trends** (via `pytrends`) using a defined batch of seed keywords from the config. 
- Automatically handles `urllib3` deprecations and connection anomalies.
- **Competitor Scraping (Optional)**: Can use `instagrapi` to scrape specified competitor accounts. This relies on the private Instagram API and carries higher risk, so it is gated behind the `trend_scraping` feature flag.

### 2. MediaAgent (`agents/media_agent.py`)
*Procures high-quality assets.*
- Connects exclusively to the **Unsplash API**.
- Focuses on downloading cinematic, vertical images.
- **Perceptual Hashing (pHash)**: As images are downloaded, they are run through `imagehash`. If the hash matches an image already in the DB (even slightly cropped/resized), the image is rejected. This guarantees the bot never posts the exact same photo twice.

### 3. CaptionAgent (`agents/caption_agent.py`)
*Generates the engaging text content.*
- Interfaces locally with **Ollama** (defaulting to the `mistral` model).
- Contains logic to automatically trigger an `ollama pull mistral` command if the model isn't currently installed on the host machine.
- Forces the LLM to output pure JSON to guarantee machine-readable structures, which separates the caption text from the generated tags.

### 4. SchedulerAgent (`agents/scheduler_agent.py`)
*Machine-learning driven posting times.*
- Keeps track of the `daily_limit` feature flag constraints.
- Monitors the `posting_windows` database table. It actively links previous historic post times to their `reach` metrics to mathematically prefer posting at hours that have proven to yield the most engagement.
- Applies a **humanization jitter**: Adds a random `3 to 15 minute` offset so posts don't fire exactly on the hour.

### 5. PosterAgent (`agents/poster_agent.py`)
*The executor.*
- Orchestrates the official **Meta Graph API 3-step upload**:
  1. Creates a media container (`POST /{ig-id}/media`).
  2. Polls `status_code` every 5 seconds until it transitions to `FINISHED`.
  3. Publishes the container (`POST /{ig-id}/media_publish`).
- **Error Handling**: Hardcoded interception for Error `#10` (Permission/Scope issues) and `#190` (Token Expiration) to immediately alert the user without burning retry tokens.

### 6. EngagementAgent (`agents/engagement_agent.py`)
*The feedback loop.*
- Does not run synchronously. Instead, passing an `ig_post_id` schedules jobs inside an `APScheduler` background thread.
- Automatically wakes up at **1-hour, 6-hour, and 24-hour** marks after a post goes live.
- Fetches `likes`, `comments`, `shares`, `reach`, and `saves`. If a post crosses a high-performance threshold defined in `config.yaml`, it marks it in the DB.
- At the 24-hour mark, it commits the total `reach` to the `posting_windows` table to train the `SchedulerAgent` for future posts.
