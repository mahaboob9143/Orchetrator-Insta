"""
agents/caption_agent.py — CaptionAgent for InstaAgent.

Responsibilities:
  1. Verify Ollama is running and the configured model is available.
  2. Auto-pull the model via Ollama REST API if not found (when auto_pull=true).
  3. Generate bilingual (Arabic + English) Islamic captions using Mistral 7B.
  4. Fall back to curated template captions if Ollama fails or auto_caption=false.
  5. Optimize hashtag selection using high-performer data from the engagement table.
"""

import json
import random
from typing import Dict, List, Optional

import requests

from core.db import get_top_hashtags_by_reach
from core.flags import get_config, get_flags
from core.logger import get_logger
from core.retry import retry

logger = get_logger("CaptionAgent")


# ── Default Islamic hashtags (used when DB has no high-performer data yet) ────

_DEFAULT_HASHTAGS: List[str] = [
    "#Islam", "#Islamic", "#Quran", "#Allah", "#Muslim",
    "#Hadith", "#ProphetMuhammad", "#IslamicQuotes", "#Iman", "#Tawakkul",
    "#Sabr", "#Dhikr", "#Jannah", "#Deen", "#IslamicReminder",
    "#IslamicPost", "#QuranVerses", "#IslamicArt", "#SubhanAllah",
    "#AllahuAkbar", "#Alhamdulillah", "#MashAllah", "#Jummah",
    "#IslamicMotivation", "#MosqueArchitecture",
]

# ── Fallback template captions (used when auto_caption=false or Ollama fails) ─

_TEMPLATE_CAPTIONS: List[str] = [
    (
        "سبحان الله — Glory be to Allah ☁️\n\n"
        "In His creation lies every sign for those who reflect.\n"
        "Let this moment be a reminder of His endless grace.\n\n"
        "{cta}\n\n{hashtags}"
    ),
    (
        "الحمد لله — All praise belongs to Allah 🤍\n\n"
        "No matter what life brings, gratitude opens every door.\n"
        "Count your blessings — they are beyond counting.\n\n"
        "{cta}\n\n{hashtags}"
    ),
    (
        "اللَّهُ أَكْبَر — Allah is the Greatest ✨\n\n"
        "In every hardship, He has already written the relief.\n"
        "Trust His plan. He knows what you do not.\n\n"
        "{cta}\n\n{hashtags}"
    ),
    (
        "إِنَّ مَعَ الْعُسْرِ يُسْرًا\n"
        "Indeed, with hardship comes ease. (Quran 94:6) 🌿\n\n"
        "Hold on. Your dawn is coming.\n\n"
        "{cta}\n\n{hashtags}"
    ),
]

_CTAS: List[str] = [
    "Save this as a reminder 📌",
    "Share with someone who needs this today 🤍",
    "Tag a brother or sister ☁️",
    "Let this fill your heart with peace 🌙",
    "Read this and reflect 📖",
    "Share the blessing 🤍",
]


class CaptionAgent:
    """
    Generates bilingual Islamic Instagram captions using Mistral 7B via Ollama.
    """

    def __init__(self):
        self.config = get_config()
        self.flags = get_flags()
        ollama_cfg = self.config.get("ollama", {})
        self.base_url: str = ollama_cfg.get("base_url", "http://localhost:11434")
        self.model: str = ollama_cfg.get("model", "mistral")
        self.auto_pull: bool = ollama_cfg.get("auto_pull", True)
        self.temperature: float = float(ollama_cfg.get("temperature", 0.7))
        self.max_tokens: int = int(ollama_cfg.get("max_tokens", 300))

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self, topic: str, image_metadata: Dict) -> str:
        """
        Generate a caption for the given topic and image.

        Args:
            topic:          Current trending topic (e.g. "Quran reflection").
            image_metadata: Image dict from DB (used for context; currently unused
                            in the prompt but available for future expansion).

        Returns:
            Caption string ready for Instagram.
        """
        # Hot-reload flags each run
        self.config = get_config()
        self.flags = get_flags()

        ollama_cfg = self.config.get("ollama", {})
        self.base_url = ollama_cfg.get("base_url", "http://localhost:11434")
        self.model = ollama_cfg.get("model", "mistral")
        self.auto_pull = ollama_cfg.get("auto_pull", True)

        if not self.flags.auto_caption:
            logger.info("auto_caption=false — using template caption")
            return self._template_caption(topic)

        try:
            self._ensure_model_available()
            caption = self._generate_with_ollama(topic)
            if caption:
                logger.info(f"Ollama caption generated ({len(caption)} chars)")
                return caption
        except Exception as exc:
            logger.error(f"Ollama caption generation failed: {exc}")

        logger.warning("Falling back to template caption")
        return self._template_caption(topic)

    # ── Model availability ────────────────────────────────────────────────────

    def _ensure_model_available(self) -> None:
        """
        Check Ollama for the configured model.
        Auto-pulls it if missing and auto_pull=true.

        Raises:
            RuntimeError: If Ollama is unreachable or model is missing and auto_pull=false.
        """
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=10)
            resp.raise_for_status()
        except requests.ConnectionError:
            raise RuntimeError(
                f"Cannot reach Ollama at {self.base_url}. "
                "Run: ollama serve"
            )

        models = [m.get("name", "") for m in resp.json().get("models", [])]
        model_found = any(self.model in m for m in models)

        if model_found:
            logger.debug(f"Model '{self.model}' is available in Ollama")
            return

        if not self.auto_pull:
            raise RuntimeError(
                f"Model '{self.model}' not found in Ollama and auto_pull=false. "
                f"Run: ollama pull {self.model}"
            )

        logger.info(f"Model '{self.model}' not found — auto-pulling (this may take a few minutes)...")
        self._pull_model()

    def _pull_model(self) -> None:
        """Stream-pull the model from Ollama, logging progress."""
        try:
            resp = requests.post(
                f"{self.base_url}/api/pull",
                json={"name": self.model},
                stream=True,
                timeout=600,      # 10 min timeout — model pulls can be slow
            )
            resp.raise_for_status()

            for line in resp.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(f"Pull error: {data['error']}")
                status = data.get("status", "")
                if status:
                    logger.info(f"[ollama pull] {status}")

            logger.info(f"Model '{self.model}' pulled successfully")

        except Exception as exc:
            raise RuntimeError(f"Failed to pull model '{self.model}': {exc}") from exc

    # ── Caption generation ────────────────────────────────────────────────────

    @retry(
        max_attempts=2,
        backoff_factor=2,
        initial_wait=3.0,
        exceptions=(requests.RequestException,),
    )
    def _generate_with_ollama(self, topic: str) -> Optional[str]:
        """
        Send a structured prompt to Ollama and return the caption.
        """
        hashtags = self._get_hashtags()
        sample_tags = " ".join(hashtags[:10])

        prompt = f"""You are writing an Instagram caption for an Islamic cinematic content account.
Topic: "{topic}"

Write the caption using EXACTLY this structure (no extra text, no greetings):

[Line 1-2]: 1-2 lines of Arabic — a relevant Quranic verse or authentic dhikr
[blank line]
[Lines 3-5]: English reflection — 2-3 sentences, spiritual and peaceful tone, not political
[blank line]
[Line 6]: A short call to action (e.g. "Save this as a reminder 📌")
[blank line]
[Line 7]: Hashtags — include these: {sample_tags}

Important: Keep it universally Islamic, peaceful, and inspiring."""

        resp = requests.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.max_tokens,
                },
            },
            timeout=120,
        )
        resp.raise_for_status()

        caption = resp.json().get("response", "").strip()

        if not caption:
            return None

        # Ensure hashtags are present — append if LLM forgot
        if not any(word.startswith("#") for word in caption.split()):
            caption += f"\n\n{' '.join(hashtags[:20])}"

        # Enforce Instagram 2200-char caption limit
        if len(caption) > 2100:
            caption = caption[:2100].rsplit("\n", 1)[0] + "\n..."

        return caption

    # ── Hashtag selection ─────────────────────────────────────────────────────

    def _get_hashtags(self) -> List[str]:
        """
        Return an ordered list of hashtags.
        Prioritizes terms from high-performing posts (engagement DB),
        supplemented with defaults to maintain freshness.
        """
        max_tags: int = self.config.get("caption", {}).get("max_hashtags", 25)
        try:
            db_tags = get_top_hashtags_by_reach(limit=max_tags)
            if db_tags:
                # Merge: DB-learned tags first, fill remaining from defaults
                merged = db_tags[:]
                for tag in _DEFAULT_HASHTAGS:
                    if tag not in merged:
                        merged.append(tag)
                return merged[:max_tags]
        except Exception as exc:
            logger.debug(f"Could not load hashtags from DB: {exc}")

        return _DEFAULT_HASHTAGS[:max_tags]

    # ── Template fallback ─────────────────────────────────────────────────────

    def _template_caption(self, topic: str) -> str:
        """Generate a curated template caption (no LLM required)."""
        template = random.choice(_TEMPLATE_CAPTIONS)
        cta = random.choice(_CTAS)
        hashtags = " ".join(self._get_hashtags()[:20])
        return template.format(topic=topic, cta=cta, hashtags=hashtags)
