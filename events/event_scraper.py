#!/usr/bin/env python3
"""Best-effort, polite event collector for public event calendars.

Uses JSON-LD/schema.org Event data first, then common HTML cards. This is a
crawler framework, not a guarantee that every site exposes scrapeable data.
It does not bypass logins, CAPTCHAs, paywalls, bot controls, or access limits.
"""
from __future__ import annotations
import argparse, json, re, sys, time, hashlib, urllib.parse, urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

TZ = "Asia/Kolkata"
COUNTRY = "India"
# URL seeds can also be supplied via --urls-file. Keep site-specific changes in adapters().
DEFAULT_SEEDS = [
 "https://in.bookmyshow.com/explore/events-national-capital-region-ncr",
 "https://www.district.in/events/", "https://allevents.in/new-delhi", "https://luma.com/new-delhi",
 "https://www.delhievents.com/", "https://www.delhievents.com/search/label/Today?m=1", "https://www.delhievents.com/search/label/Tmrw?m=1", "https://www.delhievents.com/search/label/Wknd?m=1", "https://www.meetup.com/find/in--delhi/",
 "https://www.eventbrite.com/d/india--delhi--102030425/events-and-conferences-delhi-ncr/",
 "https://10times.com/newdelhi-in", "https://delhi2go.in/this-weekend", "https://www.itpo.gov.in/itpo-event",
 "https://www.itpo.gov.in/", "https://www.indiatradefair.com/",
 "https://yashobhoomi.org/events/yashobhoomi-events/", "https://yashobhoomi.org/",
 "https://www.iiccnewdelhi.com/public/convention-centre",
 "https://www.nsic.co.in/Schemes/ExhibitionComplexEventManagement", "https://indiaexpocentre.com/",
 "https://kamaniauditorium.org/schedule/index.php", "https://indiahabitat.org/Events",
 "https://indiahabitat.org/Programmes", "https://indiahabitat.org/Programme_venues", "https://iicdelhi.in/",
 "https://ignca.gov.in/", "https://nsd.gov.in/", "https://aksharatheatre.com/",
 "https://www.ltgdelhi.org/events", "https://www.trivenikalasangam.org/",
 "https://www.trivenikalasangam.org/spaces", "https://www.shriramcentre.org/", "https://www.ficci.in/",
 "https://www.aifacs.org.in/", "https://in.usembassy.gov/education-culture/american-spaces/american-center-new-delhi/",
 "https://www.britishcouncil.in/events", "https://afdelhi.org/events/",
 "https://www.goethe.de/ins/in/en/sta/del.html?wt_sc=newdelhi", "https://lokkalamanch.org/", 
 "https://sangeetnatak.gov.in/", "https://lalitkala.gov.in/", "https://sahitya-akademi.gov.in/",
 "https://www.knma.org/whats-on/", "https://www.nationalmuseumindia.gov.in/",
 "https://www.nationalmuseumindia.gov.in/en/regular-programmes", "https://ngmaindia.gov.in/",
 "https://www.ngmaindia.gov.in/plan-your-visit.asp", "https://nscd.gov.in/forthcoming-events/",
 "https://nrmindia.org/", "https://indiabiennale.culture.gov.in/", "https://indiabiennale.culture.gov.in/venues",
 "https://sportsauthorityofindia.nic.in/", "https://dff.nic.in/",
 "https://www.delhitourism.gov.in/tourist_place/dilli_haat_INA.html", "https://www.delhitourism.gov.in/",
 "https://sundernursery.org/", "https://asi.nic.in/", "https://theashok.com/", "https://www.tajhotels.com/",
 "https://www.itchotels.com/", "https://www.theleela.com/", "https://www.marriott.com/",
 "https://punjabiacademy.delhi.gov.in/dccws/calendar-activities-2026-27", "https://indiaculture.gov.in/",
 "https://www.pib.gov.in/", "https://www.visitdilli.com/events", "https://newdelhipulse.in/events/",
 "https://delhi2go.in/venue/bharat-mandapam", "https://delhi2go.in/venue/india-international-centre", "https://gurugram.gov.in/"
]

EVENT_HINT = re.compile(r"event|events|programme|program|calendar|schedule|what.?s.on|exhibition|conference|ticket|activity", re.I)
NCR_HINT = re.compile(r"delhi|ncr|gurugram|gurgaon|noida|ghaziabad|faridabad|greater-noida|dwarka|new.?delhi", re.I)
NCR_SOURCE_HOSTS = {'in.bookmyshow.com','www.district.in','allevents.in','luma.com','www.meetup.com','www.eventbrite.com','delhi2go.in','indiahabitat.org','iicdelhi.in','www.ltgdelhi.org','www.trivenikalasangam.org','afdelhi.org','www.knma.org','www.delhievents.com','www.iiccnewdelhi.com','yashobhoomi.org','www.yashobhoomi.org','newdelhipulse.in','www.visitdilli.com'}

def text(v: Any) -> str:
    if isinstance(v, list): return ", ".join(filter(None, (text(x) for x in v)))
    if isinstance(v, dict): return text(v.get("name") or v.get("@id") or "")
    return re.sub(r"\s+", " ", str(v or "")).strip()

def iso_date(v: Any) -> str | None:
    if not v: return None
    try:
        # ISO strings in schema.org may carry UTC offsets. Preserve local date text.
        m = re.match(r"^(\d{4}-\d\d-\d\d)", str(v))
        if m: return m.group(1)
        return dateparser.parse(str(v), fuzzy=True, dayfirst=True).date().isoformat()
    except Exception: return None

def iso_time(v: Any) -> str | None:
    if not v: return None
    try:
        m = re.search(r"T(\d{2}:\d{2})", str(v))
        if m: return m.group(1)
        dt = dateparser.parse(str(v), fuzzy=True)
        return dt.strftime("%H:%M") if re.search(r"\b\d{1,2}(:\d\d)?\s*(am|pm)\b|T\d", str(v), re.I) else None
    except Exception: return None

def dates_in_text(value: str) -> tuple[str | None, str | None]:
    """Parse common event-card dates, including dates with omitted year (e.g. Sat, 14 Nov)."""
    value = re.sub(r"\s+", " ", value or "")
    patterns = [
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\d{1,2}\s+[A-Za-z]{3,9}(?:\s+20\d{2})?\b",
        r"\b\d{1,2}\s+[A-Za-z]{3,9}(?:,?\s+20\d{2})?\b",
        r"\b[A-Za-z]{3,9}\s+\d{1,2}(?:,?\s+20\d{2})?\b",
    ]
    matches=[]
    for pat in patterns:
        matches = list(re.finditer(pat, value, re.I))
        if matches: break
    if not matches: return None, None
    def conv(raw):
        try:
            # ISO is unambiguous; don't pass YYYY-MM-DD through dayfirst parsing.
            iso=iso_date(raw)
            if iso and re.match(r'^20\d{2}-\d{2}-\d{2}$',raw): return iso
            dt=dateparser.parse(raw, fuzzy=True, dayfirst=True, default=datetime.now().replace(month=1,day=1))
            # If no explicit year was present, use current year, not the parser's default.
            if not re.search(r"20\d{2}", raw): dt=dt.replace(year=datetime.now().year)
            return dt.date().isoformat()
        except Exception: return None
    start=conv(matches[0].group(0))
    end=conv(matches[1].group(0)) if len(matches)>1 else start
    return start,end

def walk_jsonld(obj):
    if isinstance(obj, list):
        for x in obj: yield from walk_jsonld(x)
    elif isinstance(obj, dict):
        typ = obj.get("@type", [])
        types = [typ] if isinstance(typ, str) else typ
        if any("Event" in str(t) for t in types): yield obj
        for k, v in obj.items():
            if k != "@context": yield from walk_jsonld(v)

def canonical(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))

def clean_event_title(value: str) -> str:
    value=text(value)
    # Some listing anchors wrap the entire card (title, date, venue and price).
    value=re.sub(r"\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\d{1,2}\b.*$", "", value, flags=re.I)
    value=re.sub(r"\s+\d{1,2}:\d{2}\s*(?:AM|PM)\b.*$", "", value, flags=re.I)
    value=re.sub(r"\s+₹\s*[\d,]+.*$", "", value)
    return value.strip(" -–|·")

def slugify(s):
    slug=re.sub(r"[^a-z0-9]+", "-", text(s).lower()).strip("-")[:90].rstrip("-")
    return slug or "event"

def normalize_image_url(value: Any, base_url: str) -> str | None:
    """Return an absolute, usable image URL from Schema.org image shapes or HTML attributes."""
    if isinstance(value, list):
        for item in value:
            result = normalize_image_url(item, base_url)
            if result: return result
        return None
    if isinstance(value, dict):
        value = value.get("contentUrl") or value.get("url") or value.get("thumbnailUrl") or value.get("@id")
    if not isinstance(value, str): return None
    value=value.strip()
    if not value or value.startswith(('data:', 'blob:', 'javascript:')): return None
    value=urllib.parse.urljoin(base_url, value)
    p=urllib.parse.urlsplit(value)
    if p.scheme not in ('http','https') or not p.netloc: return None
    return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,p.query,''))

def image_from_element(element, base_url: str) -> str | None:
    """Choose the real lazy-loaded asset rather than a placeholder src."""
    if element is None: return None
    for attr in ("data-src", "data-lazy-src", "data-original", "data-image", "src"):
        candidate=normalize_image_url(element.get(attr),base_url)
        if candidate and not re.search(r"placeholder|spacer|blank\.gif|pixel",candidate,re.I): return candidate
    srcset=element.get("srcset") or element.get("data-srcset")
    if srcset:
        candidates=[]
        for part in srcset.split(","):
            bits=part.strip().split()
            if bits:
                m=re.search(r"(\d+)",bits[1]) if len(bits)>1 else None
                width=int(m.group(1)) if m else 0
                url=normalize_image_url(bits[0],base_url)
                if url: candidates.append((width,url))
        if candidates: return max(candidates)[1]
    style=element.get("style","")
    m=re.search(r"background-image\s*:\s*url\(['\"]?([^)'\"]+)",style,re.I)
    return normalize_image_url(m.group(1),base_url) if m else None

def map_event(raw: dict, page_url: str, source_name: str) -> dict | None:
    name = clean_event_title(text(raw.get("name")))
    if len(name) < 3: return None
    start_raw = raw.get("startDate") or raw.get("doorTime")
    end_raw = raw.get("endDate") or start_raw
    start = iso_date(start_raw)
    if not start: return None
    location = raw.get("location") or {}
    if isinstance(location, list): location = location[0] if location else {}
    addr = location.get("address", {}) if isinstance(location, dict) else {}
    venue = text(location.get("name")) if isinstance(location, dict) else ""
    address = text(addr.get("streetAddress")) if isinstance(addr, dict) else text(addr)
    city = text(addr.get("addressLocality")) if isinstance(addr, dict) else ""
    region = text(addr.get("addressRegion")) if isinstance(addr, dict) else ""
    area = " ".join((venue, address, city, region, text(raw.get('description')), page_url))
    page_host=(urllib.parse.urlsplit(page_url).hostname or '').lower()
    page_targets_ncr=bool(NCR_HINT.search(page_url)) or page_host in NCR_SOURCE_HOSTS
    if area and not NCR_HINT.search(area):
        # Unknown locality may be retained only on an explicitly NCR-targeted source page.
        if not page_targets_ncr: return None
    offers = raw.get("offers") or {}
    if isinstance(offers, list): offers = offers[0] if offers else {}
    price = offers.get("price") if isinstance(offers, dict) else None
    try: price = float(price) if price not in (None, "") else None
    except Exception: price = None
    image = normalize_image_url(raw.get("image") or raw.get("imageUrl") or raw.get("thumbnailUrl"), page_url)
    organizer = text(raw.get("organizer")) or source_name
    event_url = urllib.parse.urljoin(page_url, text(raw.get("url")) or page_url)
    start_time = iso_time(start_raw)
    ident = slugify(name) + "-" + start.replace("-", "")
    return {
      "id": "evt-" + ident, "slug": slugify(name), "title": name,
      "description": text(raw.get("description")) or name,
      "category": text(raw.get("eventType") or raw.get("category")) or "Events",
      "tags": [text(t) for t in (raw.get("keywords", "").split(",") if isinstance(raw.get("keywords"), str) else raw.get("keywords", [])) if text(t)],
      "startDate": start, "endDate": iso_date(end_raw) or start,
      "startTime": start_time, "endTime": iso_time(end_raw), "timezone": TZ,
      "venue": venue or None, "address": address or None, "city": city or ("New Delhi" if NCR_HINT.search(area) or page_targets_ncr else None),
      "state": region or ("Delhi" if city.lower() in ("delhi", "new delhi") else None),
      "country": text(addr.get("addressCountry")) or COUNTRY if isinstance(addr, dict) else COUNTRY,
      "countryCode": "IN", "price": price, "currency": text(offers.get("priceCurrency")) or "INR" if isinstance(offers, dict) else "INR",
      "priceType": "free_entry" if price == 0 else ("paid" if price is not None else "not_specified"),
      "registrationUrl": event_url, "eventUrl": event_url,
      "imageUrl": image, "dateStatus": "published",
      "sourceName": organizer, "sourceUrl": page_url
    }

_ROBOTS_CACHE: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
def robots_allowed(url: str, agent="*") -> bool:
    # Bounded and cached robots.txt fetch: RobotFileParser.read() can block indefinitely.
    import urllib.request
    try:
        parts = urllib.parse.urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        cached = _ROBOTS_CACHE.get(robots_url)
        if cached and time.time() - cached[0] < 3600:
            return cached[1].can_fetch(agent, url) if cached[1] is not None else True
        req = urllib.request.Request(robots_url, headers={"User-Agent": "DelhiEventsResearchBot/1.0"})
        with urllib.request.urlopen(req, timeout=3) as response:
            body = response.read(512_000).decode("utf-8", "replace")
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.parse(body.splitlines())
        _ROBOTS_CACHE[robots_url] = (time.time(), rp)
        return rp.can_fetch(agent, url)
    except Exception:
        # Cache unavailable robots responses briefly to avoid repeatedly stalling on a host.
        _ROBOTS_CACHE[robots_url] = (time.time(), None)
        return True

def extract_from_page(page, url, seed_host):
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    found=[]
    # Schema.org Event JSON-LD is the most reliable generic source.
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            for raw in walk_jsonld(json.loads(tag.string or tag.get_text())):
                item = map_event(raw, url, urllib.parse.urlsplit(seed_host).hostname or "Source")
                if item: found.append(item)
        except Exception: pass
    # OpenGraph/common meta fallbacks for a single event detail page.
    if not found:
        title = (soup.select_one('meta[property="og:title"]') or {}).get("content") if soup.select_one('meta[property="og:title"]') else None
        title = title or (soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else None)
        start = None
        for sel, attr in [('meta[property="event:start_time"]','content'),('time[datetime]','datetime'),('[itemprop="startDate"]','content')]:
            el=soup.select_one(sel)
            if el: start=el.get(attr) or el.get_text(" ",strip=True); break
        end=None
        if not start:
            start,end=dates_in_text(soup.get_text(' ',strip=True)[:12000])
        if title and start:
            og_image=soup.select_one('meta[property="og:image"]') or soup.select_one('meta[name="twitter:image"]')
            raw={"@type":"Event","name":title,"startDate":start,"endDate":end or start,"url":url,"description":(soup.select_one('meta[name="description"]') or {}).get('content',''),"image":og_image.get('content') if og_image else None}
            item=map_event(raw,url,urllib.parse.urlsplit(seed_host).hostname or "Source")
            if item: found.append(item)
    links=[]
    # Only treat links as event-detail candidates when their own card-like container
    # contains a date signal. The previous ancestor-text heuristic matched menus,
    # footer links and every event-category URL, causing runaway crawling.
    date_signal = re.compile(r"\b(?:20\d{2}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}(?:\s+20\d{2})?|[A-Za-z]{3,9}\s+\d{1,2}(?:,?\s+20\d{2})?|today|tomorrow|this weekend)\b", re.I)
    for a in soup.select('a[href]'):
        href=urllib.parse.urljoin(url,a.get('href',''))
        if urllib.parse.urlsplit(href).netloc != urllib.parse.urlsplit(seed_host).netloc: continue
        if not (EVENT_HINT.search(href) or EVENT_HINT.search(a.get_text(' ',strip=True)) or '/event/' in href.lower()): continue
        label=a.get_text(" ",strip=True)
        context=a
        # Prefer the nearest bounded event-like card; avoid the whole page/container.
        for _ in range(4):
            if not context.parent: break
            context=context.parent
            cls=' '.join(context.get('class',[])) if hasattr(context,'get') else ''
            if context.name in ('article','li') or re.search(r'event|card|listing|result',cls,re.I): break
        txt=context.get_text(" ",strip=True)[:700]
        if len(label) >= 3 and date_signal.search(txt):
            links.append((canonical(href), txt, label))
    links=list(dict.fromkeys(links))
    # Site card fallback. Parse only event-like cards with an identifiable date.
    for card in soup.select('article, [itemtype*="Event"], [class*="event-card"], [class*="EventCard"], [data-testid*="event"]'):
        txt=card.get_text(" ",strip=True)
        if len(txt)<8: continue
        d=None
        t=card.select_one('time[datetime], [itemprop="startDate"]')
        if t: d=iso_date(t.get('datetime') or t.get('content') or t.get_text(" ",strip=True))
        if not d:
            match=re.search(r"\b(?:\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}|[A-Za-z]{3,9}\s+\d{1,2},?\s+20\d{2}|20\d{2}-\d{2}-\d{2})\b",txt)
            if match: d=iso_date(match.group(0))
        link=card.select_one('a[href]')
        title_el=card.select_one('h1,h2,h3,h4,[class*="title"],[itemprop="name"]')
        title=title_el.get_text(" ",strip=True) if title_el else (link.get_text(" ",strip=True) if link else "")
        if d and title and len(title)<180:
            raw={"@type":"Event","name":title,"startDate":d,"url":urllib.parse.urljoin(url,link.get('href')) if link else url,"description":txt,"image":image_from_element(card.select_one('img'),url)}
            item=map_event(raw,url,urllib.parse.urlsplit(seed_host).hostname or "Source")
            if item: found.append(item)
    # Broad but bounded fallback for platforms whose cards are divs rather than article tags.
    # Walk up from each event-looking anchor until a compact dated card is found.
    for a in soup.select('a[href]'):
        href=urllib.parse.urljoin(url,a.get('href',''))
        if urllib.parse.urlsplit(href).netloc != urllib.parse.urlsplit(seed_host).netloc: continue
        path=urllib.parse.urlsplit(href).path.lower()
        if not (re.search(r"/(event|events|experience|experiences|programme|programmes)/",path) or EVENT_HINT.search(path)): continue
        title=a.get_text(' ',strip=True) or a.get('aria-label','').strip()
        if not title or len(title)>180: continue
        node=a; card_text=''
        for _ in range(7):
            if not node.parent: break
            node=node.parent
            candidate=node.get_text(' ',strip=True)
            if len(candidate)>1200: break
            dates=dates_in_text(candidate)
            if dates[0]:
                card_text=candidate
                break
        if not card_text: continue
        start,end=dates_in_text(card_text)
        if not start: continue
        raw={'@type':'Event','name':title,'startDate':start,'endDate':end or start,'url':href,'description':card_text[:700],'image':image_from_element(node.select_one('img'),url) or image_from_element(node,url)}
        item=map_event(raw,url,urllib.parse.urlsplit(seed_host).hostname or 'Source')
        if item: found.append(item)
        links.append((canonical(href),card_text,title))
    # Site-specific adapters for calendars whose card markup does not expose Schema.org dates.
    host=(urllib.parse.urlsplit(url).hostname or '').lower()
    page_text=soup.get_text(' ',strip=True)
    if host.endswith('delhi2go.in'):
        # Their permalink contains the canonical event date (YYYYMMDD); do not infer it from teaser text.
        for a in soup.select('a[href*="/event/"]'):
            href=urllib.parse.urljoin(url,a.get('href',''))
            m=re.search(r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-3]\d)(?!\d)',href)
            if not m: continue
            try: start=date(int(m.group(1)),int(m.group(2)),int(m.group(3))).isoformat()
            except ValueError: continue
            title=clean_event_title(a.get_text(' ',strip=True) or a.get('aria-label',''))
            if len(title)<3: continue
            node=a
            for _ in range(5):
                if not node.parent: break
                node=node.parent
                if len(node.get_text(' ',strip=True))>1000: break
            raw={'@type':'Event','name':title,'startDate':start,'endDate':start,'url':href,'description':node.get_text(' ',strip=True)[:700],'image':image_from_element(node.select_one('img'),url)}
            item=map_event(raw,url,host)
            if item: found.append(item)
            links.append((canonical(href),raw['description'],title))
    elif host.endswith('itpo.gov.in'):
        # ITPO's public calendar uses heading + plain-text date blocks, not Event JSON-LD.
        for h in soup.select('h3,h4,h5,h6'):
            title=clean_event_title(h.get_text(' ',strip=True))
            if not title or len(title)>180: continue
            node=h
            for _ in range(5):
                if not node.parent: break
                parent=node.parent
                candidate=parent.get_text(' ',strip=True)
                if len(candidate)>1800: break
                node=parent
                if re.search(r'20\d{2}',candidate): break
            block=node.get_text(' ',strip=True)
            if not re.search(r'20\d{2}',block): continue
            start,end=dates_in_text(block)
            if not start: continue
            raw={'@type':'Event','name':title,'startDate':start,'endDate':end or start,'url':url,'description':block[:700],'image':image_from_element(node.select_one('img'),url)}
            item=map_event(raw,url,host)
            if item: found.append(item)
    elif host.endswith('meetup.com'):
        # Meetup cards expose event links with compact date/time copy but often no year.
        for a in soup.select('a[href*="/events/"]'):
            title=clean_event_title(a.get_text(' ',strip=True) or a.get('aria-label',''))
            node=a
            for _ in range(5):
                if not node.parent: break
                node=node.parent
                if len(node.get_text(' ',strip=True))>900: break
            block=node.get_text(' ',strip=True)
            start,end=dates_in_text(block)
            if not start or len(title)<3: continue
            raw={'@type':'Event','name':title,'startDate':start,'endDate':end or start,'url':urllib.parse.urljoin(url,a.get('href')),'description':block[:700],'image':image_from_element(node.select_one('img'),url)}
            item=map_event(raw,url,host)
            if item: found.append(item)
    elif host.endswith('indiahabitat.org'):
        # IHC calendar stores month/year once and day numbers adjacent to programme cards.
        month_match=re.search(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b',page_text,re.I)
        if month_match:
            month_num=datetime.strptime(month_match.group(1),'%B').month; year=int(month_match.group(2))
            for a in soup.select('a[href*="Events_details/"]'):
                title=clean_event_title(a.get_text(' ',strip=True))
                if not title or len(title)>180: continue
                node=a
                for _ in range(5):
                    if not node.parent: break
                    node=node.parent
                    if len(node.get_text(' ',strip=True))>1000: break
                block=node.get_text(' ',strip=True)
                dates=dates_in_text(block)
                # The calendar heading supplies the month/year; the adjacent weekday + number supplies day.
                pos=page_text.find(a.get_text(' ',strip=True))
                before=page_text[max(0,pos-180):pos] if pos>=0 else ''
                day_matches=re.findall(r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2})\b',before,re.I)
                start=None
                if day_matches:
                    try: start=date(year,month_num,int(day_matches[-1])).isoformat()
                    except ValueError: start=None
                if not start: start=dates[0]
                if not start: continue
                raw={'@type':'Event','name':title,'startDate':start,'endDate':dates[1] or start,'url':urllib.parse.urljoin(url,a.get('href')),'description':block[:700],'image':image_from_element(node.select_one('img'),url)}
                item=map_event(raw,url,host)
                if item: found.append(item)
    # Keep one card record per title/date/link; dedup later.
    unique={}
    for item in found: unique.setdefault((item['title'].casefold(),item['startDate'],item.get('eventUrl')),item)
    return list(unique.values()), list(dict.fromkeys(links))

def absolute_https(value):
    if not value: return value
    value=str(value).strip()
    if value.lower().startswith('webcal://'): value='https://' + value[len('webcal://'):]
    return value

def infer_ncr_city_state(event):
    hay=' '.join(str(event.get(k) or '') for k in ('city','state','venue','address','title','description','eventUrl','sourceUrl','sourceName')).lower()
    places=[('greater noida','Greater Noida','Uttar Pradesh'),('gurugram','Gurugram','Haryana'),('gurgaon','Gurugram','Haryana'),('ghaziabad','Ghaziabad','Uttar Pradesh'),('faridabad','Faridabad','Haryana'),('noida','Noida','Uttar Pradesh'),('new delhi','New Delhi','Delhi'),('delhi','New Delhi','Delhi')]
    city=text(event.get('city')); state=text(event.get('state'))
    for token, guessed_city, guessed_state in places:
        if token in hay:
            if not city: city=guessed_city
            if not state: state=guessed_state
            break
    # These seed pages explicitly target Delhi/NCR. Fill missing locality conservatively.
    if not city:
        city='New Delhi'
    if not state:
        state='Delhi'
    return city,state

def finalize_events(events, cutoff=None, keep_past=False):
    """Repair schema fields, remove non-events/past items, and make IDs/slugs unique."""
    cutoff=cutoff or date.today().isoformat()
    filtered=[]
    for e in events:
        event_url=str(e.get('eventUrl') or '').lower()
        path=urllib.parse.urlsplit(event_url).path.lower()
        title0=str(e.get('title') or '').lower().strip()
        if any(part in path for part in ('/guides/','/places/','/venue/','/venues/','/directions/')) or re.search(r"/(events?|programmes?|programs?|calendar|schedule)/?$",path):
            continue
        if title0 in {'view event record','venue directions','directions','faq','district gurugram'}: continue
        # Dates encoded in a Delhi2go event permalink are more reliable than card copy.
        m=re.search(r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-3]\d)(?!\d)',event_url)
        if m:
            try:
                linked=date(int(m.group(1)),int(m.group(2)),int(m.group(3))).isoformat()
                e['startDate']=linked
                if not e.get('endDate') or e['endDate']<linked: e['endDate']=linked
            except ValueError: pass
        if e.get('startDate') and e.get('endDate') and e['endDate']<e['startDate']:
            e['endDate']=e['startDate']
        if not keep_past and (e.get('endDate') or e.get('startDate') or '')<cutoff: continue
        filtered.append(e)
    events=filtered
    for e in events:
        e['title']=clean_event_title(text(e.get('title')) or 'Untitled event')
        e['slug']=slugify(e['title'])
        e['city'],e['state']=infer_ncr_city_state(e)
        e['currency']=(text(e.get('currency')) or 'INR').upper()
        for key in ('eventUrl','registrationUrl','sourceUrl','imageUrl'):
            e[key]=absolute_https(e.get(key))
        if e.get('eventUrl') and not e.get('registrationUrl'):
            e['registrationUrl']=e['eventUrl']
    # IDs include a deterministic digest of distinguishing event fields to avoid same-title/date collisions.
    used_ids=set()
    for e in events:
        base='evt-'+slugify(e.get('title','event'))+'-'+re.sub(r'[^0-9]','',str(e.get('startDate') or ''))
        identity='|'.join(str(e.get(k) or '') for k in ('title','startDate','endDate','venue','city','eventUrl','sourceUrl'))
        digest=hashlib.sha1(identity.encode('utf-8')).hexdigest()[:8]
        candidate=base
        if candidate in used_ids: candidate=f'{base}-{digest}'
        n=2; original=candidate
        while candidate in used_ids:
            candidate=f'{original}-{n}'; n+=1
        e['id']=candidate; used_ids.add(candidate)
    used_slugs=set()
    for e in events:
        base=slugify(e.get('title','event'))
        candidate=base
        if candidate in used_slugs:
            date_suffix=re.sub(r'[^0-9]','',str(e.get('startDate') or ''))[:8]
            candidate=f'{base}-{date_suffix}' if date_suffix else base
        identity='|'.join(str(e.get(k) or '') for k in ('title','startDate','venue','city','eventUrl','sourceUrl'))
        if candidate in used_slugs: candidate=f'{base}-{hashlib.sha1(identity.encode()).hexdigest()[:8]}'
        n=2; original=candidate
        while candidate in used_slugs:
            candidate=f'{original}-{n}'; n+=1
        e['slug']=candidate; used_slugs.add(candidate)
    return events

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--urls-file', help='Text file with one seed URL per line; defaults to built-in list')
    ap.add_argument('--output', default='events-delhi-ncr.json')
    ap.add_argument('--template', help='Optional sample JSON file; preserves schemaVersion and metadata conventions')
    ap.add_argument('--max-pages-per-site', type=int, default=1, help='Listing/pagination pages per seed (bounded; do not crawl arbitrary site links)')
    ap.add_argument('--max-detail-pages', type=int, default=12, help='Maximum dated event detail pages to inspect from each seed')
    ap.add_argument('--from-date', default=date.today().isoformat(), help='Keep events whose end date is on/after this date (YYYY-MM-DD)')
    ap.add_argument('--keep-past', action='store_true', help='Do not filter past events')
    ap.add_argument('--timeout', type=int, default=12000, help='Navigation timeout in milliseconds')
    ap.add_argument('--scroll-count', type=int, default=6, help='Bounded lazy-load scroll rounds per listing page')
    ap.add_argument('--delay', type=float, default=0.8, help='Seconds between navigations')
    ap.add_argument('--headful', action='store_true')
    args=ap.parse_args()
    seeds=DEFAULT_SEEDS
    if args.urls_file:
        seeds=[line.strip() for line in Path(args.urls_file).read_text().splitlines() if line.strip() and not line.strip().startswith('#')]
    results=[]; source_log=[]; seen_event=set()
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=not args.headful)
        context=browser.new_context(locale='en-IN', timezone_id=TZ, user_agent='Mozilla/5.0 (compatible; DelhiEventsResearchBot/1.0; +respect-site-policies)')
        page=context.new_page()
        seen_detail_urls=set()
        for seed in seeds:
            seed_host=urllib.parse.urlsplit(seed)
            count_before=len(results)
            image_count_before=sum(1 for e in results if e.get('imageUrl'))
            status='ok'; err=''; detail_links=[]; fetched=set(); queue=[canonical(seed)]
            cap=args.max_pages_per_site
            # Restrict each crawl to its seed hostname; obey a conservative delay.
            while queue and len(fetched)<cap:
                url=queue.pop(0)
                if url in fetched: continue
                fetched.add(url)
                if not robots_allowed(url):
                    status='robots_disallowed'; continue
                try:
                    time.sleep(args.delay)
                    page.goto(url, wait_until='domcontentloaded', timeout=args.timeout)
                    # Do not wait for networkidle: ticketing sites often keep analytics/socket
                    # requests open forever. Bounded scrolling reveals lazy cards/infinite-feed batches.
                    stable_heights=0; last_height=0
                    for _ in range(max(0,min(args.scroll_count,12))):
                        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                        page.wait_for_timeout(400)
                        height=page.evaluate('document.body.scrollHeight')
                        stable_heights=stable_heights+1 if height==last_height else 0
                        last_height=height
                        if stable_heights>=2: break
                    events, links=extract_from_page(page,url,seed)
                    for ev in events:
                        key=(ev['title'].casefold(),ev['startDate'],ev.get('city',''))
                        if key not in seen_event: seen_event.add(key); results.append(ev)
                    for href,txt,label in links:
                        if href not in fetched:
                            # Follow likely event detail links first; retain pagination/calendar paths too.
                            if EVENT_HINT.search(href) or any(k in href.lower() for k in ('/event/','/events/','/programme','/program/','/schedule')):
                                detail_links.append(href)
                    if url==canonical(seed):
                        # Follow same-site pages explicitly linked from event cards; crawl cap prevents runaway traversal.
                        queue.extend(x for x in dict.fromkeys(detail_links) if x not in fetched)
                except Exception as e:
                    status='fetch_error'; err=str(e)[:220]
                    if len(fetched)==1: break
            # Process discovered pages only for same host and within the configured per-site budget.
            extras=[u for u in dict.fromkeys(detail_links) if u not in fetched and u not in seen_detail_urls][:args.max_detail_pages]
            for url in extras:
                seen_detail_urls.add(url)
                if not robots_allowed(url): continue
                try:
                    time.sleep(args.delay)
                    page.goto(url,wait_until='domcontentloaded',timeout=args.timeout)
                    events,_=extract_from_page(page,url,seed)
                    for ev in events:
                        key=(ev['title'].casefold(),ev['startDate'],ev.get('city',''))
                        if key not in seen_event: seen_event.add(key); results.append(ev)
                except Exception: pass
            events_added=len(results)-count_before
            if status=='ok' and events_added==0: status='no_new_unique_records'
            source_log.append({'sourceName':seed_host.hostname or seed,'sourceUrl':seed,'status':status,'pagesVisited':len(fetched),'discoveredEventLinks':len(set(detail_links)),'detailPagesVisited':len(extras),'eventsAdded':events_added,'eventsWithImagesAdded':sum(1 for e in results if e.get('imageUrl'))-image_count_before,'error':err or None})
            # Checkpoint after every source so an interruption never loses all prior results.
            partial={'schemaVersion':'1.0.0','dataset':{'name':'Delhi NCR Events Discovery','description':'Partial checkpoint; crawl still in progress.','generatedAt':datetime.now().astimezone().isoformat(timespec='seconds'),'defaultTimezone':TZ,'defaultCountry':'IN','eventCount':len(results),'currencyDefault':'INR','notice':'Partial checkpoint. Wait for the final output file and verify records.'},'sources':source_log,'events':sorted(results,key=lambda x:(x.get('startDate') or '',x.get('title','').casefold()))}
            Path(args.output + '.partial').write_text(json.dumps(partial,ensure_ascii=False,indent=2))
            added=len(results)-count_before
            with_images=sum(1 for e in results if e.get('imageUrl'))-image_count_before
            print(f"{seed_host.hostname}: status={status}; {len(fetched)} listing pages, {len(extras)} detail pages, {len(set(detail_links))} candidates; +{added} new events (+{with_images} with images), {len(results)} total",file=sys.stderr)
        browser.close()
    template={}
    if args.template:
        try: template=json.loads(Path(args.template).read_text())
        except Exception as e: print(f"Could not read template: {e}",file=sys.stderr)
    before_filter=len(results)
    results=finalize_events(results,args.from_date,args.keep_past)
    print(f"Upcoming-event filter: kept {len(results)} of {before_filter} extracted records",file=sys.stderr)
    now=datetime.now().astimezone().isoformat(timespec='seconds')
    data={'schemaVersion':template.get('schemaVersion','1.0.0'),'dataset':{
      'name':'Delhi NCR Events Discovery','description':'Best-effort crawl of publicly accessible event listings. Records are deduplicated by title, date and location.','generatedAt':now,'defaultTimezone':TZ,'defaultCountry':'IN','eventCount':len(results),'currencyDefault':'INR','notice':'Automated extraction is imperfect. Confirm event details with organisers. Sources may block automated requests or render content only after interaction. No login or access controls are bypassed.'},'sources':source_log,'events':sorted(results,key=lambda x:(x.get('startDate') or '',x.get('title','').casefold()))}
    Path(args.output).write_text(json.dumps(data,ensure_ascii=False,indent=2))
    with_images=sum(1 for e in results if e.get('imageUrl'))
    print(f"Wrote {len(results)} events ({with_images} with image URLs) from {len(source_log)} seed URLs to {args.output}")
if __name__=='__main__': main()
