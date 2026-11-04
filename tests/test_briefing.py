"""briefing.receipts(): keep only sentences whose numbers appear in the facts given to the model."""

from highground.briefing import receipts

FACTS = {
    "place": "Civic Center, San Francisco",
    "level": "high",
    "reasons": [{"text": "42.5 mm of rain in the last 3 hours", "value": 42.5, "unit": "mm"},
                {"text": "2 flooding/drain reports on or next to this block in the last 6 hours", "value": 2, "unit": "reports"}],
    "actions": ["Move your car out of the garage.", "Report flooding to SF311."],
    "history": {"flood_reports_5y": 7},
    "sandbags_available": 1234,
}


def test_receipts_drops_made_up_numbers():
    text = ("High risk of flooding at Civic Center right now. "
            "Rain reached 42.5 mm in the last 3 hours. "
            "The storm dropped 99 mm downtown, trust me. "
            "There were 2 reports nearby in the last 6 hours. "
            "Call 311 if you see water.")
    kept, dropped = receipts(text, FACTS)
    assert dropped == ["The storm dropped 99 mm downtown, trust me."]
    assert "42.5 mm" in kept and "2 reports nearby" in kept and "Call 311" in kept and "High risk" in kept
    assert "99 mm" not in kept


def test_receipts_keeps_numbers_present_in_facts():
    text = "7 flood reports on this block in 5 years. We have 1234 sandbags ready. Level is high."
    kept, dropped = receipts(text, FACTS)
    assert dropped == []
    assert kept == text


def test_receipts_comma_numbers_match_facts():
    text = "About 1,234 sandbags are available."
    kept, dropped = receipts(text, FACTS)
    assert dropped == [] and "1,234" in kept


def test_receipts_emergency_numbers_always_allowed():
    kept, dropped = receipts("Call 911 now. Dial 311. The year is 1.", {})
    assert dropped == [] and "911" in kept and "311" in kept


def test_receipts_empty_and_no_numbers():
    assert receipts("", FACTS) == ("", [])
    kept, dropped = receipts("Stay safe out there.", FACTS)
    assert kept == "Stay safe out there." and dropped == []


def test_receipts_drops_sentence_with_any_unknown_number():
    text = "Rain was 42.5 mm. Wind hit 88 km/h and rain was 42.5 mm."
    kept, dropped = receipts(text, FACTS)
    assert dropped == ["Wind hit 88 km/h and rain was 42.5 mm."]
    assert kept == "Rain was 42.5 mm."
