"""Photo/text hazard report -> moderation -> Mistral vision classification -> duplicate check in ES -> SF311 draft.

Nothing is filed automatically: the resident confirms and sends it through the official SF311 flow.
"""

import base64
import json

from highground.feeds import classify_text

LABELS = {"flooding": "Flooding (street or sidewalk)", "catch_basin": "Clogged storm drain / catch basin", "sewer_backup": "Sewer backup or overflow",
          "tree": "Fallen or damaged tree", "water_leak": "Water leak", "other": "Other street hazard"}  # fmt: skip
SF311 = "https://sf311.org"
PROMPT = ("You classify San Francisco street-hazard reports for SF311. Look at the photo (if any) and the text. Reply as JSON with keys: "
          '"category" (one of ' + ", ".join(LABELS) + '), "water_visible" (true/false/null), "severity" ("low","medium","high"), '
          '"description" (one factual sentence for a city crew, <= 200 chars, in English, no personal data), "confidence" (0-1).')  # fmt: skip


def duplicates(s, lat: float, lon: float, category: str) -> list[dict]:
    r = s.search("reports", size=5, sort=[{"_geo_distance": {"location": {"lat": lat, "lon": lon}, "order": "asc", "unit": "m"}}],
                 query={"bool": {"filter": [{"geo_distance": {"distance": "75m", "location": {"lat": lat, "lon": lon}}},
                                            {"range": {"requested_at": {"gte": "now-3h"}}}, {"terms": {"category": [category, "flooding", "catch_basin"]}}]}},
                 _source=["category", "address", "requested_at", "status"])  # fmt: skip
    return [{**h["_source"], "distance_m": round(h["sort"][0])} for h in r["hits"]["hits"]]


def draft(s, lat: float, lon: float, text: str, image: bytes | None, mime: str | None, audio: bytes | None = None) -> dict:
    cfg = s.cfg
    by, vision, cat, desc = "keywords", None, classify_text(text), (text or "").strip()[:200]
    if cfg.has_mistral:
        from mistralai.client import Mistral

        m = Mistral(api_key=cfg.mistral_api_key, timeout_ms=25000)
        if audio:  # Voxtral speech-to-text, any language
            t = m.audio.transcriptions.complete(model=cfg.model_transcribe, file={"content": audio, "file_name": "report.webm"})
            text = f"{text} {t.text}".strip()
        if text:
            mod = m.classifiers.moderate(model=cfg.model_moderation, inputs=[text])
            flagged = [k for k, v in (mod.results[0].categories or {}).items() if v and k not in ("violence_and_threats",)]
            if flagged:
                return {"blocked": True, "message": "This report can't be posted. If someone is in danger, call 911.", "flags": flagged}
        content = [{"type": "text", "text": f"Report text: {text or '(none)'}"}]
        if image:
            content.append({"type": "image_url", "image_url": f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(image).decode()}"})
        try:
            r = m.chat.complete(model=cfg.model_large, temperature=0, response_format={"type": "json_object"}, max_tokens=300,
                                messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": content}])  # fmt: skip
            out = json.loads(r.choices[0].message.content)
            if out.get("category") in LABELS:
                cat, desc, by = out["category"], (out.get("description") or desc)[:200], cfg.model_large
                vision = {k: out.get(k) for k in ("water_visible", "severity", "confidence")} if image else None
        except Exception:  # noqa: BLE001 - fall back to keyword classification
            pass
    return {"category": cat, "by": by, "vision": vision, "duplicates": duplicates(s, lat, lon, cat),
            "draft": {"category_label": LABELS.get(cat, LABELS["other"]), "description": desc or LABELS.get(cat, ""), "lat": lat, "lon": lon},
            "handoff_url": SF311, "adopt_a_drain": "https://adoptadrain.sfwater.org/" if cat in ("catch_basin", "flooding") else None}  # fmt: skip
