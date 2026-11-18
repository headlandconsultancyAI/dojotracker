"""
Rules that decide whether a post or review is genuinely about redundancies.

Nothing in here names the client. Client-specific terms (company name,
people, other organisations that share the name) live in the MONITOR_CONFIG
repository variable, so they never appear in this public repo.

How scoring works
-----------------
Each pattern below adds its weight once if it matches.
  score >= 3  -> "Confirmed"   (clearly about job losses)
  score 1-2   -> "Possible"    (adjacent language, worth a human glance)
  score 0     -> not about redundancies

GUARDS are removed from the text before scoring, so phrases like
"dismissive", "fired up" or "redundant meetings" can't trigger a match.

To tune: add a pattern, push, and the next run re-scores everything.
"""

import re

I = re.I

# --------------------------------------------------------------------------
# Phrases that look like redundancy language but aren't
# --------------------------------------------------------------------------
GUARDS = [
    r"\bfired up\b",
    r"\bfiring on all cylinders\b",
    r"\blet(?:ting)? go of\b",
    r"\bredundant (?:code|systems?|process(?:es)?|work|meetings?|tasks?|data|checks?|steps?|layers?|approvals?|tools?|reports?|questions?|paperwork|admin)\b",
    r"\b(?:price|rate|fee|tax|interest rate) cuts?\b",
    r"\bshort ?cuts?\b",
    r"\bcut(?:ting)? corners\b",
    r"\bcorners (?:are|were|being) cut\b",
    r"\bhair ?cuts?\b",
    r"\bdismissive\w*\b",
    r"\bcustomer consultation\b",
    # technical redundancy (backup systems), not job losses
    r"\b(?:network|data|power|server|hardware|system|systems|built-in|builtin|geographic|geo|connectivity|N\+1|backup|failover|link|carrier|site|infrastructure|full)\s+redundanc(?:y|ies)\b",
    r"\bredundanc(?:y|ies)\s+(?:and|&|or)\s+(?:failover|resilience|backup|uptime|reliability)\b",
    r"\b(?:for|with|add(?:ing)?|provide[sd]?|providing|offer(?:s|ing)?|ensur\w+)\s+redundancy\b",
    r"\bredundant (?:connections?|links?|power|internet|networks?|servers?|backups?|copies|circuits?|paths?|systems?)\b",
]
GUARD_RE = re.compile("|".join(GUARDS), I)

# --------------------------------------------------------------------------
# Redundancy language: (label shown on the dashboard, regex, weight)
# --------------------------------------------------------------------------
TOPIC_PATTERNS = [
    # Strong signals
    ("redundancy",          r"\bredundanc(?:y|ies)\b", 3),
    ("made redundant",      r"\b(?:made|make|making|makes)\s+(?:\S+\s+){0,6}?redundant\b", 3),
    ("redundant",           r"\b(?:be|been|being|was|were|am)\s+redundant\b|\bredundant (?:staff|employees|colleagues|roles?|positions?|people|workers)\b", 2),
    ("layoffs",             r"\blay[- ]?offs?\b|\blaid[- ]off\b|\blay(?:ing)?[- ]off\b", 3),
    ("job cuts",            r"\b(?:job|staff|headcount|workforce|people|role|employee)\s+cuts?\b|\bcuts? (?:to|in|of) (?:staff|jobs|headcount|the workforce|employees|roles)\b", 3),
    ("headcount reduction", r"\bheadcount (?:reduction|reduce\w*)\b|\breduc\w+ (?:our |the )?headcount\b|\breduction in force\b", 3),
    ("RIF",                 r"\bRIF\b", 3),  # case-sensitive variant handled below
    ("consultation",        r"\bcollective consultation\b|\b(?:redundancy |formal )?consultation (?:period|process)\b|\bHR1\b", 3),
    ("at risk",             r"\b(?:placed |put |are |were |is |was |being )at[- ]risk\b|\bat risk of (?:redundancy|losing (?:my|our|their) (?:job|role)s?)\b", 3),
    ("downsizing",          r"\bdownsiz\w*\b|\bright[- ]?siz\w*\b", 3),
    ("roles eliminated",    r"\b(?:role|roles|position|positions|job|jobs|team|teams)\s+(?:was|were|has been|have been|is being|are being|got)\s+(?:eliminated|removed|cut|axed|disestablished|made redundant)\b", 3),
    ("mass firing",         r"\b(?:fir(?:ed|ing)|sack(?:ed|ing)|ax(?:ed|ing)|cann(?:ed|ing)|cut(?:ting)?|lost|losing)\s+(?:all|half|most|many|hundreds|dozens|loads|a third|a quarter|a lot|\d+%?)\s+(?:of\s+)?(?:the\s+|our\s+)?(?:\S+\s+)?(?:staff|team|teams|devs|developers|engineers|people|employees|workforce|QAs?|CS|managers|colleagues|roles|jobs)\b", 3),
    ("severance",           r"\bseverance\b|\bsettlement agreements?\b", 3),
    ("mass exits",          r"\bmass (?:redundanc\w+|lay[- ]?offs|cuts|sackings|firings?)\b", 3),
    # Other languages (Glassdoor has Spanish, Italian, Portuguese and French reviews)
    ("despidos (ES)",       r"\bdespid\w+\b|\bexpediente de regulaci[oó]n\b|\bEREs?\b", 3),
    ("esuberi (IT)",        r"\besuber\w+\b|\blicenziament\w+\b", 3),
    ("demissões (PT)",      r"\bdemiss(?:ão|ões|oes|ao)\b|\bdemitid\w+\b", 3),
    ("licenciements (FR)",  r"\blicenciement\w*\b|\bplan social\b", 3),

    # Medium signals
    ("let go",              r"\b(?:let|letting)\s+(?:\S+\s+){0,3}?go\b", 2),
    ("restructuring",       r"\brestructur\w*\b", 2),
    ("fired / sacked",      r"\b(?:fired|firing|sacked|sacking|sackings|axed|axing)\b|\bget(?:s|ting)? rid of (?:people|staff|employees)\b", 2),
    ("cost cutting",        r"\bcost[- ]cutting\b|\bcut(?:ting)? costs\b", 2),
    ("no job security",     r"\b(?:no|zero|little|lack of|without|not (?:a lot of|much|any)) job security\b|\bjob insecurity\b", 2),

    ("corporate phrasing",  r"\b(?:proposed|proposing|propose) changes\b|\bdifficult decisions?\b|\baffect(?:s|ed|ing)? (?:some |a number of |a small number of )?(?:roles|colleagues|team members|people)\b|\bimpacted (?:colleagues|roles|team members|employees|people|teammates)\b|\bsaying goodbye to (?:some )?(?:colleagues|talented|team)\b|\breduc\w+ the size of (?:our|the) (?:team|business|workforce)\b|\bstreamlin\w+ (?:our|the) (?:team|business|organisation|organization|structure)\b", 2),

    # Weak signals: only enough on their own for "Possible"
    ("consultation (other)", r"\bthe consultation\b|\bconsultation\b(?= (?:with|on) (?:colleagues|staff|employees|affected))", 1),
    ("job security",        r"(?<!no )(?<!zero )(?<!little )(?<!of )(?<!without )\bjob security\b", 1),
    ("reorg",               r"\bre-?orgs?\b|\breorganis\w+|\breorganiz\w+", 1),
    ("open to work",        r"#?\bopen ?to ?work\b", 1),
    ("dismissal",           r"\b(?:constructive|unfair) dismissal\b", 1),
    ("managed out",         r"\b(?:managed|pushed|forced) out\b", 1),
    ("cuts",                r"\bcuts\b", 1),
]

_CASE_SENSITIVE = {"RIF", "despidos (ES)"}  # 'ERE' / 'RIF' must be upper case

TOPIC_RES = []
for label, pat, w in TOPIC_PATTERNS:
    if label in _CASE_SENSITIVE:
        # case-insensitive for the word parts, case-sensitive for acronyms
        if label == "RIF":
            rx = re.compile(r"\bRIFs?\b")
        else:
            rx = re.compile(r"(?i:\bdespid\w+\b|\bexpediente de regulaci[oó]n\b)|\bEREs?\b")
    else:
        rx = re.compile(pat, I)
    TOPIC_RES.append((label, rx, w))

CONFIRMED_AT = 3

# --------------------------------------------------------------------------
# Themes that travel with redundancy mentions. Shown as tags and charted.
# --------------------------------------------------------------------------
THEMES = [
    ("Handling & communication", r"\bno (?:warning|notice|explanation)\b|\blittle (?:to no )?(?:warning|notice)\b|\bout of the blue\b|\babrupt\w*\b|\binsensitiv\w*\b|\btone[- ]deaf\b|\btransparen\w+\b|\bcommunicat\w+\b|\bsilence\b|\bhandled\b|\bblindsided\b|\bmessaging\b"),
    ("Package & terms",          r"\bpackage\b|\bseverance\b|\bpay ?outs?\b|\benhanced\b|\bstatutory\b|\bnotice period\b|\bgarden leave\b|\bvoluntary redundanc\w+\b|\bsettlement\b|\bcompensation\b"),
    ("Leadership & founders",    r"\bfounders?\b|\bCEO\b|\bC-level\b|\bleadership\b|\bexec(?:utive)?s?\b|\bsenior management\b|\bboard\b"),
    ("Return to office",         r"\bRTO\b|\breturn to (?:the )?office\b|\b(?:[2-5]|two|three|four|five) days (?:a week )?in (?:the )?office\b|\b(?:[2-5]|three|four|five) days a week\b|\boffice mandate\b|\bin-office\b"),
    ("Rehiring after cuts",      r"\bre-?hir\w+\b|\bhiring again\b|\bnew hires\b|\bnow hiring\b|\breplaced (?:by|with)\b|\bpromoted into\b|\bsame roles\b|\bdoing a lot of new hiring\b"),
    ("Morale & trust",           r"\bmorale\b|\btrust\b|\bfear\b|\banx\w+\b|\bworr\w+\b|\bstress\w*\b|\bbetray\w*\b|\bdemotivat\w+\b|\bdisillusion\w*\b"),
    ("Repeat rounds",            r"\brounds? of\b|\banother round\b|\bannual\b|\bevery year\b|\byearly\b|\bback to back\b|\bconsecutive\b|\brepeated\b|\bmultiple (?:rounds|redundanc\w+|layoffs)\b|\b(?:two|three|four|2|3|4)(?:rd|nd)? (?:rounds|waves|groups)\b"),
    ("Finances, sale & investors", r"\bIPO\b|\bgo(?:ing)? public\b|\blisting\b|\bacquir\w+\b|\bsale\b|\binvestors?\b|\bvaluation\b|\bfunding\b|\bprofitab\w+\b|\blosses\b|\bfinancial\w*\b"),
    ("Customers & service",      r"\bcustomers?\b|\bmerchants?\b|\bservice levels?\b|\bcomplaints?\b|\bsupport team\b"),
]
THEME_RES = [(name, re.compile(p, I)) for name, p in THEMES]

# --------------------------------------------------------------------------
# Tone for LinkedIn posts (Glassdoor uses the reviewer's star rating instead)
# --------------------------------------------------------------------------
NEGATIVE_WORDS = r"""gutted devastat\w* shock\w* disgrace\w* disgust\w* toxic betray\w* appall\w* cruel heartless brutal
chaos chaotic shambles awful terrible shameful callous angry anger furious heartbroken unfair unjust poorly badly
insult\w* sad sadly disappoint\w* frustrat\w* outrag\w* ruthless dreadful horrendous deceit\w* lied lies incompeten\w*
blindsided gaslight\w* scandal\w* worst avoid run sickening tone-deaf abrupt\w* insensitiv\w* cold-hearted"""
NEGATIVE_PHRASES = [r"\bno warning\b", r"\bout of the blue\b", r"\bwithout (?:any )?(?:warning|notice)\b",
                    r"\bbadly handled\b", r"\bpoorly handled\b", r"\bhandled (?:so )?(?:badly|poorly|terribly)\b",
                    r"\black of (?:transparency|communication|empathy|respect)\b", r"\bnot (?:a )?(?:good|great) look\b",
                    r"\btreated (?:like|as) (?:numbers|a number|dirt|rubbish)\b", r"\bstay away\b"]
POSITIVE_WORDS = r"""grateful thankful proud excited privilege honoured honored fortunate supportive kind generous
dignity respectful onwards forward lucky blessed amazing brilliant fantastic incredible appreciat\w* thank thanks"""
POSITIVE_PHRASES = [r"\bnext chapter\b", r"\bhandled (?:it )?(?:really |so )?well\b", r"\bwith (?:real )?(?:care|dignity|compassion)\b",
                    r"\bwhat'?s next\b", r"\bnew opportunit\w+\b"]

NEG_RE = re.compile(r"\b(?:" + "|".join(NEGATIVE_WORDS.split()) + r")\b", I)
POS_RE = re.compile(r"\b(?:" + "|".join(POSITIVE_WORDS.split()) + r")\b", I)
NEG_PHRASE_RES = [re.compile(p, I) for p in NEGATIVE_PHRASES]
POS_PHRASE_RES = [re.compile(p, I) for p in POSITIVE_PHRASES]

# --------------------------------------------------------------------------
# LinkedIn post types
# --------------------------------------------------------------------------
AFFECTED_RE = re.compile(
    r"\b(?:I|I've|I have|I was|I'm|I am|my role|my position|my job|myself)\b[^.!?\n]{0,80}?\b(?:redundan\w+|laid[- ]off|lay[- ]?offs?|impacted|affected|let go|at risk|cuts|restructur\w+)"
    r"|#?\bopen ?to ?work\b|\blooking for (?:my )?(?:next|new) (?:role|opportunit\w+|challenge|position)\b"
    r"|\bafter \d+ (?:years|months)\b[^.!?\n]{0,60}\b(?:redundan\w+|laid off|let go|impacted)", I)
RECRUITER_RE = re.compile(
    r"\bif you(?:'ve| have)? (?:been|were) (?:affected|impacted)\b|\banyone (?:affected|impacted)\b|\bDM me\b|\bdrop me a (?:message|line|DM)\b"
    r"|\bwe(?:'re| are) hiring\b|\bhappy to (?:help|chat|connect)\b|\breach out\b|\btalent\b|\brecruit\w*\b|\bopen roles\b", I)
OFFICIAL_ROLE_RE = re.compile(r"\b(?:CEO|founder|co-founder|chief|CPO|CFO|COO|CTO|VP|head of people|people director|HR director|communications|press office|spokes\w+)\b", I)
