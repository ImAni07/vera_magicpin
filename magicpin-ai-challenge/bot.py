#!/usr/bin/env python3

"""
Deterministic Vera message engine for the magicpin AI Challenge.

Run locally:
    python bot.py

The module also exposes compose(category, merchant, trigger, customer=None).
It uses only the Python standard library so it can run in a fresh environment.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


HOST = "0.0.0.0"
PORT = 8080
START = time.time()

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, dict[str, Any]] = {}
sent_suppression_keys: set[str] = set()
merchant_suppressed: dict[str, str] = {}
auto_reply_counts: dict[tuple[str, str], int] = {}


TEAM_METADATA = {
    "team_name": "Vera Deterministic Composer",
    "team_members": ["Anirban Majumder"],
    "model": "no external model - deterministic rules",
    "approach": (
        "trigger router plus category-specific copy blocks, context fact extraction, "
        "dedupe suppression, and reply intent handling"
    ),
    "contact_email": "",
    "version": "1.0.0",
    "submitted_at": "2026-05-02T00:00:00Z",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean(value: Any) -> str:
    """Normalize challenge-pack mojibake and keep copy WhatsApp-friendly."""
    if value is None:
        return ""
    text = str(value)
    replacements = {
        "\u00e2\u201a\u00b9": "Rs ",
        "\u20b9": "Rs ",
        "\u00e2\u20ac\u201d": "-",
        "\u00e2\u20ac\u201c": "-",
        "\u2014": "-",
        "\u2013": "-",
        "\u00e2\u2020\u2019": "->",
        "\u2192": "->",
        "\u00e2\u02dc\u2026": "star",
        "\u2605": "star",
        "\u00f0\u0178\u00a6\u00b7": "",
        "\u00f0\u0178\u2019\u008d": "",
        "\u00f0\u0178\u2018\u2039": "",
        "\u00f0\u0178\u2122\u008f": "",
        "\u00c2": "",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("Rs  ", "Rs ")
    return text


def pct(value: Any, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    if abs(number) <= 1:
        number *= 100
    sign = "+" if signed and number > 0 else ""
    if abs(number - round(number)) < 0.05:
        return f"{sign}{number:.0f}%"
    return f"{sign}{number:.1f}%"


def pct_abs(value: Any) -> str:
    try:
        number = abs(float(value))
    except (TypeError, ValueError):
        return "?"
    return pct(number)


def first_name(merchant: dict[str, Any]) -> str:
    ident = merchant.get("identity", {})
    owner = clean(ident.get("owner_first_name"))
    if owner:
        return owner.replace("Dr. ", "").replace("Dr ", "").strip()
    name = clean(ident.get("name", "there"))
    return name.split()[0].replace(",", "")


def salutation(category: dict[str, Any], merchant: dict[str, Any]) -> str:
    slug = category.get("slug") or merchant.get("category_slug", "")
    owner = first_name(merchant)
    if slug == "dentists":
        return owner if owner.lower().startswith("dr") else f"Dr. {owner}"
    return owner or clean(merchant.get("identity", {}).get("name", "there"))


def merchant_place(merchant: dict[str, Any]) -> str:
    ident = merchant.get("identity", {})
    locality = clean(ident.get("locality"))
    city = clean(ident.get("city"))
    if locality and city:
        return f"{locality}, {city}"
    return locality or city


def active_offers(merchant: dict[str, Any]) -> list[str]:
    offers = []
    for offer in merchant.get("offers", []) or []:
        if offer.get("status") == "active":
            title = clean(offer.get("title"))
            if title:
                offers.append(title)
    return offers


def best_offer(merchant: dict[str, Any], category: dict[str, Any] | None = None) -> str:
    offers = active_offers(merchant)
    if offers:
        return offers[0]
    if category:
        for offer in category.get("offer_catalog", []) or []:
            title = clean(offer.get("title"))
            if title and "Flat 30%" not in title:
                return title
    return ""


def choose_offer(merchant: dict[str, Any], category: dict[str, Any] | None,
                keywords: list[str]) -> str:
    all_offers = active_offers(merchant)
    if category:
        all_offers += [clean(o.get("title")) for o in category.get("offer_catalog", []) or []]
    lowered = [(offer, offer.lower()) for offer in all_offers if offer]
    for keyword in keywords:
        for offer, low in lowered:
            if keyword in low:
                return offer
    return best_offer(merchant, category)


def peer_ctr(category: dict[str, Any]) -> str:
    ctr = category.get("peer_stats", {}).get("avg_ctr")
    return pct(ctr) if ctr is not None else ""


def merchant_ctr(merchant: dict[str, Any]) -> str:
    ctr = merchant.get("performance", {}).get("ctr")
    return pct(ctr) if ctr is not None else ""


def customer_name(customer: dict[str, Any] | None) -> str:
    if not customer:
        return ""
    raw = clean(customer.get("identity", {}).get("name", "there"))
    if "(" in raw:
        return raw.split("(")[0].strip()
    return raw or "there"


def wants_hinglish(customer: dict[str, Any] | None, merchant: dict[str, Any] | None = None) -> bool:
    if customer:
        pref = clean(customer.get("identity", {}).get("language_pref", "")).lower()
        if "hi" in pref or "hinglish" in pref:
            return True
    langs = (merchant or {}).get("identity", {}).get("languages", []) or []
    return "hi" in langs and len(langs) <= 2


def has_customer_consent(customer: dict[str, Any] | None, trigger: dict[str, Any]) -> bool:
    if not customer:
        return True
    prefs = customer.get("preferences", {}) or {}
    if prefs.get("reminder_opt_in") is False:
        return False
    consent = customer.get("consent", {}) or {}
    scopes = consent.get("scope", []) or []
    if not consent.get("opted_in_at") and not scopes:
        return False
    kind = trigger.get("kind", "")
    if kind in {"recall_due", "appointment_tomorrow"}:
        return any("reminder" in s or "appointment" in s for s in scopes)
    if kind in {"customer_lapsed_hard", "customer_lapsed_soft", "trial_followup"}:
        return any("winback" in s or "promotional" in s or "program" in s for s in scopes)
    if kind == "chronic_refill_due":
        return any("refill" in s or "delivery" in s for s in scopes)
    return True


def find_digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any] | None:
    payload = trigger.get("payload", {}) or {}
    top = payload.get("top_item")
    if isinstance(top, dict):
        return top
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    digest = category.get("digest", []) or []
    if wanted:
        for item in digest:
            if item.get("id") == wanted:
                return item
    kind = trigger.get("kind", "")
    kind_map = {
        "research_digest": "research",
        "regulation_change": "compliance",
        "cde_opportunity": "cde",
        "category_seasonal": "seasonal",
    }
    target = kind_map.get(kind)
    if target:
        for item in digest:
            if item.get("kind") == target:
                return item
    return digest[0] if digest else None


def compact_source(source: str) -> str:
    source = clean(source)
    return f" - {source}" if source else ""


def template_name(kind: str, customer: dict[str, Any] | None) -> str:
    side = "customer" if customer else "merchant"
    safe = re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_") or "generic"
    return f"vera_{side}_{safe}_v1"


def make_conversation_id(merchant_id: str, trigger_id: str, customer_id: str | None = None) -> str:
    raw = f"{merchant_id}:{trigger_id}:{customer_id or ''}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    kind = trigger_id.split("_")[2] if "_" in trigger_id else "ctx"
    return f"conv_{merchant_id[:18]}_{kind}_{digest}"


def compose(category: dict[str, Any], merchant: dict[str, Any],
            trigger: dict[str, Any], customer: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return body, cta, send_as, suppression_key, and rationale."""
    if customer:
        return compose_customer(category, merchant, trigger, customer)
    return compose_merchant(category, merchant, trigger)


def compose_customer(category: dict[str, Any], merchant: dict[str, Any],
                    trigger: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    cname = customer_name(customer)
    mname = clean(merchant.get("identity", {}).get("name", "your store"))
    owner = salutation(category, merchant)
    offer = best_offer(merchant, category)
    relationship = customer.get("relationship", {}) or {}
    prefs = customer.get("preferences", {}) or {}
    body = ""
    cta = "binary_yes_no"
    rationale = "Customer-scoped trigger; message is sent as the merchant with consent-aware, relationship-specific context."

    if kind == "recall_due":
        slots = payload.get("available_slots", []) or []
        slot_joiner = " ya " if wants_hinglish(customer, merchant) else " or "
        slot_text = slot_joiner.join(clean(s.get("label")) for s in slots[:2] if s.get("label"))
        service = clean(payload.get("service_due", "recall")).replace("_", " ")
        last = clean(payload.get("last_service_date") or relationship.get("last_visit"))
        if wants_hinglish(customer, merchant):
            body = (
                f"Hi {cname}, {mname} here. Your {service} window is due"
                f"{f' after your last visit on {last}' if last else ''}. "
                f"Apke liye {slot_text or 'this week'} slot rakh sakte hain. "
                f"{offer + '. ' if offer else ''}Reply YES and I will hold it for you."
            )
        else:
            body = (
                f"Hi {cname}, {mname} here. Your {service} window is due"
                f"{f' after your last visit on {last}' if last else ''}. "
                f"{slot_text or 'This week'} is open. {offer + '. ' if offer else ''}"
                "Reply YES and I will hold the slot."
            )
        cta = "binary_yes_no"
        rationale = "Recall due is the strongest signal; uses service, visit timing, slots, and active offer without medical overclaim."

    elif kind in {"wedding_package_followup", "bridal_followup"}:
        days = payload.get("days_to_wedding") or prefs.get("days_to_wedding")
        trial = clean(payload.get("trial_completed") or relationship.get("last_visit"))
        slot = clean(prefs.get("preferred_slots", "preferred"))
        bridal_offer = choose_offer(merchant, category, ["bridal", "skin", "spa", "facial", "makeup"])
        body = (
            f"Hi {cname}, {owner} from {mname} here. {f'{days} days to your wedding' if days else 'Your bridal prep window is open'}"
            f"{f' after your trial on {trial}' if trial else ''}. "
            f"{bridal_offer + ' can be the first prep step. ' if bridal_offer else 'I can map the next prep step. '}"
            f"Want me to block your {slot.replace('_', ' ')} slot for next week?"
        )
        rationale = "Bridal follow-up uses wedding timing, trial relationship, and the customer's preferred slot."

    elif kind in {"customer_lapsed_hard", "customer_lapsed_soft"}:
        days = payload.get("days_since_last_visit")
        focus = clean(payload.get("previous_focus") or prefs.get("training_focus") or "").replace("_", " ")
        winback_offer = choose_offer(merchant, category, ["trial", "free", "first month", "demo"])
        if category.get("slug") == "gyms":
            body = (
                f"Hi {cname}, {owner} from {mname} here. "
                f"It's been {days} days since your last visit - no judgment. "
                f"{f'I remember your {focus} focus. ' if focus else ''}"
                f"{winback_offer or 'A free trial class'} is open this week. Reply YES and I will hold one spot, no auto-charge."
            )
        else:
            body = (
                f"Hi {cname}, {mname} here. It has been {days or 'a while'} since your last visit. "
                f"{offer + ' is available this week. ' if offer else ''}"
                "Reply YES and we will share one easy slot."
            )
        rationale = "Winback avoids guilt, references lapse/focus, and gives one low-risk yes/no action."

    elif kind == "chronic_refill_due":
        meds = [clean(x) for x in payload.get("molecule_list", [])]
        runs_out = clean(payload.get("stock_runs_out_iso", "")[:10])
        delivery = "Free home delivery to your saved address. " if payload.get("delivery_address_saved") else ""
        senior = "Senior discount applied. " if customer.get("identity", {}).get("senior_citizen") else ""
        namaste = "Namaste - " if wants_hinglish(customer, merchant) else "Hello - "
        body = (
            f"{namaste}{mname}. {cname}'s monthly medicines"
            f"{f' ({', '.join(meds)})' if meds else ''} run out on {runs_out or 'the due date'}. "
            f"Same-brand refill can be packed today. {senior}{delivery}Reply CONFIRM to dispatch."
        )
        cta = "binary_confirm_cancel"
        rationale = "Refill reminder uses exact molecules, due date, saved delivery status, and a precise confirm CTA."

    elif kind == "trial_followup":
        options = payload.get("next_session_options", []) or []
        slot = clean(options[0].get("label")) if options else clean(prefs.get("preferred_slots", "next session"))
        body = (
            f"Hi {cname}, {owner} from {mname} here. Hope the trial on {clean(payload.get('trial_date')) or 'your last visit'} felt useful. "
            f"The next slot is {slot}. Reply YES and I will reserve it."
        )
        rationale = "Trial follow-up uses the completed trial and next available session as the reply hook."

    elif kind == "appointment_tomorrow":
        slot = clean(payload.get("slot_label") or payload.get("appointment_time") or "tomorrow")
        body = (
            f"Hi {cname}, reminder from {mname}: your appointment is {slot}. "
            "Reply CONFIRM if this still works, or CHANGE if you need another slot."
        )
        cta = "binary_confirm_cancel"
        rationale = "Appointment reminder prioritizes confirmation and reschedule handling."

    else:
        last = clean(relationship.get("last_visit"))
        body = (
            f"Hi {cname}, {mname} here. Quick update based on your last visit"
            f"{f' on {last}' if last else ''}: {offer or 'we have a relevant slot open this week'}. "
            "Reply YES if you want me to share details."
        )
        rationale = "Generic customer trigger uses relationship history and avoids unsupported claims."

    return {
        "body": clean(body),
        "cta": cta,
        "send_as": "merchant_on_behalf",
        "suppression_key": clean(trigger.get("suppression_key") or f"{kind}:{customer.get('customer_id')}"),
        "rationale": rationale,
    }


def compose_merchant(category: dict[str, Any], merchant: dict[str, Any],
                    trigger: dict[str, Any]) -> dict[str, Any]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    slug = category.get("slug") or merchant.get("category_slug", "")
    who = salutation(category, merchant)
    place = merchant_place(merchant)
    perf = merchant.get("performance", {}) or {}
    agg = merchant.get("customer_aggregate", {}) or {}
    signals = merchant.get("signals", []) or []
    offer = best_offer(merchant, category)
    item = find_digest_item(category, trigger)
    body = ""
    cta = "binary_yes_no"
    rationale = "Merchant-facing trigger composed from the strongest current merchant and category signal."

    if kind == "research_digest":
        title = clean((item or {}).get("title", "a new category research item"))
        source = compact_source((item or {}).get("source", ""))
        trial_n = (item or {}).get("trial_n")
        segment = clean((item or {}).get("patient_segment", "")).replace("_", " ")
        high_risk = agg.get("high_risk_adult_count")
        metric = f" You have {high_risk} high-risk adult patients." if high_risk else ""
        if merchant_ctr(merchant) and peer_ctr(category):
            metric += f" CTR is {merchant_ctr(merchant)} vs peer {peer_ctr(category)}."
        body = (
            f"{who}, one useful read for {place}: {title}. "
            f"{f'{trial_n:,}-patient data. ' if isinstance(trial_n, int) else ''}"
            f"{metric} Want me to pull a 2-min abstract + draft a patient WhatsApp?{source}"
        )
        cta = "open_ended"
        rationale = "Research digest gets source-cited and tied to the merchant's cohort/performance before asking for one draft action."

    elif kind in {"regulation_change", "compliance_alert"}:
        title = clean((item or {}).get("title", payload.get("topic", "new compliance update")))
        source = compact_source((item or {}).get("source", ""))
        deadline = clean(payload.get("deadline_iso") or trigger.get("expires_at", "")[:10])
        action = clean((item or {}).get("actionable", "I can draft the checklist"))
        body = (
            f"{who}, compliance heads-up: {title}. Deadline: {deadline}. "
            f"{action}. Want me to turn this into a 5-point staff checklist?{source}"
        )
        rationale = "Compliance trigger prioritizes deadline, authority/source, and a low-friction operational checklist."

    elif kind == "supply_alert":
        batches = ", ".join(clean(x) for x in payload.get("affected_batches", []))
        molecule = clean(payload.get("molecule", "medicine"))
        maker = clean(payload.get("manufacturer"))
        chronic = agg.get("chronic_rx_count")
        body = (
            f"{who}, urgent stock check: {molecule} recall"
            f"{f' from {maker}' if maker else ''}{f' for batches {batches}' if batches else ''}. "
            f"Your repeat-Rx base is {chronic} customers. Want me to draft the customer note + replacement pickup workflow?"
        )
        rationale = "Supply alert uses exact molecule/batches and turns urgency into a concrete workflow."

    elif kind == "cde_opportunity":
        title = clean((item or {}).get("title", "CDE opportunity"))
        source = compact_source((item or {}).get("source", ""))
        credits = payload.get("credits") or (item or {}).get("credits")
        fee = clean(payload.get("fee") or (item or {}).get("actionable")).replace("_", " ")
        body = (
            f"{who}, quick CDE pick: {title}. "
            f"{f'{credits} credits; ' if credits else ''}{fee or 'details are in the calendar'}. "
            f"Want me to send a 3-line invite you can forward to your associate dentists?{source}"
        )
        rationale = "CDE trigger is low urgency, so the message uses professional value and a tiny forwarding task."

    elif kind == "competitor_opened":
        comp = clean(payload.get("competitor_name", "a nearby competitor"))
        dist = payload.get("distance_km")
        their_offer = clean(payload.get("their_offer"))
        opened = clean(payload.get("opened_date"))
        counter = offer or "your strongest current service"
        body = (
            f"{who}, {comp} opened {f'{dist} km away' if dist else 'nearby'}"
            f"{f' on {opened}' if opened else ''}{f' with {their_offer}' if their_offer else ''}. "
            f"Do not race to the bottom - use {counter} with your review/quality angle. Want me to draft that GBP post?"
        )
        rationale = "Competitor trigger avoids panic discounting and uses the merchant's real offer as the counter-position."

    elif kind == "ipl_match_today":
        match = clean(payload.get("match", "IPL match"))
        venue = clean(payload.get("venue"))
        active = offer or "your active food offer"
        weekend = payload.get("is_weeknight") is False
        if weekend:
            body = (
                f"{who}, {match}{f' at {venue}' if venue else ''} tonight. Saturday IPL tends to move demand to home-watch orders; "
                f"category data shows restaurant covers down 12% vs normal Saturdays. Push {active} as delivery-first, not dine-in. "
                "Want me to draft the banner + story copy?"
            )
        else:
            body = (
                f"{who}, {match}{f' at {venue}' if venue else ''} is a weeknight demand window. "
                f"Use {active} as a match-night combo and post by 5pm. Want me to draft it?"
            )
        rationale = "IPL trigger adds category judgment and uses the merchant's live offer instead of a generic match promo."

    elif kind == "active_planning_intent":
        topic = clean(payload.get("intent_topic", "new campaign")).replace("_", " ")
        if "thali" in topic:
            base_price = re.search(r"(\d+)", offer or "")
            price = int(base_price.group(1)) if base_price else 149
            body = (
                f"{who}, starter {topic} for offices near {place}: 10 thalis @ Rs {max(price - 20, 99)} each, "
                f"25 @ Rs {max(price - 30, 89)}, 50+ @ Rs {max(price - 40, 79)}. Orders by 5pm previous day; delivery 12:30-1pm. "
                "Reply CONFIRM and I will turn this into a 3-line WhatsApp pitch."
            )
        elif "kids" in topic or "yoga" in topic:
            body = (
                f"{who}, here is a clean {topic}: 4 weeks, 3 classes/week, age 7-12, weekend demo class first. "
                f"Anchor it with {offer or 'a first-month intro offer'} and cap the first batch at 12 kids. Reply CONFIRM and I will draft the GBP post."
            )
        else:
            body = (
                f"{who}, here is the first draft for {topic}: one clear package, one price anchor ({offer or 'your current hero offer'}), "
                "and one WhatsApp pitch. Reply CONFIRM and I will format it for posting."
            )
        cta = "binary_confirm_cancel"
        rationale = "Merchant already showed planning intent, so the bot switches to a concrete draft instead of more qualification."

    elif kind == "seasonal_perf_dip":
        metric = clean(payload.get("metric", "views"))
        delta = pct_abs(payload.get("delta_pct"))
        active_members = agg.get("total_active_members")
        note = "April-June is the low acquisition window for gyms" if slug == "gyms" else clean(payload.get("season_note", "seasonal dip"))
        body = (
            f"{who}, {metric} are down {delta} this week, but this looks seasonal: {note}. "
            f"{f'Protect your {active_members} active members first. ' if active_members else ''}"
            "Want me to draft a retention challenge instead of spending on ads now?"
        )
        rationale = "Seasonal dip is reframed to prevent overreaction and proposes the category-correct retention move."

    elif kind == "perf_dip":
        metric = clean(payload.get("metric", "calls"))
        delta = pct_abs(payload.get("delta_pct"))
        baseline = payload.get("vs_baseline")
        no_offer = "no_active_offers" in signals or not active_offers(merchant)
        body = (
            f"{who}, {metric} dropped {delta} over {clean(payload.get('window', '7d'))}"
            f"{f' from a {baseline} baseline' if baseline else ''}. "
            f"{'You have no active offer live. ' if no_offer else f'Your live hook is {offer}. '}"
            "Want me to draft one recovery post with a service+price CTA?"
        )
        rationale = "Performance dip uses the metric shift and selects the most fixable lever: offer/post recovery."

    elif kind == "perf_spike":
        metric = clean(payload.get("metric", "calls"))
        delta = pct(payload.get("delta_pct"), signed=True)
        driver = clean(payload.get("likely_driver")).replace("_", " ")
        body = (
            f"{who}, {metric} are up {delta} this week"
            f"{f' - likely from {driver}' if driver else ''}. "
            f"Let's compound it with {offer or 'your best intro offer'}. Want me to draft a follow-up GBP post for today?"
        )
        rationale = "Performance spike acts quickly while attention is warm and builds on the likely driver."

    elif kind == "renewal_due":
        days = payload.get("days_remaining") or merchant.get("subscription", {}).get("days_remaining")
        amount = payload.get("renewal_amount")
        body = (
            f"{who}, your {clean(payload.get('plan') or merchant.get('subscription', {}).get('plan', 'plan'))} renewal is in {days} days"
            f"{f' (Rs {amount})' if amount else ''}. Before you decide, I can show the last 30 days: "
            f"{perf.get('views', '?')} views, {perf.get('calls', '?')} calls, {perf.get('directions', '?')} directions. Want the 1-screen ROI summary?"
        )
        rationale = "Renewal trigger earns the ask with recent performance proof rather than a generic renewal nudge."

    elif kind in {"winback_eligible", "dormant_with_vera"}:
        days = payload.get("days_since_expiry") or payload.get("days_since_last_merchant_message") or merchant.get("subscription", {}).get("days_since_expiry")
        lapsed = payload.get("lapsed_customers_added_since_expiry") or agg.get("lapsed_90d_plus") or agg.get("lapsed_180d_plus")
        body = (
            f"{who}, it has been {days or '30+'} days since the last Vera action. "
            f"{f'{lapsed} customers are now lapsed. ' if lapsed else ''}"
            f"I can restart with one low-risk post using {offer or 'a service-price offer'}. Reply YES and I will draft it."
        )
        rationale = "Dormancy/winback uses elapsed time and lapsed customer count, then asks for one restart action."

    elif kind == "curious_ask_due":
        theme = (merchant.get("review_themes") or [{}])[0]
        clue = clean(theme.get("theme", "")).replace("_", " ")
        body = (
            f"{who}, quick check - what service is most asked-for this week at {clean(merchant.get('identity', {}).get('name'))}? "
            f"{f'I can pair it with your {clue} review theme and ' if clue else 'I can '}"
            "turn your answer into a Google post + 4-line WhatsApp reply. Takes 5 min."
        )
        cta = "open_ended"
        rationale = "Curious ask is intentionally low-stakes and invites merchant input while promising a useful artifact."

    elif kind == "review_theme_emerged":
        theme = clean(payload.get("theme", "review theme")).replace("_", " ")
        count = payload.get("occurrences_30d")
        quote = clean(payload.get("common_quote"))
        body = (
            f"{who}, {count or 'multiple'} reviews this month mention {theme}"
            f"{f' - \"{quote}\"' if quote else ''}. "
            "Want me to draft a public reply + one operations note for your staff?"
        )
        rationale = "Review theme trigger chooses the customer-visible issue and offers both response and ops fix."

    elif kind == "milestone_reached":
        metric = clean(payload.get("metric", "milestone")).replace("_", " ")
        now = payload.get("value_now")
        milestone = payload.get("milestone_value")
        body = (
            f"{who}, you are at {now} {metric} - close to {milestone}. "
            "A short thank-you post now can push the milestone without sounding needy. Want me to draft it?"
        )
        rationale = "Milestone trigger uses the exact count and keeps the action celebratory and simple."

    elif kind == "gbp_unverified":
        uplift = pct(payload.get("estimated_uplift_pct"))
        path = clean(payload.get("verification_path", "verification")).replace("_", " ")
        body = (
            f"{who}, your Google profile is still unverified. The profile data estimates {uplift} more visibility after verification. "
            f"Path: {path}. Want me to prepare the exact 3-step checklist?"
        )
        rationale = "Unverified GBP is a utility-first fix with estimated uplift and a concrete checklist."

    elif kind in {"category_seasonal", "festival_upcoming"}:
        if kind == "festival_upcoming":
            festival = clean(payload.get("festival", "festival"))
            days = payload.get("days_until")
            body = (
                f"{who}, {festival} is {days} days away for {place}. "
                f"{offer or 'Your strongest seasonal offer'} can be planned now without discount panic. Want me to draft the first festival post?"
            )
        else:
            trends = ", ".join(clean(x).replace("_", " ") for x in payload.get("trends", [])[:3])
            artifact = "shelf checklist" if slug == "pharmacies" else "menu/action checklist"
            body = (
                f"{who}, seasonal shift spotted: {trends or clean((item or {}).get('title', 'demand is changing'))}. "
                f"Want me to turn this into a {artifact} for this week?"
            )
        rationale = "Seasonal trigger uses timing/trend specifics and asks for one planning artifact."

    else:
        title = clean((item or {}).get("title") or payload.get("metric_or_topic") or kind.replace("_", " "))
        numbers = []
        for key, value in payload.items():
            if isinstance(value, (int, float)) and key not in {"placeholder"}:
                numbers.append(f"{key.replace('_', ' ')} {pct(value) if 'pct' in key or 'delta' in key else value}")
        fact = "; ".join(numbers[:2])
        body = (
            f"{who}, quick signal for {place}: {title}"
            f"{f' ({fact})' if fact else ''}. "
            f"{f'Your current hook is {offer}. ' if offer else ''}"
            "Want me to draft the next WhatsApp/Google post?"
        )
        rationale = "Fallback path uses the trigger label plus any numeric payload facts and avoids inventing missing details."

    return {
        "body": clean(body),
        "cta": cta,
        "send_as": "vera",
        "suppression_key": clean(trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}"),
        "rationale": rationale,
    }


def get_payload(scope: str, context_id: str | None) -> dict[str, Any] | None:
    if not context_id:
        return None
    entry = contexts.get((scope, context_id))
    return entry.get("payload") if entry else None


def action_from_trigger(trigger_id: str) -> dict[str, Any] | None:
    trigger = get_payload("trigger", trigger_id)
    if not trigger:
        return None
    merchant_id = trigger.get("merchant_id") or (trigger.get("payload", {}) or {}).get("merchant_id")
    merchant = get_payload("merchant", merchant_id)
    if not merchant:
        return None
    if merchant_id in merchant_suppressed:
        return None
    category = get_payload("category", merchant.get("category_slug"))
    if not category:
        return None
    customer_id = trigger.get("customer_id")
    customer = get_payload("customer", customer_id) if customer_id else None
    if trigger.get("scope") == "customer" and not customer:
        return None
    if customer and not has_customer_consent(customer, trigger):
        return None

    message = compose(category, merchant, trigger, customer)
    suppression_key = message["suppression_key"]
    if suppression_key in sent_suppression_keys:
        return None

    conversation_id = make_conversation_id(merchant_id, trigger.get("id", trigger_id), customer_id)
    action = {
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": message["send_as"],
        "trigger_id": trigger.get("id", trigger_id),
        "template_name": template_name(trigger.get("kind", ""), customer),
        "template_params": [salutation(category, merchant), trigger.get("kind", ""), suppression_key],
        "body": message["body"],
        "cta": message["cta"],
        "suppression_key": suppression_key,
        "rationale": message["rationale"],
    }
    sent_suppression_keys.add(suppression_key)
    conversations[conversation_id] = {
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "trigger_id": trigger.get("id", trigger_id),
        "last_body": message["body"],
        "turns": [{"from": "vera", "body": message["body"], "ts": utc_now()}],
        "auto_count": 0,
    }
    return action


def detect_auto_reply(message: str) -> bool:
    m = message.lower()
    phrases = [
        "thank you for contacting",
        "will respond shortly",
        "we will get back",
        "business hours",
        "automated assistant",
        "auto-reply",
        "auto reply",
        "team tak",
    ]
    return any(p in m for p in phrases)


def reply_response(body: dict[str, Any]) -> dict[str, Any]:
    conv_id = body.get("conversation_id", "")
    merchant_id = body.get("merchant_id") or conversations.get(conv_id, {}).get("merchant_id") or "unknown"
    message = clean(body.get("message", ""))
    lower = message.lower()
    conv = conversations.setdefault(conv_id, {"merchant_id": merchant_id, "turns": [], "auto_count": 0})
    conv.setdefault("turns", []).append({"from": body.get("from_role", "merchant"), "body": message, "ts": body.get("received_at")})

    hostile = ["stop", "not interested", "useless", "spam", "don't message", "dont message", "bothering", "unsubscribe", "abuse"]
    if any(word in lower for word in hostile):
        merchant_suppressed[merchant_id] = "merchant_negative_reply"
        return {"action": "end", "rationale": "Merchant gave a negative or opt-out signal; ending and suppressing future nudges."}

    if detect_auto_reply(message):
        key = (merchant_id, lower)
        auto_reply_counts[key] = auto_reply_counts.get(key, 0) + 1
        conv["auto_count"] = conv.get("auto_count", 0) + 1
        count = max(auto_reply_counts[key], conv["auto_count"])
        if count >= 3:
            return {"action": "end", "rationale": "Same auto-reply pattern repeated 3 times; closing to avoid burning turns."}
        if count == 1:
            return {
                "action": "wait",
                "wait_seconds": 14400,
                "rationale": "Detected WhatsApp Business auto-reply; backing off 4 hours for the owner/manager.",
            }
        return {
            "action": "wait",
            "wait_seconds": 86400,
            "rationale": "Repeated auto-reply with no human signal; waiting 24 hours before any retry.",
        }

    if any(word in lower for word in ["later", "busy", "tomorrow", "after some time", "call me later"]):
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Merchant asked for time; backing off for 30 minutes."}

    out_of_scope = ["gst", "tax", "filing", "loan", "rent agreement", "accounting"]
    if any(word in lower for word in out_of_scope):
        return {
            "action": "send",
            "body": "That part is best handled by your CA. Coming back to this Vera task: reply YES and I will prepare the draft/checklist from the signal above.",
            "cta": "binary_yes_no",
            "rationale": "Out-of-scope ask politely declined while redirecting to the active merchant-growth task.",
        }

    yes_words = ["yes", "ok", "okay", "go ahead", "do it", "send", "confirm", "let's do", "lets do", "sure", "please"]
    if any(word in lower for word in yes_words):
        return {
            "action": "send",
            "body": "Done - drafting it now. I will keep it short, use only the facts from your listing, and format it for WhatsApp/Google. Reply CONFIRM when you want me to send/publish the final copy.",
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant accepted; switching immediately from pitch to execution with a confirm step.",
        }

    no_words = ["no", "not now", "skip", "leave it"]
    if any(re.search(rf"\b{re.escape(word)}\b", lower) for word in no_words):
        return {"action": "end", "rationale": "Merchant declined the proposed action; ending without another nudge."}

    if "price" in lower or "cost" in lower or "kitna" in lower:
        merchant = get_payload("merchant", merchant_id) or {}
        category = get_payload("category", merchant.get("category_slug")) or {}
        offer = best_offer(merchant, category) or "the current offer from your catalog"
        return {
            "action": "send",
            "body": f"For this draft I will anchor on {offer} and avoid adding any fake discount. Reply CONFIRM and I will prepare the exact customer-facing line.",
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant asked about pricing; answering from the active offer/catalog and keeping the next step explicit.",
        }

    return {
        "action": "send",
        "body": "Got it. I can turn this into one ready-to-send draft using the same merchant facts. Reply YES and I will prepare it now.",
        "cta": "binary_yes_no",
        "rationale": "Ambiguous but non-negative reply; offers one low-friction next action without over-asking.",
    }


class VeraHandler(BaseHTTPRequestHandler):
    server_version = "VeraDeterministic/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/healthz":
            counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
            for scope, _ in contexts:
                counts[scope] = counts.get(scope, 0) + 1
            self._send({"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts})
            return
        if path == "/v1/metadata":
            self._send(TEAM_METADATA)
            return
        self._send({"error": "not_found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception as exc:
            self._send({"accepted": False, "reason": "malformed_json", "details": str(exc)}, 400)
            return

        if path == "/v1/context":
            scope = body.get("scope")
            context_id = body.get("context_id")
            version = body.get("version")
            payload = body.get("payload")
            if scope not in VALID_SCOPES:
                self._send({"accepted": False, "reason": "invalid_scope", "details": scope}, 400)
                return
            if not context_id or not isinstance(version, int) or not isinstance(payload, dict):
                self._send({"accepted": False, "reason": "invalid_context_body"}, 400)
                return
            key = (scope, context_id)
            current = contexts.get(key)
            if current and current.get("version", -1) >= version:
                self._send({"accepted": False, "reason": "stale_version", "current_version": current["version"]}, 409)
                return
            contexts[key] = {"version": version, "payload": payload, "delivered_at": body.get("delivered_at")}
            self._send({"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": utc_now()})
            return

        if path == "/v1/tick":
            trigger_ids = body.get("available_triggers", []) or []
            actions = []
            for trigger_id in trigger_ids:
                if len(actions) >= 20:
                    break
                action = action_from_trigger(trigger_id)
                if action:
                    actions.append(action)
            self._send({"actions": actions})
            return

        if path == "/v1/reply":
            self._send(reply_response(body))
            return

        if path == "/v1/teardown":
            contexts.clear()
            conversations.clear()
            sent_suppression_keys.clear()
            merchant_suppressed.clear()
            auto_reply_counts.clear()
            self._send({"accepted": True, "wiped_at": utc_now()})
            return

        self._send({"error": "not_found"}, 404)


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), VeraHandler)
    print(f"Vera deterministic bot listening on http://127.0.0.1:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()