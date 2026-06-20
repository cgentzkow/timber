#!/usr/bin/env python3
"""
Timber — San Diego retail/commercial permit pipeline.
Downloads the City of San Diego development-permit feeds (current + prior year),
filters to consumer-facing retail/commercial projects, classifies each project,
and writes a compact permits.json for the front-end map.

Runs locally and in GitHub Actions (weekly). No paid APIs.
"""
import csv, io, json, re, sys, urllib.request, datetime, os
from collections import defaultdict

BASE = "https://seshat.datasd.org/development_permits/approvals_created_{year}_datasd.csv"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "permits.json")

THIS_YEAR = datetime.date.today().year
YEARS = [THIS_YEAR - 1, THIS_YEAR]  # 2-year rolling window

# ---- Classification dictionaries -------------------------------------------------

# BC codes that are unambiguously residential -> always exclude
RESIDENTIAL_BC = re.compile(
    r"(1 or 2 Fam|1 Family|One Family|Two family|Companion Unit|Acc Apt|"
    r"Family Apt|Family Condo|3\+ Fam|Guest House|Mobile Home|Pool or Spa|"
    r"Acc Struct to 1|Acc Bldg to 1|Acc Bldgs to 3|Housekeeping|ADU|Duplex)",
    re.I,
)

# Approval / permit types that are never a real building project we care about
NONBUILDING_TYPE = re.compile(
    r"(Traffic Control|Transportation Permit|Construction Noise|Hydrant|"
    r"Photovoltaic|Zone History|Zoning Use|Mills Act|Agreement|"
    r"Right Of Way|Right of Way|Grading|Mapping|Dry Utilities|Map\b|"
    r"Notice of Termination|Stormwater|Storm Water|SWMD)",
    re.I,
)

# Multifamily residential BC codes — drop UNLESS an explicit commercial/retail
# signal is present (true mixed-use ground-floor retail is kept).
MULTIFAMILY_BC = re.compile(
    r"(Add/Alt 3\+|3\+ Fam|Family Apt|Family Condo|Five or More|"
    r"Three or Four Family|Two family|Add/Alt Acc)", re.I)
STRONG_RETAIL = re.compile(
    r"\b(retail|restaurant|storefront|mercantile|commercial (space|tenant|unit|"
    r"shell)|ground[- ]?floor (retail|commercial)|drive[- ]?thru|car ?wash|"
    r"gas station|mixed[- ]?use commercial)\b", re.I)

# Hard excludes — commercial but NOT consumer-facing retail
EXCLUDE_RE = re.compile(
    r"(wireless|telecommunication|\bwcf\b|cell (tower|site)|antenna|monopole|"
    r"\bt-?mobile\b|verizon|at&t|small cell|data center|warehouse|distribution "
    r"center|self[- ]?storage|industrial|manufacturing|wastewater|substation|"
    r"\butility\b|pump station|reservoir|solar (farm|array)|"
    r"medical office|hospital\b|clinic|laborator)",
    re.I,
)

# Retail / consumer-facing BC codes -> auto-include (whitelist)
RETAIL_BC = re.compile(
    r"(Store/Mercantile|Service Station|Amusement/Recreation|Office/Bank)",
    re.I,
)

# Keyword -> retail sub-type (scanned across title + scope)
RETAIL_KEYWORDS = [
    ("Restaurant", r"\b(restaurant|eatery|dining|kitchen|bistro|grill|taqueria|"
                   r"pizz|sushi|cafe|café|coffee|bakery|deli|food hall|"
                   r"qsr|fast food|drive[- ]?thru|drive[- ]?through|juice bar|"
                   r"ice cream|donut|doughnut|smoothie|teahouse|boba)\b"),
    ("Bar / Brewery", r"\b(brewery|brewpub|taproom|tap room|cocktail|wine bar|"
                      r"\bbar\b|tavern|pub|distillery|nightclub|lounge)\b"),
    ("Gas / Fuel", r"\b(gas station|fueling|service station|petroleum|"
                   r"chevron|arco|mobil(?!e)|76 station|circle k|ev charg|"
                   r"fuel canopy|fuel dispenser)\b"),
    ("Car Wash / Auto", r"\b(car ?wash|auto ?wash|lube|tire|smog|auto repair|"
                        r"repair garage|dealership|automotive|oil change|"
                        r"jiffy lube|valvoline)\b"),
    ("Grocery / Market", r"\b(grocery|supermarket|market|grocer|food 4 less|"
                         r"vons|ralphs|albertsons|sprouts|trader joe|whole foods|"
                         r"aldi|costco|sam's club|liquor store|convenience store)\b"),
    ("Fitness / Gym", r"\b(gym|fitness|crossfit|yoga|pilates|cycle studio|"
                      r"climbing|martial arts|athletic club)\b"),
    ("Personal Care", r"\b(salon|barber|nail|spa|massage|tattoo|med ?spa|"
                      r"wellness|aesthetic)\b"),
    ("Pharmacy / Health Retail", r"\b(pharmacy|cvs|walgreens|rite aid|"
                                 r"urgent care|dental|dentist|optometr|vision)\b"),
    ("Bank / Financial", r"\b(bank|credit union|chase|wells fargo|"
                         r"financial center)\b"),
    ("Cannabis", r"\b(dispensary|cannabis|marijuana|smoke shop)\b"),
    ("Hotel / Hospitality", r"\b(hotel|motel|inn\b|hospitality|resort|lodging)\b"),
    ("Entertainment", r"\b(theater|theatre|cinema|bowling|arcade|amusement|"
                      r"entertainment|recreation)\b"),
    ("General Retail", r"\b(retail|mercantile|store|shop\b|shopping center|"
                       r"shopping centre|strip mall|outlet|boutique|showroom|"
                       r"tenant space|shell building|shell bldg)\b"),
]
RETAIL_ANY = re.compile("|".join("(?:%s)" % p for _, p in RETAIL_KEYWORDS), re.I)

# Project category detection
NEW_RE = re.compile(r"\b(new construction|ground[- ]?up|new building|new bldg|"
                    r"new\b.*\b(building|structure|store|restaurant|shell)|"
                    r"new shell|core (and|&) shell)\b", re.I)
DEMO_RE = re.compile(r"\b(demo|demolition|demolish)\b", re.I)
TI_RE = re.compile(r"\b(tenant improvement|t\.?i\.?\b|interior improvement|"
                   r"build[- ]?out|buildout)\b", re.I)
REDEV_RE = re.compile(r"\b(remodel|renovat|reconfigur|fa[cç]ade|"
                      r"reposition|redevelop|conversion|convert|"
                      r"alteration|addition)\b", re.I)

# On-hold / stalled status detection.
# Genuine "shelved" signal only — NOT routine review states like pending invoice.
HOLD_RE = re.compile(r"(\bhold\b|stop work|suspend|abeyance|inactive)", re.I)
DEAD_RE = re.compile(r"(cancel|expire|withdraw|void)", re.I)
ACTIVE_RE = re.compile(r"(inspect|issued|permit\(s\) issued|approved upon)", re.I)
REVIEW_RE = re.compile(r"(review|pre-screen|submitted|checklist|invoice|"
                       r"pending|recheck|updates required|ready for issuance)", re.I)
DONE_RE = re.compile(r"(closed|final|complete)", re.I)

# Generic data-entry titles with no real business name
GENERIC_TITLE = re.compile(r"^(actively managed|self managed|dig\b|digi|"
                           r"express|standard|building const|rapid review|"
                           r"no[- ]?plan|combination|over[- ]?the[- ]?counter|otc|"
                           r"downtown|public project|plan\b|expedite|"
                           r"professional certification|demolition|grading|"
                           r"const\.? change|pts\b|map\b|discretionary|"
                           r"neighborhood use|use permit|construction change)",
                           re.I)


def num(v):
    try:
        return float(v) if v not in (None, "", " ") else 0.0
    except ValueError:
        return 0.0


def status_flag(project_status, approval_status):
    s = f"{project_status} {approval_status}"
    # Project-level status is the most reliable "shelved" signal
    if HOLD_RE.search(project_status):
        return "On Hold"
    if DEAD_RE.search(s) and not ACTIVE_RE.search(s):
        return "Cancelled/Expired"
    if ACTIVE_RE.search(s):
        return "Active / Under Construction"
    if HOLD_RE.search(s):
        return "On Hold"
    if REVIEW_RE.search(s):
        return "In Review"
    if DONE_RE.search(s):
        return "Completed"
    return project_status or approval_status or "Unknown"


def category(text, bc):
    if DEMO_RE.search(text) and not REDEV_RE.search(text):
        return "Demolition"
    if NEW_RE.search(text) or re.search(r"Building$|Mercantile|Station", bc):
        # BC codes ending in "Building"/"Station" are new structures
        if not TI_RE.search(text):
            return "New Construction"
    if TI_RE.search(text) or re.search(r"Tenant Improvement", bc, re.I):
        return "Tenant Improvement"
    if REDEV_RE.search(text) or re.search(r"Add/Alt", bc, re.I):
        return "Redevelopment / Remodel"
    return "Other"


def retail_subtypes(text):
    out = []
    for label, pat in RETAIL_KEYWORDS:
        if re.search(pat, text, re.I):
            out.append(label)
    return out


# Known national/regional retail brands — scanned with word boundaries for
# reliable tenant identification. Value is the clean display name.
BRANDS = {
    r"star\s?bucks": "Starbucks", r"chick[- ]?fil[- ]?a": "Chick-fil-A",
    r"mc\s?donald": "McDonald's", r"taco bell": "Taco Bell",
    r"jack in the box": "Jack in the Box", r"7[- ]?eleven": "7-Eleven",
    r"chipotle": "Chipotle", r"dutch bros": "Dutch Bros",
    r"raising cane|\bcane'?s\b": "Raising Cane's", r"in[- ]?n[- ]?out": "In-N-Out",
    r"wendy'?s": "Wendy's", r"\bsubway\b": "Subway", r"panera": "Panera",
    r"dunkin": "Dunkin'", r"popeye": "Popeyes", r"\bkfc\b": "KFC",
    r"wingstop": "Wingstop", r"jersey mike": "Jersey Mike's",
    r"panda express": "Panda Express", r"el pollo": "El Pollo Loco",
    r"\bhabit burger|the habit\b": "The Habit", r"shake shack": "Shake Shack",
    r"five guys": "Five Guys", r"carl'?s jr": "Carl's Jr.",
    r"jollibee": "Jollibee", r"\bdel taco\b": "Del Taco",
    r"jamba juice": "Jamba", r"\bportos?\b": "Porto's",
    r"\bcava\b": "CAVA", r"sweetgreen": "Sweetgreen",
    r"jp morgan|\bchase bank\b|\bchase\b": "Chase Bank",
    r"wells fargo": "Wells Fargo", r"bank of america": "Bank of America",
    r"\bcvs\b": "CVS", r"walgreens": "Walgreens", r"rite aid": "Rite Aid",
    r"\btarget\b": "Target", r"wal[- ]?mart": "Walmart", r"costco": "Costco",
    r"\bvons\b": "Vons", r"ralphs": "Ralphs", r"sprouts": "Sprouts",
    r"\baldi\b": "Aldi", r"trader joe": "Trader Joe's", r"whole foods": "Whole Foods",
    r"grocery outlet": "Grocery Outlet", r"food 4 less": "Food 4 Less",
    r"smart\s?&\s?final|smart and final": "Smart & Final", r"\b99 cents?\b": "99 Cents Only",
    r"dollar tree": "Dollar Tree", r"dollar general": "Dollar General",
    r"planet fitness": "Planet Fitness", r"la fitness": "LA Fitness",
    r"\bcrunch\b": "Crunch Fitness", r"orange\s?theory": "Orangetheory",
    r"eos fitness": "EOS Fitness", r"24 hour fitness": "24 Hour Fitness",
    r"quick quack": "Quick Quack Car Wash", r"autozone": "AutoZone",
    r"o'?reilly": "O'Reilly", r"take 5": "Take 5", r"valvoline": "Valvoline",
    r"jiffy lube": "Jiffy Lube", r"\bchevron\b": "Chevron", r"\barco\b": "ARCO",
    r"circle k": "Circle K", r"\bwawa\b": "Wawa", r"\b76 (gas|station|fuel)": "76",
    r"\btesla\b": "Tesla", r"\brivian\b": "Rivian", r"\bulta\b": "Ulta",
    r"\bsephora\b": "Sephora", r"home depot": "Home Depot", r"lowe'?s\b": "Lowe's",
    r"\bross\b": "Ross", r"marshalls|\btj ?maxx\b": "TJX", r"\bnordstrom\b": "Nordstrom",
}
BRAND_RE = [(re.compile(p, re.I), name) for p, name in BRANDS.items()]

ADDR_TOKEN = re.compile(r"\d{2,}\s+\w+|\bst\b|\bave\b|\bblvd\b|\bdr\b|\brd\b|"
                        r"\bunit\b|\bste\b|\bsuite\b|digital", re.I)


def brand_scan(text):
    for rx, name in BRAND_RE:
        if rx.search(text):
            return name
    return ""


def tenant_guess(title):
    """Best-effort: the business name usually leads the project title."""
    if not title:
        return ""
    if GENERIC_TITLE.search(title):
        return ""
    t = re.split(r"[-–@]| at |,", title)[0].strip()
    t = re.sub(r"^\s*(digital|new|proposed|the)\s+", "", t, flags=re.I).strip()
    if ADDR_TOKEN.search(t) or len(t) < 3 or t.isdigit():
        return ""
    if re.fullmatch(r"(general|retail|commercial|shell|core|ti|tenant|"
                    r"improvement|building|construction|misc|various|"
                    r"interior|exterior|remodel|restaurant|store|office)"
                    r"(\s+\w+)?", t, re.I):
        return ""
    return t[:60]


def main():
    projects = {}  # PROJECT_ID -> aggregated record
    for year in YEARS:
        url = BASE.format(year=year)
        cache = os.path.join(os.path.dirname(OUT), f".cache_{year}.csv")
        if os.environ.get("TIMBER_CACHE") and os.path.exists(cache):
            sys.stderr.write(f"Using cache {cache}\n")
            raw = open(cache, encoding="utf-8", errors="replace").read()
        else:
            sys.stderr.write(f"Downloading {url}\n")
            try:
                raw = urllib.request.urlopen(url, timeout=120).read().decode("utf-8", "replace")
                if os.environ.get("TIMBER_CACHE"):
                    open(cache, "w").write(raw)
            except Exception as e:
                sys.stderr.write(f"  skip {year}: {e}\n")
                continue
        reader = csv.DictReader(io.StringIO(raw))
        for row in reader:
            atype = row.get("APPROVAL_TYPE", "") or ""
            bc = row.get("JOB_BC_CODE_DESCRIPTION", "") or ""
            ptype = row.get("PROJECT_TYPE", "") or ""
            if NONBUILDING_TYPE.search(atype):
                continue
            if RESIDENTIAL_BC.search(bc):
                continue
            title = row.get("PROJECT_TITLE", "") or ""
            scope = row.get("PROJECT_SCOPE", "") or ""
            ascope = row.get("APPROVAL_SCOPE", "") or ""
            text = f"{title} {scope} {ascope} {bc}"

            if EXCLUDE_RE.search(text):
                continue
            # drop multifamily-residential renovations unless explicitly retail
            if MULTIFAMILY_BC.search(bc) and not STRONG_RETAIL.search(text):
                continue
            # Retail gate: whitelist BC OR a retail keyword somewhere in the text
            is_retail = bool(RETAIL_BC.search(bc)) or bool(RETAIL_ANY.search(text))
            if not is_retail:
                continue

            pid = row.get("PROJECT_ID") or row.get("DEVELOPMENT_ID")
            if not pid:
                continue
            val = num(row.get("APPROVAL_VALUATION"))
            area = num(row.get("APPROVAL_FLOOR_AREA"))
            lat = num(row.get("GIS_LATITUDE"))
            lng = num(row.get("GIS_LONGITUDE"))

            rec = projects.get(pid)
            if rec is None:
                rec = {
                    "id": pid,
                    "title": title.strip(),
                    "scope": scope.strip()[:400],
                    "address": (row.get("GIS_ADDRESS") or "").strip(),
                    "apn": (row.get("GIS_APN") or "").strip(),
                    "lat": lat, "lng": lng,
                    "useType": bc,
                    "valuation": val,
                    "floorArea": area,
                    "stories": num(row.get("APPROVAL_STORIES")),
                    "permitHolder": (row.get("APPROVAL_PERMIT_HOLDER") or "").strip(),
                    "createDate": (row.get("PROJECT_CREATE_DATE") or "")[:10],
                    "issueDate": (row.get("APPROVAL_ISSUE_DATE") or "")[:10],
                    "_pstatus": row.get("PROJECT_STATUS", ""),
                    "_astatus": set(),
                    "_atypes": set(),
                    "_text": text,
                }
                projects[pid] = rec
            # aggregate
            rec["valuation"] = max(rec["valuation"], val)
            rec["floorArea"] = max(rec["floorArea"], area)
            rec["stories"] = max(rec["stories"], num(row.get("APPROVAL_STORIES")))
            if lat and not rec["lat"]:
                rec["lat"], rec["lng"] = lat, lng
            if not rec["useType"] and bc:
                rec["useType"] = bc
            if not rec["permitHolder"]:
                rec["permitHolder"] = (row.get("APPROVAL_PERMIT_HOLDER") or "").strip()
            rec["_astatus"].add(row.get("APPROVAL_STATUS", ""))
            if atype:
                rec["_atypes"].add(atype)
            rec["_text"] += " " + text

    # finalize
    out = []
    for rec in projects.values():
        text = rec.pop("_text")
        astatus = " ".join(rec.pop("_astatus"))
        rec["status"] = status_flag(rec.pop("_pstatus"), astatus)
        rec["category"] = category(text, rec["useType"])
        rec["retailTypes"] = retail_subtypes(text)
        rec["tenant"] = brand_scan(text) or tenant_guess(rec["title"])
        rec["approvalTypes"] = sorted(rec.pop("_atypes"))
        # drop projects with no location (can't map) unless they have value
        out.append(rec)

    # sort: biggest valuation first
    out.sort(key=lambda r: r["valuation"], reverse=True)

    payload = {
        "generated": datetime.datetime.utcnow().isoformat() + "Z",
        "source": "City of San Diego Open Data — development permits",
        "years": YEARS,
        "count": len(out),
        "permits": out,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    sys.stderr.write(f"Wrote {len(out)} projects -> {OUT}\n")

    # quick category/type breakdown for sanity
    from collections import Counter
    cat = Counter(r["category"] for r in out)
    st = Counter(r["status"] for r in out)
    sys.stderr.write(f"Categories: {dict(cat)}\n")
    sys.stderr.write(f"Statuses: {dict(st)}\n")


if __name__ == "__main__":
    main()
