"""
core/caption_engine.py — Rule-based Instagram Caption Engine.

Pipeline:
  1. Clean    — strip hashtags, normalize whitespace
  2. Classify — keyword scoring → category
  3. Build    — [PRE-HOOK CTA] + [HOOK] + [BODY] + [EMOTIONAL LINE] + [HASHTAGS]

Caption structure:
  Line 1 (PRE-HOOK): Bilingual direct ask — "👇 Comment 'آمين' if this is you"
                     Visible before "...more" — drives engagement immediately.
  Line 2 blank
  Line 3+ (HOOK): Short, punchy opener (same category)
  blank
  (BODY): Original caption, cleaned
  blank
  (EMOTIONAL LINE): Quranic/Hadith reference with Arabic
  blank
  (HASHTAGS): Category-specific
  (CREDIT): Optional "Via @handle"

One CTA per caption (the pre-hook). No separate CTA at bottom.
All components drawn from the SAME category — never mixed.

Categories: sabr | shukr | tawakkul | akhirah | dua | general
"""

import re
import random
from typing import Optional

# ── Category keyword banks (lowercase) ────────────────────────────────────────
_KEYWORDS: dict[str, list[str]] = {
    "sabr": [
        "pain", "struggle", "test", "patience", "hardship", "trial",
        "difficult", "burden", "suffering", "sabr", "bear", "endure",
        "ease after", "darkness", "wound", "broken", "heartbreak", "hurt",
    ],
    "shukr": [
        "gratitude", "blessing", "rizq", "thankful", "grateful", "alhamdulillah",
        "bounty", "favour", "mercy", "provision", "gift", "appreciate",
        "shukr", "contentment", "satisfied",
    ],
    "tawakkul": [
        "trust", "control", "plan", "rely", "depend", "tawakkul",
        "allah's plan", "let go", "worry", "outcome", "overthink",
        "surrender", "put your trust", "in allah we trust", "leave it to allah",
    ],
    "akhirah": [
        "death", "jannah", "grave", "hereafter", "paradise", "akhirah",
        "afterlife", "eternal", "day of judgement", "qiyamah", "duniya",
        "dunya", "temporary", "world is temporary", "accountability",
    ],
    "dua": [
        "pray", "dua", "forgive", "supplication", "ask allah", "make dua",
        "du'a", "prayer", "supplicate", "raise your hands", "3am", "night prayer",
        "ameen", "ya allah", "forgiveness", "accepted",
    ],
}

# ── Per-category content pools ─────────────────────────────────────────────────

# PRE-HOOK: The very first line — direct English engagement ask.
# Shown before "...more" — drives comments immediately.
# Keep the comment word simple English so people actually type it.
_PRE_HOOKS: dict[str, list[str]] = {
    "sabr": [
        "👇 Comment 'Ameen' if you needed this today",
        "👇 Type 'Ameen' if you're going through a test right now",
        "👇 Comment 'Ameen' — this is for the ones still holding on 🤲",
        "👇 Say 'Ameen' if Allah is carrying you through something hard 🌙",
    ],
    "shukr": [
        "👇 Comment 'Alhamdulillah' if you're grateful today 🤍",
        "👇 Type 'Alhamdulillah' — let's fill the comments with gratitude",
        "👇 Comment 'Alhamdulillah' if Allah has blessed you today ✨",
        "👇 Say 'Alhamdulillah' if you woke up with more than you deserve 🌙",
    ],
    "tawakkul": [
        "👇 Comment 'Ameen' if you're trusting Allah's plan today 🌙",
        "👇 Type 'Ameen' if you're learning to let go and trust Allah",
        "👇 Comment 'Ameen' if you needed this reminder right now 🤍",
        "👇 Say 'Ameen' if you're leaving it all to Allah today ✨",
    ],
    "akhirah": [
        "👇 Share this before scrolling — someone needs to see this 🕌",
        "👇 Comment 'Ameen' if you're preparing for what truly matters 🌙",
        "👇 Tag someone who needs this reminder today 🤍",
        "👇 Comment 'Ameen' — the akhirah is closer than we think",
    ],
    "dua": [
        "👇 Comment 'Ameen' and make a du'a right now 🤲",
        "👇 Type 'Ameen' — let's all raise our hands together 🌙",
        "👇 Comment your du'a below — Allah is always listening 🤍",
        "👇 Say 'Ameen' if you're in need of Allah's mercy today",
    ],
    "general": [
        "👇 Comment 'Ameen' if this speaks to you today 🤍",
        "👇 Type 'SubhanAllah' if this touched your heart ✨",
        "👇 Comment 'Ameen' — share this with someone who needs it 🌙",
        "👇 Say 'SubhanAllah' if you needed this reminder today",
    ],
}

_HOOKS: dict[str, list[str]] = {
    "sabr": [
        "سبحان الله — Every hardship is a test from Allah. 🤲",
        "Allah tests those He loves most — الصبر جميل. 🌙",
        "Sabr is not silence — it's trusting Allah completely. 🤍",
        "The pain you feel today is shaping you for tomorrow. ✨",
        "إن مع العسر يسرا — After hardship comes ease. Always. 🌙",
    ],
    "shukr": [
        "الحمد لله — Alhamdulillah for what you have right now. 🤍",
        "Count your blessings — they outweigh your burdens. ✨",
        "Gratitude is the language of the believer — شكر الله. 🌙",
        "Your rizq is written — trust Allah's provision. 🤲",
        "الحمد لله على كل حال — Say Alhamdulillah, always. 🤍",
    ],
    "tawakkul": [
        "توكّل على الله — Let go. Trust Allah's plan. 🌙",
        "Stop worrying — Allah is already planning for you. 🤍",
        "You don't control the outcome. Only your effort. ✨",
        "حسبي الله ونعم الوكيل — Allah is enough. 🤲",
        "Release the grip. Trust the One who holds everything. 🌙",
    ],
    "akhirah": [
        "الدنيا فانية — This world is temporary. Build for what lasts. 🕌",
        "Jannah is the goal. This dunya is the test. 🌙",
        "The grave will come for all of us. Are you ready? 🤍",
        "كل نفس ذائقة الموت — Every soul shall taste death. ✨",
        "What are you building for the day it truly matters? 🌙",
    ],
    "dua": [
        "ادعوني أستجب لكم — Make du'a. Allah always listens. 🤲",
        "Your du'a reaches Allah even at 3am. 🌙",
        "Never underestimate the power of your supplication. 🤍",
        "Speak to Allah — He hears every word. ✨",
        "وإذا سألك عبادي — He is near. Always near. 🤲",
    ],
    "general": [
        "سبحان الله — A reminder every Muslim needs to read. 🤍",
        "SubhanAllah — let this sink in. 🌙",
        "Read this twice. Share it once. 🤍",
        "ما شاء الله — May this reminder reach the heart that needs it. ✨",
        "This is for the one who needed it today. 🌙",
    ],
}

_EMOTIONAL_LINES: dict[str, list[str]] = {
    "sabr": [
        "«لَا يُكَلِّفُ اللَّهُ نَفْسًا إِلَّا وُسْعَهَا» — Allah does not burden a soul beyond what it can bear. (2:286)",
        "«إِنَّ مَعَ الْعُسْرِ يُسْرًا» — Indeed, with every hardship comes ease. (94:6)",
        "Your sabr is not wasted — Allah sees every tear, every silent prayer.",
        "The most rewarded in the akhirah are those who were tested the most in this dunya.",
    ],
    "shukr": [
        "«لَئِن شَكَرْتُمْ لَأَزِيدَنَّكُمْ» — If you are grateful, I will surely increase you. (14:7)",
        "Shukr is not just words — it's a way of living, a way of seeing the world.",
        "The believer who is grateful is always in a state of increase.",
        "«وَإِن تَعُدُّوا نِعْمَةَ اللَّهِ لَا تُحْصُوهَا» — You cannot count Allah's blessings. (16:18)",
    ],
    "tawakkul": [
        "«وَمَن يَتَوَكَّلْ عَلَى اللَّهِ فَهُوَ حَسْبُهُ» — Whoever relies upon Allah, He is sufficient. (65:3)",
        "Tawakkul is doing your best, then leaving the rest to Allah completely.",
        "Allah's timing is always perfect — even when it doesn't feel like it.",
        "Trust is not passive. It is the highest form of faith in Allah's plan.",
    ],
    "akhirah": [
        "«كُلُّ نَفْسٍ ذَائِقَةُ الْمَوْتِ» — Every soul shall taste death. (3:185) Prepare while you can.",
        "What you plant in this dunya, you will harvest in the akhirah.",
        "The best investment is the one that follows you into your grave — your deeds.",
        "This life is a bridge — don't build your home on it.",
    ],
    "dua": [
        "«ادْعُونِي أَسْتَجِبْ لَكُمْ» — Call upon Me, I will respond to you. (40:60)",
        "A believer's most powerful weapon is du'a — never leave it.",
        "«وَإِذَا سَأَلَكَ عِبَادِي عَنِّي فَإِنِّي قَرِيبٌ» — I am near. (2:186) Always.",
        "Allah loves those who ask Him persistently and with certainty.",
    ],
    "general": [
        "May Allah fill your heart with peace — «السَّلَامُ» is one of His beautiful names.",
        "«وَمَا أَرْسَلْنَاكَ إِلَّا رَحْمَةً لِّلْعَالَمِينَ» — Islam is a mercy to all of mankind. (21:107)",
        "Return to Allah — He is always waiting for you, no matter how far you've gone.",
        "Every moment is a chance to start again. That is the mercy of Allah.",
    ],
}

_HASHTAGS: dict[str, str] = {
    "sabr": (
        "#Sabr #Patience #Islam #Muslim #IslamicReminder #Quran #Allah "
        "#Alhamdulillah #Hardship #Tawakkul #SubhanAllah #IslamicQuotes "
        "#ImanBooster #IslamicPost #Faith"
    ),
    "shukr": (
        "#Shukr #Alhamdulillah #Gratitude #Rizq #Islam #Muslim #Quran "
        "#Allah #Barakah #IslamicPost #IslamicReminder #Blessing "
        "#IslamicQuotes #SubhanAllah #Tawakkul"
    ),
    "tawakkul": (
        "#Tawakkul #Trust #AllahsPlan #Islam #Muslim #Quran #Allah "
        "#SubhanAllah #Iman #IslamicPost #IslamicReminder #IslamicQuotes "
        "#Faith #Sabr #Deen"
    ),
    "akhirah": (
        "#Akhirah #Jannah #Islam #Muslim #Quran #Allah #IslamicReminder "
        "#Hereafter #Deen #IslamicPost #IslamicQuotes #SubhanAllah "
        "#Salah #Iman #LastDay"
    ),
    "dua": (
        "#Dua #Prayer #Islam #Muslim #Quran #Allah #MakeDua "
        "#IslamicReminder #IslamicPost #Dhikr #SubhanAllah #Alhamdulillah "
        "#AllahuAkbar #Ameen #Iman"
    ),
    "general": (
        "#Islam #Islamic #Quran #Allah #Muslim #Hadith #ProphetMuhammad "
        "#IslamicQuotes #Iman #Tawakkul #Sabr #Dhikr #Jannah #Deen "
        "#IslamicReminder #IslamicPost #SubhanAllah #AllahuAkbar #Alhamdulillah"
    ),
}


# ── Public API ────────────────────────────────────────────────────────────────

def clean_caption(text: str) -> str:
    """
    Remove hashtags and normalize whitespace.
    Keeps emojis and Arabic text intact — they add authentic personality.
    Returns cleaned body text only.
    """
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [l.rstrip() for l in text.splitlines()]
    return "\n".join(lines).strip()


def classify_caption(text: str) -> str:
    """
    Score the cleaned caption against each category's keyword bank.
    Returns the highest-scoring category name, or 'general' if no match.
    """
    lower = text.lower()
    scores: dict[str, int] = {cat: 0 for cat in _KEYWORDS}

    for category, keywords in _KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                scores[category] += 1

    best_cat = max(scores, key=lambda c: scores[c])
    return best_cat if scores[best_cat] > 0 else "general"


def build_caption(
    original: str,
    add_credit: bool = True,
    credit_handle: str = "softeningsayings",
) -> str:
    """
    Full pipeline:
      1. Clean the original caption (strip hashtags, normalize whitespace)
      2. Classify into a category via keyword scoring
      3. Assemble:
           [PRE-HOOK CTA]   ← bilingual direct ask, shown before "...more"
           [HOOK]           ← short punchy opener
           [BODY]           ← cleaned original
           [EMOTIONAL LINE] ← Quranic/Hadith reference with Arabic
           [HASHTAGS]       ← category-specific
           [CREDIT]         ← optional
      4. All components come from the SAME category — never mixed.
      5. One CTA only (the pre-hook). No repeat at the bottom.
    """
    body = clean_caption(original)
    category = classify_caption(body)

    pre_hook = random.choice(_PRE_HOOKS[category])
    hook = random.choice(_HOOKS[category])
    emotional = random.choice(_EMOTIONAL_LINES[category])
    hashtags = _HASHTAGS[category]

    parts = [
        pre_hook,    # "👇 Comment 'آمين' if..."
        "",
        hook,        # "Every hardship is a test from Allah."
        "",
        body,        # original caption (cleaned)
        "",
        emotional,   # Quranic reference + Arabic
        "",
        hashtags,
    ]

    if add_credit:
        parts += ["", f"Via @{credit_handle} \U0001f90d"]

    return "\n".join(parts)
