#!/usr/bin/env python3
"""
Issue monitor: pulls the latest Glassdoor and LinkedIn scrapes from Apify,
keeps every item it has ever seen in data/state, decides which ones are
genuinely about redundancies, and builds the dashboard into out/.

Usage (GitHub Actions does this for you):
    python monitor.py                         # pull from Apify, rebuild
    python monitor.py --glassdoor-file x.json # use a local scrape instead
    python monitor.py --linkedin-file y.json
    python monitor.py --preview               # also write out/preview.html with the data embedded

Environment / repository settings:
    APIFY_TOKEN                 secret
    DASHBOARD_PASSWORD          secret. If set, the data is encrypted and the page asks for it.
    APIFY_GLASSDOOR_TASK_ID     variable
    APIFY_LINKEDIN_TASK_ID      variable
    MONITOR_CONFIG              variable (JSON). Client name, people, look-alike organisations to ignore...
    MONITOR_OVERRIDES           variable (JSON). {"include": [...ids], "exclude": [...ids]}

The logs on a public repo are public, so this script only ever prints counts.
"""

import argparse, base64, datetime as dt, hashlib, json, os, re, shutil, sys, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rules  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
NOW_ISO = NOW.isoformat().replace("+00:00", "Z")
API = "https://api.apify.com/v2"
PBKDF2_ITER = 250_000


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def pick(d, *paths):
    """First non-empty value from a list of dotted paths."""
    for p in paths:
        cur = d
        for k in p.split("."):
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                cur = None
                break
        if cur not in (None, "", [], {}):
            return cur
    return None


def to_iso(v):
    """Turn whatever date a scraper gives us into ISO-8601 UTC, or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        secs = v / 1000 if v > 1e11 else v
        return dt.datetime.fromtimestamp(secs, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    s = str(v).strip()
    if re.fullmatch(r"\d{10,13}", s):
        return to_iso(int(s))
    m = re.fullmatch(r"(\d+)\s*(m|min|mins|h|hr|hrs|d|day|days|w|wk|wks|mo|y|yr)\w*(?: ago)?", s, re.I)
    if m:  # "3h", "2d ago" style relative stamps
        n, u = int(m.group(1)), m.group(2).lower()
        mins = {"m": 1, "min": 1, "mins": 1, "h": 60, "hr": 60, "hrs": 60, "d": 1440, "day": 1440, "days": 1440,
                "w": 10080, "wk": 10080, "wks": 10080, "mo": 43200, "y": 525600, "yr": 525600}[u]
        return (NOW - dt.timedelta(minutes=n * mins)).isoformat().replace("+00:00", "Z")
    try:
        s2 = s.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return None


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def clean_text(s):
    if not s:
        return ""
    s = str(s).replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


# ---------------------------------------------------------------- encryption
def _kdf(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER, 32)


def encrypt(obj, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_kdf(password, salt)).encrypt(iv, json.dumps(obj, separators=(",", ":")).encode(), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt(env, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = lambda x: base64.b64decode(x)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), d(env["salt"]), env.get("iter", PBKDF2_ITER), 32)
    return json.loads(AESGCM(key).decrypt(d(env["iv"]), d(env["ct"]), None))


# ---------------------------------------------------------------- config & state
DEFAULT_CONFIG = {
    "title": "Issue monitor",
    "company": "Example Co",
    "entity_pattern": r"\bexample ?co\b",
    "context_terms": [],
    "exclude_terms": [],
    "people": [],
    "monitoring_start": None,
    "glassdoor_slug": "Company",
}


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    raw = None
    if path and os.path.exists(path):
        raw = open(path, encoding="utf-8").read()
    elif os.environ.get("MONITOR_CONFIG"):
        raw = os.environ["MONITOR_CONFIG"]
    if raw:
        cfg.update(json.loads(raw))
    else:
        log("! No MONITOR_CONFIG found, using placeholder settings")
    return cfg


def load_overrides(path):
    raw = None
    if path and os.path.exists(path):
        raw = open(path, encoding="utf-8").read()
    elif os.environ.get("MONITOR_OVERRIDES"):
        raw = os.environ["MONITOR_OVERRIDES"]
    o = json.loads(raw) if raw and raw.strip() else {}
    return {"include": set(map(str, o.get("include", []))), "exclude": set(map(str, o.get("exclude", [])))}


def state_paths(data_dir):
    return os.path.join(data_dir, "state.enc.json"), os.path.join(data_dir, "state.json")


def load_state(data_dir, password):
    enc, plain = state_paths(data_dir)
    if password and os.path.exists(enc):
        env = json.load(open(enc))
        try:
            return decrypt(env, password)
        except Exception:
            # passphrase was changed: read with the old one, the save re-encrypts with the new one
            prev = os.environ.get("DASHBOARD_PASSWORD_PREVIOUS")
            if not prev:
                sys.exit("Couldn't unlock data/state.enc.json. If you changed DASHBOARD_PASSWORD, add the old one as DASHBOARD_PASSWORD_PREVIOUS")
            state = decrypt(env, prev)
            state["_rekey"] = True
            return state
    if os.path.exists(plain):
        return json.load(open(plain, encoding="utf-8"))
    if os.path.exists(enc) and not password:
        sys.exit("data/state.enc.json exists but DASHBOARD_PASSWORD isn't set")
    return {"version": 1, "sources": {}, "items": {}, "snapshots": []}


def save_state(state, data_dir, password):
    os.makedirs(data_dir, exist_ok=True)
    enc, plain = state_paths(data_dir)
    if password:
        json.dump(encrypt(state, password), open(enc, "w"))
        if os.path.exists(plain):
            os.remove(plain)
    else:
        json.dump(state, open(plain, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- Apify
def apify_get(path, token):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def fetch_latest(task_id, token, dataset_id=None):
    """Returns (run_info, items). run_info None means 'nothing new to fetch'."""
    if dataset_id:
        run = {"id": "dataset:" + dataset_id, "finishedAt": NOW_ISO, "defaultDatasetId": dataset_id}
    else:
        run = apify_get(f"/actor-tasks/{task_id}/runs/last?status=SUCCEEDED", token)["data"]
    items, offset = [], 0
    while True:
        page = apify_get(f"/datasets/{run['defaultDatasetId']}/items?clean=true&format=json&limit=1000&offset={offset}", token)
        items.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    return {"id": run["id"], "finished": to_iso(run.get("finishedAt")) or NOW_ISO}, items


# ---------------------------------------------------------------- normalise
def norm_glassdoor(it, cfg):
    rid = it.get("reviewId")
    if not rid:
        return None
    resp = it.get("employerResponses") or []
    loc = it.get("location")
    if isinstance(loc, dict):
        loc = loc.get("name")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (it.get("employer") or {}).get("shortName") or cfg.get("glassdoor_slug", "Company")).strip("-")
    return {
        "id": f"gd:{rid}",
        "source": "glassdoor",
        "date": to_iso(it.get("reviewDateTime")),
        "url": f"https://www.glassdoor.co.uk/Reviews/Employee-Review-{slug}-RVW{rid}.htm",
        "title": clean_text(it.get("summary")),
        "sections": {k: clean_text(it.get(k)) for k in ("pros", "cons", "advice") if it.get(k)},
        "rating": as_int(it.get("ratingOverall")),
        "outlook": it.get("ratingBusinessOutlook"),
        "recommend": it.get("ratingRecommendToFriend"),
        "ceo": it.get("ratingCeo"),
        "job_title": clean_text((it.get("jobTitle") or {}).get("text")),
        "location": loc,
        "current": it.get("isCurrentJob"),
        "lang": it.get("languageId") or it.get("reviewLanguageCode"),
        "helpful": as_int(it.get("countHelpful")) or 0,
        "employer_response": clean_text(resp[0].get("response")) if resp else None,
        "employer_response_date": to_iso(resp[0].get("responseDateTime")) if resp else None,
    }


def glassdoor_snapshot(items):
    for it in items:
        rg = pick(it, "companyReviewStats.data.employerReviewsRG")
        if rg and rg.get("ratings"):
            r = rg["ratings"]
            dist = rg.get("ratingCountDistribution", {}).get("overall", {})
            return {
                "at": NOW_ISO,
                "reviews": rg.get("allReviewsCount"),
                "overall": r.get("overallRating"),
                "recommend": r.get("recommendToFriendRating"),
                "ceo": r.get("ceoRating"),
                "senior_mgmt": r.get("seniorManagementRating"),
                "culture": r.get("cultureAndValuesRating"),
                "career": r.get("careerOpportunitiesRating"),
                "one_star": dist.get("_1"),
                "five_star": dist.get("_5"),
            }
    return None


def norm_linkedin(it, entity_rx=None):
    """Returns the post plus, for posts that name the company, each of its scraped comments."""
    post = _norm_linkedin_post(it)
    if not post:
        return []
    out = [post]
    if entity_rx and entity_rx.search(post["text"] + " " + " ".join(post.get("tags") or [])):
        for cm in it.get("comments") or []:
            ctext = clean_text(cm.get("text") or cm.get("commentary") or cm.get("content"))
            if not ctext:
                continue
            link = cm.get("link") or cm.get("url") or ""
            post_num = post["id"].split(":", 1)[1]
            nums = [n for n in re.findall(r"(\d{15,})", str(cm.get("id") or cm.get("urn") or link)) if n != post_num]
            cid = nums[0] if nums else hashlib.sha1((ctext + str(cm.get("time"))).encode()).hexdigest()[:16]
            a = cm.get("author") or {}
            name = " ".join(x for x in (a.get("firstName"), a.get("lastName")) if x) or a.get("name")
            out.append({
                "id": f"li-c:{cid}",
                "source": "linkedin",
                "kind": "comment",
                "parent_id": post["id"],
                "parent_author": post["author"]["name"],
                "parent_snippet": post["text"][:240],
                "date": to_iso(cm.get("time") or cm.get("createdAt") or cm.get("postedAt")),
                "url": link or post["url"],
                "title": None,
                "text": ctext,
                "author": {"name": clean_text(name), "headline": clean_text(a.get("occupation") or a.get("headline")),
                           "url": f"https://www.linkedin.com/in/{a['publicId']}" if a.get("publicId") else None},
                "engagement": None,
            })
    return out


def _norm_linkedin_post(it):
    text = clean_text(pick(it, "text", "content", "postText", "commentary", "post_text", "textContent", "description", "body"))
    reshared = clean_text(pick(it, "resharedPost.text", "resharedPost.content", "reshared_post.text", "repost.text", "sharedPost.text"))
    if not text and not reshared:
        return None
    url = pick(it, "url", "postUrl", "post_url", "linkedinUrl", "shareUrl", "share_url", "link")
    raw_id = pick(it, "urn", "activityUrn", "activity_urn", "full_urn", "postUrn", "postId", "post_id", "entityId", "id")
    key = None
    if raw_id:
        m = re.search(r"(\d{15,})", str(raw_id))
        key = m.group(1) if m else str(raw_id)
    if not key and url:
        m = re.search(r"(?:activity|share|ugcPost)[:-](\d{15,})", str(url))
        key = m.group(1) if m else hashlib.sha1(str(url).encode()).hexdigest()[:16]
    if not key:
        key = hashlib.sha1(text.encode()).hexdigest()[:16]
    name = pick(it, "author.name", "authorName", "author.fullName", "author_name", "actor.name", "authorFullName", "posterName", "author.title")
    if not name:
        fn, ln = pick(it, "author.first_name", "author.firstName"), pick(it, "author.last_name", "author.lastName")
        name = " ".join(x for x in (fn, ln) if x) or None
    return {
        "id": f"li:{key}",
        "source": "linkedin",
        "date": to_iso(pick(it, "postedAtISO", "postedAt.date", "postedAt.timestamp", "posted_at.date", "posted_at.timestamp",
                            "postedAtTimestamp", "postedAt", "posted_at", "postedDate", "publishedAt", "date", "createdAt", "time")),
        "url": url,
        "kind": "post",
        "title": None,
        "text": text + (("\n\n[Reshared] " + reshared) if reshared else ""),
        "tags": sorted({(a.get("company") or {}).get("universalName") for a in (it.get("attributes") or [])
                        if isinstance(a, dict) and (a.get("company") or {}).get("universalName")}),
        "author": {
            "name": clean_text(name),
            "headline": clean_text(pick(it, "author.headline", "author.info", "authorHeadline", "authorTitle", "author.occupation",
                                        "author.description", "actor.description", "author_headline", "author.position")),
            "url": pick(it, "author.url", "author.linkedinUrl", "author.profile_url", "author.profileUrl", "authorProfileUrl", "authorUrl", "actor.url"),
            "type": pick(it, "author.type", "authorType"),
        },
        "engagement": {
            "reactions": as_int(pick(it, "engagement.likes", "engagement.reactions", "stats.total_reactions", "stats.reactions", "numLikes",
                                     "likesCount", "reactionsCount", "totalReactionCount", "likes", "reactions")) or 0,
            "comments": as_int(pick(it, "engagement.comments", "stats.comments", "numComments", "commentsCount", "comments_count", "comments")) or 0,
            "reposts": as_int(pick(it, "engagement.shares", "engagement.reposts", "stats.reposts", "stats.shares", "numShares", "repostsCount",
                                   "sharesCount", "shares", "reposts")) or 0,
        },
    }


# ---------------------------------------------------------------- classify
def compile_cfg(cfg):
    alt = lambda xs: re.compile(r"(?:" + "|".join(xs) + r")", re.I) if xs else None
    names = cfg.get("brand_names") or [cfg["company"]]
    return {
        "brand": re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b", re.I),
        "next_ok": set(cfg.get("brand_next_ok", [])),
        "slugs": set(cfg.get("linkedin_company_slugs", [])),
        "entity": re.compile(cfg["entity_pattern"], re.I),
        "context": alt([rf"\b{t}" for t in cfg.get("context_terms", [])]),
        "exclude": alt([rf"\b{t}\b" for t in cfg.get("exclude_terms", [])]),
        "people": alt([re.escape(p) for p in cfg.get("people", [])]),
        # "laid off from Acme", "Acme's founders": the capitalised name used as a company
        "proper": re.compile(
            rf"\b(?:at|from|by|with|left|joined|joining|leaving|across|inside)\s+{re.escape(cfg['company'])}(?:'s)?\b"
            rf"|\b{re.escape(cfg['company'])}'s (?:staff|employees|team|people|founders|CEO|leadership|workforce|management|redundanc\w+|layoffs)\b"
        ) if cfg.get("company") else None,
    }


def topic_score(text, positions=None):
    # guards are blanked out character for character so match positions still line up
    guarded = rules.GUARD_RE.sub(lambda g: " " * len(g.group(0)), text)
    hits, score, spans = [], 0, set()
    for label, rx, w in rules.TOPIC_RES:
        found = list(rx.finditer(guarded))
        if found:
            hits.append(label)
            score += w
            spans.update(f.group(0).lower() for f in found)
            if positions is not None and w >= 2:
                positions.extend(f.start() for f in found)
    return score, hits, spans


ALLOWED_PREV = {"At", "With", "From", "By", "And", "For", "Join", "Joined", "Joining", "Why", "Ex", "Left", "Leaving", "Inside",
                "Across", "About", "On", "Of", "To", "Like", "Unlike", "As", "Than", "Using", "Meet", "Partnered", "Including",
                "Congratulations", "Congrats", "Welcome", "Via", "Thanks", "Thank", "Did", "Does", "Is", "Has", "Have", "Hearing"}


# Capitalised words that can open a sentence about the company itself ("Today Acme said...")
SENTENCE_START = {"The", "Our", "Your", "My", "How", "When", "If", "Introducing", "New", "Today", "Yesterday", "This", "Last", "Now", "So", "But", "Here", "Breaking", "News", "Update", "Exclusive",
                  "Reportedly", "Fintech", "Payments", "Payment", "Unicorn", "London", "British", "Startup", "Scaleup", "Sadly",
                  "Unfortunately", "Apparently", "Interesting", "Big", "More", "Another", "Former", "Ex-colleagues", "Colleagues"}


def clean_mentions(text, cc):
    """Positions where the brand is used as the company's name, not as part of
    another name ('Market Acme', 'Acme Films', 'ACME™') or an everyday word ('my acme')."""
    out = []
    for mt in cc["brand"].finditer(text):
        if mt.group(0).islower():
            continue
        after = text[mt.end():mt.end() + 40]
        if after[:1] in ("™", "®"):
            continue
        nxt = re.match(r"[ \t]+([A-Z][\w&'-]*)", after)
        if nxt and nxt.group(1) not in cc["next_ok"]:
            continue
        before = text[max(0, mt.start() - 60):mt.start()]
        pm = re.search(r"([A-Za-z][\w&'-]*)[ \t]+$", before)
        if pm and pm.group(1)[0].isupper() and pm.group(1) not in ALLOWED_PREV:
            word = pm.group(1)
            pre = before[:pm.start()].rstrip(" \t")
            at_start = not pre or pre[-1] in ".!?\n:•-\"'(“‘|#*⠀"
            if not at_start or word.isupper() or word not in SENTENCE_START:
                continue  # 'Market Acme', 'CS Acme': a capitalised word in front makes it another name
        out.append(mt.start())
    return out


def themes_for(text):
    return [name for name, rx in rules.THEME_RES if rx.search(text)]


def tone_from_rating(r):
    if r is None:
        return "Unrated"
    return "Negative" if r <= 2 else "Mixed" if r == 3 else "Positive"


def tone_from_text(text):
    neg = len(rules.NEG_RE.findall(text)) + sum(1 for rx in rules.NEG_PHRASE_RES if rx.search(text))
    pos = len(rules.POS_RE.findall(text)) + sum(1 for rx in rules.POS_PHRASE_RES if rx.search(text))
    if neg and pos:
        return "Negative" if neg >= pos + 2 else "Positive" if pos >= neg + 3 else "Mixed"
    if neg:
        return "Negative"
    if pos:
        return "Positive"
    return "Neutral"


def full_text(rec):
    if rec["source"] == "glassdoor":
        return "\n".join(filter(None, [rec.get("title"), *rec.get("sections", {}).values()]))
    return rec.get("text") or ""


def classify(rec, cc, cfg, overrides, parent=None):
    text = full_text(rec)
    score, hits, spans = topic_score(text)
    raw_score, raw_hits, _ = topic_score_unguarded(text)
    c = {"score": score, "matched": hits, "hl": sorted(spans, key=len, reverse=True)[:12], "themes": themes_for(text) if score else [],
         "raw": raw_score}

    if rec["source"] == "glassdoor":
        c["tone"] = tone_from_rating(rec.get("rating"))
        c["entity"] = "n/a"
        if score >= rules.CONFIRMED_AT:
            c["relevance"], c["reason"] = "confirmed", "Redundancy language"
        elif score > 0:
            c["relevance"], c["reason"] = "possible", "Adjacent language only"
        else:
            c["relevance"] = "excluded"
            c["reason"] = ("Matched only on a ruled-out phrase (" + ", ".join(raw_hits) + ")") if raw_score else "No redundancy language"
        # where in the review the mention sits matters: praise in 'pros' vs complaint in 'cons'
        c["in"] = [k for k, v in rec.get("sections", {}).items() if topic_score(v)[0]] + (["title"] if rec.get("title") and topic_score(rec["title"])[0] else [])
    else:
        headline = (rec.get("author") or {}).get("headline") or ""
        company = cfg["company"]
        pos = []
        score, hits, spans = topic_score(text, pos)
        c.update(score=score, matched=hits, hl=sorted(spans, key=len, reverse=True)[:12], themes=themes_for(text) if score else [])
        mpos = clean_mentions(text, cc)
        head_ok = bool(clean_mentions(headline, cc))
        tagged = bool(cc["slugs"] & set(rec.get("tags") or []))
        people = bool(cc["people"] and cc["people"].search(text + "\n" + headline))
        if rec.get("kind") == "comment":
            # a comment inherits whether its post is about the company
            pc = classify(parent, cc, cfg, {"include": set(), "exclude": set()}) if parent else {"entity": "none"}
            mentions = pc["entity"] == "confirmed" or bool(mpos)
            ctx = pc["entity"] == "confirmed"
            near = ctx
            excl = None if mentions else True
            why_other = f"Comment on a post that isn't about {company}"
        else:
            mentions = bool(mpos) or people or tagged or (score > 0 and head_ok)
            ctx = (tagged or people or head_ok or bool(cc["proper"] and cc["proper"].search(text))
                   or (bool(mpos) and bool(cc["context"] and cc["context"].search(text + "\n" + headline))))
            # the redundancy language has to sit near the company's name, or come from someone who works there
            near = head_ok or any(abs(p - q) <= 160 for p in pos for q in mpos)
            excl = cc["exclude"].search(text + "\n" + headline) if cc["exclude"] else None
            why_other = None
            if not mentions and cc["entity"].search(text):
                other = re.search(r"(?:[A-Z][\w&'-]*[ \t]+)?\b" + cc["entity"].pattern + r"\b(?:[ \t]+[A-Z][\w&'-]*|™)?", text, re.I)
                why_other = f"Only names something else called {company} ('{other.group(0).strip() if other else company}')"
        if not mentions:
            entity, why = ("other", why_other) if why_other else ("none", f"Doesn't mention {company}")
        elif ctx:
            entity, why = "confirmed", None
        elif excl:
            entity, why = "other", f"A different {company} ('{excl.group(0)}')"
        else:
            entity, why = "ambiguous", None
        c["entity"] = entity
        c["tone"] = tone_from_text(text)
        if entity == "confirmed" and score >= rules.CONFIRMED_AT and near:
            c["relevance"], c["reason"] = "confirmed", f"Redundancy language about {company}"
        elif entity == "confirmed" and score >= rules.CONFIRMED_AT:
            c["relevance"], c["reason"] = "possible", f"Redundancy language, but not next to the mention of {company}"
        elif entity == "confirmed" and score > 0:
            c["relevance"], c["reason"] = "possible", f"About {company}, adjacent language only"
        elif entity == "ambiguous" and score >= rules.CONFIRMED_AT:
            c["relevance"], c["reason"] = "possible", f"Redundancy language, but can't tell it's this {company}"
        else:
            c["relevance"] = "excluded"
            if why:
                c["reason"] = why
            elif score > 0:
                c["reason"] = f"Adjacent language only, and can't tell it's this {company}"
            elif raw_score:
                c["reason"] = "Matched only on a ruled-out phrase (" + ", ".join(raw_hits) + ")"
            else:
                c["reason"] = "No redundancy language"
        # what kind of post is it?
        if rec.get("kind") == "comment":
            c["type"] = "Comment"
        elif entity == "confirmed" and head_ok and rules.OFFICIAL_ROLE_RE.search(headline):
            c["type"] = "Company / leadership"
        elif rules.AFFECTED_RE.search(text):
            c["type"] = "Affected employee"
        elif rules.RECRUITER_RE.search(text + "\n" + headline):
            c["type"] = "Recruiter / support offer"
        else:
            c["type"] = "Commentary"

    rid = rec["id"].split(":", 1)[1]
    if rec["id"] in overrides["exclude"] or rid in overrides["exclude"]:
        c["relevance"], c["reason"] = "excluded", "Removed by hand"
    elif rec["id"] in overrides["include"] or rid in overrides["include"]:
        c["relevance"], c["reason"] = "confirmed", "Added by hand"
    return c


def topic_score_unguarded(text):
    hits, score = [], 0
    for label, rx, w in rules.TOPIC_RES:
        if rx.search(text):
            hits.append(label)
            score += w
    return score, hits, None


# ---------------------------------------------------------------- merge
def merge(state, source, recs):
    added = 0
    items = state["items"]
    seeding = not any(i["source"] == source for i in items.values())  # first ever pull = backfill, not "new"
    for r in recs:
        if not r:
            continue
        old = items.get(r["id"])
        if old:
            r["first_seen"] = old.get("first_seen", NOW_ISO)
            r["seed"] = old.get("seed", False)
            if not r.get("date"):
                r["date"] = old.get("date")
        else:
            r["first_seen"] = NOW_ISO
            r["seed"] = seeding
            added += 1
        if not r.get("date"):
            r["date"] = r["first_seen"]
        items[r["id"]] = r
    return added


def add_snapshot(state, snap):
    if not snap:
        return
    snaps = state.setdefault("snapshots", [])
    keys = ("reviews", "overall", "recommend", "ceo", "senior_mgmt")
    if snaps:
        last = snaps[-1]
        same = all(last.get(k) == snap.get(k) for k in keys)
        age = NOW - dt.datetime.fromisoformat(last["at"].replace("Z", "+00:00"))
        if same and age < dt.timedelta(hours=6):
            return
    snaps.append(snap)


# ---------------------------------------------------------------- site
def build_site(state, cfg, overrides, out_dir, password, preview):
    cc = compile_cfg(cfg)
    feed, audit, volume = [], [], {}
    counts = {"glassdoor": {}, "linkedin": {}}
    for rec in state["items"].values():
        c = classify(rec, cc, cfg, overrides, state["items"].get(rec.get("parent_id")) if rec.get("parent_id") else None)
        src = rec["source"]
        day = (rec.get("date") or rec["first_seen"])[:10]
        volume.setdefault(src, {}).setdefault(day, 0)
        volume[src][day] += 1
        counts[src][c["relevance"]] = counts[src].get(c["relevance"], 0) + 1
        if src == "linkedin":
            counts[src]["entity_" + c["entity"]] = counts[src].get("entity_" + c["entity"], 0) + 1
        out = {k: v for k, v in rec.items()}
        out.update(c)
        if c["relevance"] in ("confirmed", "possible"):
            feed.append(out)
        else:
            # only keep filtered items that had some redundancy signal, or were removed by hand
            if c["score"] > 0 or c.get("raw") or c["reason"] == "Removed by hand":
                audit.append(out)
            bucket = ("Not about the company" if c.get("entity") in ("other", "none") or c["reason"].startswith("Comment on")
                      else "No redundancy language" if c["reason"] == "No redundancy language" else "Weak or ruled-out language")
            why = counts[src].setdefault("why", {})
            why[bucket] = why.get(bucket, 0) + 1
    feed.sort(key=lambda r: r.get("date") or "", reverse=True)
    audit.sort(key=lambda r: r.get("date") or "", reverse=True)
    data = {
        "meta": {
            "title": cfg["title"], "company": cfg["company"], "generated_at": NOW_ISO,
            "monitoring_start": cfg.get("monitoring_start"), "sources": state.get("sources", {}), "counts": counts,
            "overrides": {"include": sorted(overrides["include"]), "exclude": sorted(overrides["exclude"])},
        },
        "items": feed,
        "audit": audit[:400],
        "volume": volume,
        "snapshots": state.get("snapshots", []),
    }
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    page = open(os.path.join(HERE, "site", "index.html"), encoding="utf-8").read()
    open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8").write(page)
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    if password:
        json.dump(encrypt(data, password), open(os.path.join(out_dir, "data.enc.json"), "w"))
    else:
        json.dump(data, open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    if preview:  # unencrypted copy for a private preview, never deployed
        pdir = os.path.join(os.path.dirname(os.path.abspath(out_dir)), "preview")
        os.makedirs(pdir, exist_ok=True)
        open(os.path.join(pdir, "index.html"), "w", encoding="utf-8").write(page)
        pdata = dict(data, meta=dict(data["meta"], preview=True))
        json.dump(pdata, open(os.path.join(pdir, "data.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return data


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glassdoor-file")
    ap.add_argument("--linkedin-file")
    ap.add_argument("--config")
    ap.add_argument("--overrides")
    ap.add_argument("--data-dir", default=os.path.join(HERE, "data"))
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-read Apify runs even if already processed")
    a = ap.parse_args()

    password = os.environ.get("DASHBOARD_PASSWORD") or None
    if os.environ.get("GITHUB_ACTIONS") and not password:
        sys.exit("DASHBOARD_PASSWORD isn't set. Add it under Settings > Secrets so nothing is published unencrypted.")
    token = os.environ.get("APIFY_TOKEN")
    cfg = load_config(a.config)
    overrides = load_overrides(a.overrides)
    state = load_state(a.data_dir, password)
    log(f"Archive: {len(state['items'])} items held")
    changed = False

    jobs = [
        ("glassdoor", a.glassdoor_file, os.environ.get("APIFY_GLASSDOOR_TASK_ID"), os.environ.get("GLASSDOOR_DATASET_ID")),
        ("linkedin", a.linkedin_file, os.environ.get("APIFY_LINKEDIN_TASK_ID"), os.environ.get("LINKEDIN_DATASET_ID")),
    ]
    for src, path, task, ds in jobs:
        meta = state["sources"].setdefault(src, {})
        try:
            if path:
                run, items = {"id": "file:" + os.path.basename(path), "finished": NOW_ISO}, json.load(open(path, encoding="utf-8"))
            elif (task or ds) and token:
                run, items = fetch_latest(task, token, ds)
                if run["id"] == meta.get("last_run_id") and not a.force:
                    log(f"{src}: no new run since last check")
                    meta["last_checked"] = NOW_ISO
                    continue
            else:
                log(f"{src}: not configured, skipping")
                continue
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as e:
            code = getattr(e, "code", "")
            log(f"! {src}: couldn't fetch the latest run {code}".rstrip())
            meta["last_error"] = f"Fetch failed {code}".strip()
            meta["last_error_at"] = NOW_ISO
            continue
        if isinstance(items, dict):
            items = items.get("items") or items.get("data") or [items]
        if src == "glassdoor":
            recs = [norm_glassdoor(i, cfg) for i in items]
        else:
            erx = re.compile(cfg["entity_pattern"], re.I)
            recs = [r for i in items for r in norm_linkedin(i, erx)]
        added = merge(state, src, recs)
        if src == "glassdoor":
            add_snapshot(state, glassdoor_snapshot(items))
        changed = True
        meta.update({"last_run_id": run["id"], "last_run_finished": run["finished"], "last_checked": NOW_ISO,
                     "items_in_last_run": len(items), "new_in_last_run": added})
        meta.pop("last_error", None)
        log(f"{src}: {len(items)} items in latest run, {added} new (incl. comments)" if src == "linkedin" else f"{src}: {len(items)} items in latest run, {added} new")

    # Only rewrite the archive when a new scrape came in, so the repo isn't
    # committed to every half hour for nothing. The page still shows the latest check time.
    enc, plain = state_paths(a.data_dir)
    if state.pop("_rekey", False):
        changed = True
    if changed or not (os.path.exists(enc) or os.path.exists(plain)) or (password and os.path.exists(plain)):
        save_state(state, a.data_dir, password)
    data = build_site(state, cfg, overrides, a.out, password, a.preview)
    c = data["meta"]["counts"]
    for src in ("glassdoor", "linkedin"):
        log(f"{src}: {c[src].get('confirmed', 0)} confirmed, {c[src].get('possible', 0)} possible, {c[src].get('excluded', 0)} filtered out")


if __name__ == "__main__":
    main()
