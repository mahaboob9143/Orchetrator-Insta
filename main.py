#!/usr/bin/env python3
"""
InstaAgent — Autonomous Islamic Instagram Content System
========================================================

A fully local, zero-cost multi-agent system for maximizing Instagram
engagement for an Islamic cinematic content account.

Usage:
  python main.py                    # Full autonomous loop
  python main.py --dry-run          # Simulate everything, no actual posting
  python main.py --once             # Run one full cycle and exit
  python main.py --agent trend      # Test TrendAgent only
  python main.py --agent media      # Test MediaAgent only
  python main.py --agent caption    # Test CaptionAgent only
  python main.py --agent scheduler  # Test SchedulerAgent only
  python main.py --agent engagement # Info about EngagementAgent
  python main.py --post-now         # Instant post (bypass scheduler + Ollama)
  python main.py --repost           # Scrape @softeningsayings and repost one image

Environment:
  Requires .env file with META_ACCESS_TOKEN, IG_ACCOUNT_ID, UNSPLASH_ACCESS_KEY.
  Copy .env.template to .env and fill in your credentials.

Config:
  All runtime settings and feature flags live in config.yaml.
  Changes take effect on the next orchestration cycle (hot-reload, no restart needed).
"""

import argparse
import os
import sys

from dotenv import load_dotenv


# ─── Bootstrap ────────────────────────────────────────────────────────────────

def _load_env_or_exit() -> None:
    """Load .env file. Abort if required variables are missing."""
    load_dotenv()

    required = {
        "META_ACCESS_TOKEN": "Long-lived Meta access token",
        "IG_ACCOUNT_ID":     "Instagram Business/Creator account ID",
        "UNSPLASH_ACCESS_KEY": "Unsplash API access key",
    }

    missing = {k: v for k, v in required.items() if not os.getenv(k)}
    if missing:
        print("\n❌  Missing required environment variables in .env:\n")
        for var, description in missing.items():
            print(f"     {var:<26} — {description}")
        print("\n  Copy .env.template → .env and fill in your credentials.\n")
        sys.exit(1)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="instaagent",
        description="Autonomous Islamic Instagram automation system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                    # autonomous loop
  python main.py --dry-run          # simulate (no posting)
  python main.py --once             # single cycle
  python main.py --agent trend      # test TrendAgent
  python main.py --agent caption    # test CaptionAgent
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate all steps without actually posting to Instagram",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one complete cycle and exit",
    )
    parser.add_argument(
        "--agent",
        type=str,
        metavar="NAME",
        choices=["trend", "media", "caption", "scheduler", "engagement"],
        help="Run a single agent in isolation for testing",
    )
    parser.add_argument(
        "--post-now",
        action="store_true",
        help=(
            "Immediately download one image and post to Instagram using a template "
            "caption — bypasses scheduler and Ollama. Use to validate the full pipeline."
        ),
    )
    parser.add_argument(
        "--image-url",
        type=str,
        metavar="URL",
        help="Use a public image URL for --post-now test to bypass local server requirements",
    )
    parser.add_argument(
        "--repost",
        action="store_true",
        help=(
            "Scrape one image from configured source accounts, rewrite its caption, "
            "and publish to your Instagram. Requires repost.enabled=true in config.yaml "
            "and IG_SCRAPE_USER / IG_SCRAPE_PASS in .env."
        ),
    )
    parser.add_argument(
        "--db",
        type=str,
        default="instaagent.db",
        metavar="PATH",
        help="Path to SQLite database file (default: instaagent.db)",
    )
    return parser.parse_args()


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    # 1. Load .env and validate credentials
    _load_env_or_exit()

    # 2. Parse CLI arguments
    args = _parse_args()

    # 3. Initialise database (creates tables if needed, creates media/queue + logs dirs)
    from core.db import init_db
    init_db(db_path=args.db)

    # 4. Check config.yaml is present
    if not os.path.isfile("config.yaml"):
        print(
            "\n❌  config.yaml not found in the current directory.\n"
            "   Make sure you are running from the project root: python main.py\n"
        )
        sys.exit(1)

    # 5. Announce startup mode
    from core.logger import get_logger
    logger = get_logger("Main")

    mode = "DRY RUN" if args.dry_run else ("SINGLE CYCLE" if args.once else "AUTONOMOUS")
    if args.agent:
        mode = f"AGENT TEST [{args.agent.upper()}]"
    if args.post_now:
        mode = "POST NOW (instant test)"
    if args.repost:
        mode = "REPOST (scrape + publish)"

    logger.info("=" * 60)
    logger.info(f"  InstaAgent starting — mode: {mode}")
    logger.info(f"  DB: {args.db}")
    logger.info("=" * 60)

    # 6. Instantiate and run the orchestrator
    from agents.orchestrator import Orchestrator
    orchestrator = Orchestrator(dry_run=args.dry_run)

    if args.repost:
        orchestrator.repost_now()
    elif args.post_now:
        orchestrator.post_now(image_url=args.image_url)
    elif args.agent:
        orchestrator.run_single_agent(args.agent)
    elif args.once:
        orchestrator.start(once=True)
    else:
        orchestrator.start(once=False)


if __name__ == "__main__":
    main()
