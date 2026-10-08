from flask import Flask, request, jsonify
import json
from flask_cors import CORS
import os
import sys
import re
import uuid
import threading
from datetime import datetime, date, timedelta, timezone
import pandas as pd
import requests
from dotenv import load_dotenv

# ATS Discovery Engine imports
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dateutil import parser as date_parser


# Load .env
load_dotenv()

from jobspy import scrape_jobs

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
CORS(app)

# ============================================================
# RATE LIMITING
# ============================================================
daily_scrape_tracker = {
    "date": None,
    "count": 0
}

# ============================================================
# BACKGROUND JOB STORE (file-backed so it survives restarts)
# Maps job_id (str) → { status, started_at, finished_at, result, error }
# ============================================================
_JOBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scrape_jobs.json')
_scrape_jobs_lock = threading.Lock()

def _load_jobs() -> dict:
    """Read all jobs from disk. Returns empty dict on any error."""
    try:
        with open(_JOBS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_jobs(jobs: dict) -> None:
    """Write the full jobs dict to disk atomically."""
    tmp = _JOBS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(jobs, f)
    os.replace(tmp, _JOBS_FILE)

def _update_job(job_id: str, update: dict) -> None:
    """Thread-safe read-modify-write of a single job entry."""
    with _scrape_jobs_lock:
        jobs = _load_jobs()
        if job_id in jobs:
            jobs[job_id].update(update)
        _save_jobs(jobs)

def _get_job(job_id: str) -> dict | None:
    """Thread-safe fetch of a single job entry."""
    with _scrape_jobs_lock:
        return _load_jobs().get(job_id)

# ============================================================
# HARDCODED CONFIG — India | BTech Freshers | Last 24 Hours
# ============================================================
LOCATION        = "India"
COUNTRY         = "India"
HOURS_OLD       = 12
RESULTS_WANTED  = 200
DISTANCE        = 100
VERBOSE         = 0
LINKEDIN_FETCH  = False   # Disabled: fetching full descriptions per job is the #1 cause of timeout/OOM on Render
IS_REMOTE       = True    # Works for LinkedIn/Glassdoor/Naukri; Indeed: use 'remote' in search_term instead
JOB_TYPE        = None    # None = all types (full-time + internship)
SITES           = ["linkedin"]  # google=429 blocked, glassdoor=403 blocked, naukri=406 recaptcha

FRESHER_ROLES = [
    {
        "role": "SDE / SWE",
        # Indeed: boolean with exact match + exclusions
        "search_term": '"software engineer" OR "software developer" "entry level" OR fresher (java OR python OR javascript) -senior -lead -manager',
        # Google Jobs: simple natural language — complex queries break the cursor
        "google_search_term": "entry level software engineer jobs India",
    },
    {
        "role": "Full Stack Developer",
        "search_term": '"full stack developer" OR "fullstack developer" "entry level" OR fresher (react OR node OR angular) -senior -lead -manager',
        "google_search_term": "entry level full stack developer jobs India",
    },
    {
        "role": "Backend Developer",
        "search_term": '"backend developer" OR "backend engineer" "entry level" OR fresher (python OR java OR golang OR node) -senior -lead',
        "google_search_term": "entry level backend developer jobs India",
    },
    {
        "role": "Frontend Developer",
        "search_term": '"frontend developer" OR "frontend engineer" "entry level" OR fresher (react OR vue OR angular OR javascript) -senior -lead',
        "google_search_term": "entry level frontend developer jobs India",
    },
    {
        "role": "GenAI / AI Engineer",
        "search_term": '"AI engineer" OR "machine learning engineer" "entry level" OR fresher (python OR pytorch OR tensorflow) -senior -lead',
        "google_search_term": "entry level AI machine learning engineer jobs India",
    },
]

# ============================================================
# GOOGLE SHEETS CONFIG
# ============================================================
SHEET_ID      = os.getenv('GOOGLE_SHEET_ID')
CREDS_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')
SCOPES        = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
SHEET_HEADERS = [
    'role_category', 'title', 'company', 'location', 'work_mode', 'skills',
    'source', 'job_url', 'posted_at', 'scraped_at', 'is_internship', 'is_fresher', 'description_snippet'
]
CLEANUP_DAYS  = 1   # delete jobs older than 24 hours


def get_sheet():
    """Authorize and return the first sheet of the configured Google Spreadsheet."""
    creds_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if creds_json:
        import json
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
        
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1


def init_sheet():
    """Ensure the sheet has a header row; insert one if missing."""
    sheet     = get_sheet()
    first_row = sheet.row_values(1)
    if not first_row or first_row[0] != 'role_category':
        sheet.insert_row(SHEET_HEADERS, 1)
        print("📋 Google Sheet initialized with headers")
    return sheet


def sheets_cleanup(sheet, days=CLEANUP_DAYS):
    """Delete rows where scraped_at is older than `days` days.
    Uses batch range deletion to avoid hitting Google Sheets API quota limits.
    """
    cutoff     = datetime.now() - timedelta(days=days)
    all_values = sheet.get_all_values()
    if len(all_values) <= 1:
        return 0

    scraped_col = SHEET_HEADERS.index('scraped_at')   # 0-indexed
    to_delete   = []

    for i, row in enumerate(all_values[1:], start=2):  # row 1 = header
        if len(row) > scraped_col:
            raw = row[scraped_col].strip()
            if raw:
                try:
                    if datetime.fromisoformat(raw) < cutoff:
                        to_delete.append(i)
                except ValueError:
                    pass

    if not to_delete:
        return 0

    # Group consecutive row indices into ranges so we can delete in bulk
    # e.g. [2,3,4,7,8] → [(2,4), (7,8)] — 2 API calls instead of 5
    sorted_rows = sorted(to_delete, reverse=True)
    ranges = []
    end = start = sorted_rows[0]
    for row in sorted_rows[1:]:
        if row == end - 1:
            end = row                        # extend the range
        else:
            ranges.append((end, start))      # save completed range
            end = start = row
    ranges.append((end, start))

    # Fix: If we are about to delete all data rows, Google Sheets will throw an error 
    # ("not possible to delete all non-frozen rows"). 
    # Workaround: append a blank row first to ensure at least one non-frozen row exists.
    if len(to_delete) == len(all_values) - 1:
        try:
            sheet.append_row([""] * len(SHEET_HEADERS))
        except Exception:
            pass

    for (range_start, range_end) in ranges:
        sheet.delete_rows(range_start, range_end)

    print(f"🗑️  Deleted {len(to_delete)} jobs older than {days} day(s) in {len(ranges)} batch(es)")
    return len(to_delete)



# ============================================================
# JOB ENRICHMENT LOGIC
# ============================================================
INTERNSHIP_KEYWORDS = ["intern", "internship", "summer intern", "sde intern", "software engineer intern"]
FRESHER_KEYWORDS = ["fresher", "new grad", "graduate", "entry level", "associate engineer", "trainee"]
SKILLS = ["Python", "Java", "C++", "Node.js", "TypeScript", "React", "Next.js", "Docker", "AWS", "PostgreSQL", "MongoDB", "Redis", "Kubernetes", "Go", "Golang", "Rust", "GraphQL", "Tailwind"]
REMOTE_KWS = ["remote", "work from home", "wfh"]
HYBRID_KWS = ["hybrid"]

BACKEND_KWS = ["backend", "back-end", "server", "data engineer"]
FRONTEND_KWS = ["frontend", "front-end", "react", "ui", "ux"]
AI_KWS = ["machine learning", "ml", "ai", "llm", "data scientist"]
DEVOPS_KWS = ["devops", "sre", "platform", "infrastructure"]
FULLSTACK_KWS = ["fullstack", "full-stack", "full stack"]

def enrich_job_data(job, text_desc):
    title = str(job.get('title', '')).lower()
    loc = str(job.get('location', '')).lower()
    desc_lower = text_desc.lower()
    
    # 1. Internship & Fresher
    is_internship = any(k in title or k in desc_lower for k in INTERNSHIP_KEYWORDS)
    is_fresher = any(k in title or k in desc_lower for k in FRESHER_KEYWORDS)
    
    # 2. Work Mode
    work_mode = "Onsite"
    if any(k in loc or k in title or k in desc_lower for k in REMOTE_KWS):
        work_mode = "Remote"
    elif any(k in loc or k in title or k in desc_lower for k in HYBRID_KWS):
        work_mode = "Hybrid"
        
    # 3. Location Normalization (India specific)
    norm_loc = "Remote" if work_mode == "Remote" else job.get('location', '')
    if work_mode != "Remote":
        # simple normalization
        if any(x in loc for x in ["bangalore", "bengaluru"]): norm_loc = "Bangalore"
        elif any(x in loc for x in ["delhi", "ncr", "noida", "gurgaon", "gurugram"]): norm_loc = "Delhi NCR"
        elif "mumbai" in loc: norm_loc = "Mumbai"
        elif "pune" in loc: norm_loc = "Pune"
        elif "hyderabad" in loc: norm_loc = "Hyderabad"
        elif "chennai" in loc: norm_loc = "Chennai"
    
    # 4. Skills extraction
    found_skills = []
    for skill in SKILLS:
        if skill.lower() in desc_lower or skill.lower() in title:
            found_skills.append(skill)
            
    # 5. Role Category from title
    role_category = job.get('role_category', 'Other')
    if role_category in ['Discovery', 'YC/HN']:
        if any(k in title for k in FULLSTACK_KWS): role_category = "Full Stack"
        elif any(k in title for k in BACKEND_KWS): role_category = "Backend"
        elif any(k in title for k in FRONTEND_KWS): role_category = "Frontend"
        elif any(k in title for k in AI_KWS): role_category = "AI/ML"
        elif any(k in title for k in DEVOPS_KWS): role_category = "DevOps"
    
    return {
        "is_internship": "TRUE" if is_internship else "FALSE",
        "is_fresher": "TRUE" if is_fresher else "FALSE",
        "work_mode": work_mode,
        "location": norm_loc,
        "skills": ",".join(found_skills),
        "role_category": role_category
    }


def sheets_write_jobs(sheet, jobs):
    """Append new jobs; skip duplicates by job_url."""
    url_col = SHEET_HEADERS.index('job_url') + 1          # 1-indexed for gspread
    try:
        existing_urls = set(sheet.col_values(url_col)[1:])  # skip header row
    except Exception:
        existing_urls = set()

    now      = datetime.now().isoformat()
    new_rows = []

    for job in jobs:
        url = str(job.get('job_url') or job.get('hn_url') or '').strip()
        if not url or url in existing_urls:
            continue
        existing_urls.add(url)

        raw_desc = str(job.get('description', '') or job.get('text', '') or '')
        desc     = re.sub(r'<[^>]+>', ' ', raw_desc)
        desc     = ' '.join(desc.split())[:300] # Kept slightly longer for skills
        
        enriched = enrich_job_data(job, str(job.get('description', '') or job.get('text', '')))

        new_rows.append([
            str(enriched['role_category'])[:50],
            str(job.get('title',   '') or '')[:150],
            str(job.get('company', '') or job.get('by', '') or '')[:100],
            str(enriched['location'])[:100],
            str(enriched['work_mode']),
            str(enriched['skills']),
            str(job.get('site',    '') or job.get('source', '') or 'hn')[:50],
            url[:500],
            str(job.get('date_posted','') or job.get('posted_at','') or '')[:50],
            now,
            str(enriched['is_internship']),
            str(enriched['is_fresher']),
            desc[:200]
        ])

    if new_rows:
        sheet.append_rows(new_rows, value_input_option='RAW')
        print(f"✅ Wrote {len(new_rows)} new jobs to Google Sheet")

    return len(new_rows)


# ============================================================
# JOBSPY HELPERS
# ============================================================
def clean_jobs(jobs_df, role_label):
    records = []
    for job in jobs_df.to_dict('records'):
        cleaned = {"role_category": role_label}
        for key, value in job.items():
            if isinstance(value, (datetime, date)):
                cleaned[key] = value.isoformat()
            else:
                try:
                    cleaned[key] = None if pd.isna(value) else value
                except (TypeError, ValueError):
                    cleaned[key] = value
        records.append(cleaned)
    return records


def scrape_role(role_cfg):
    print(f"\n🔍 [{role_cfg['role']}] → {role_cfg['search_term'][:60]}...")
    print(f"   Sites : {', '.join(SITES)}")

    jobs = scrape_jobs(
        site_name=SITES,
        search_term=role_cfg["search_term"],
        google_search_term=role_cfg["google_search_term"],
        location=LOCATION,
        distance=DISTANCE,
        results_wanted=RESULTS_WANTED,
        hours_old=HOURS_OLD,
        country_indeed=COUNTRY,
        job_type=JOB_TYPE,
        is_remote=IS_REMOTE,
        linkedin_fetch_description=LINKEDIN_FETCH,
        verbose=VERBOSE,
    )
    count = len(jobs)
    print(f"   ✅ {count} jobs found for [{role_cfg['role']}]")
    return clean_jobs(jobs, role_cfg["role"]) if count > 0 else []


# ============================================================
# HN / YC HELPERS
# ============================================================
HN_API  = "https://hn.algolia.com/api/v1"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item"


def _get_latest_hiring_thread():
    import time
    # Only look at threads posted in the last 40 days to guarantee the latest monthly thread
    since = int(time.time()) - (40 * 24 * 3600)
    resp = requests.get(
        f"{HN_API}/search_by_date",
        params={
            "query": "Ask HN: Who is hiring?",
            "tags": "ask_hn,author_whoishiring",
            "hitsPerPage": 1,
            "numericFilters": f"created_at_i>{since}",
        },
        timeout=10,
    )
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    if not hits:
        return None
    hit = hits[0]
    return {
        "thread_id":    hit["objectID"],
        "title":        hit["title"],
        "created_at":   hit["created_at"],
        "num_comments": hit.get("num_comments", 0),
        "hn_url":       f"https://news.ycombinator.com/item?id={hit['objectID']}",
        "children":     hit.get("children", []),
    }


def _fetch_comment(comment_id):
    try:
        resp = requests.get(f"{HN_ITEM}/{comment_id}.json", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if not data or data.get("deleted") or data.get("dead"):
            return None
        return {
            "role_category": "YC/HN",
            "id":         data.get("id"),
            "title":      "",                           # HN posts have no title
            "company":    data.get("by", ""),
            "location":   "",
            "source":     "hn",
            "text":       data.get("text", ""),
            "posted_at":  datetime.fromtimestamp(data["time"], timezone.utc).isoformat() if data.get("time") else None,
            "job_url":    f"https://news.ycombinator.com/item?id={data.get('id')}",
            "hn_url":     f"https://news.ycombinator.com/item?id={data.get('id')}",
        }
    except Exception:
        return None


def fetch_yc_jobs(limit=150):
    thread = _get_latest_hiring_thread()
    if not thread:
        return [], None
    print(f"\n📰 YC — {thread['title']} | fetching {min(len(thread['children']), limit)} posts...")
    jobs = []
    for i, cid in enumerate(thread["children"][:limit], 1):
        c = _fetch_comment(cid)
        if c:
            jobs.append(c)
        if i % 50 == 0:
            print(f"   HN: {i}/{min(len(thread['children']), limit)}")
    print(f"   ✅ {len(jobs)} YC 'Who is hiring?' posts fetched")
    return jobs, thread


def fetch_hn_job_stories(limit=100):
    """
    Fetches direct HN job posts (type: 'job') — standalone posts from YC companies
    with real titles like 'Stripe is hiring a Backend Engineer' and a direct job URL.
    These are separate from the monthly 'Ask HN: Who is hiring?' thread.
    API: https://hacker-news.firebaseio.com/v0/jobstories.json
    """
    base = HN_ITEM.rsplit('/item', 1)[0]
    try:
        resp = requests.get(f"{base}/jobstories.json", timeout=10)
        resp.raise_for_status()
        story_ids = resp.json()[:limit]
    except Exception as e:
        print(f"   ⚠️  Could not fetch HN job stories: {e}")
        return []

    print(f"\n💼 HN Job Posts — fetching {len(story_ids)} direct job listings...")
    jobs = []
    for i, sid in enumerate(story_ids, 1):
        try:
            r = requests.get(f"{HN_ITEM}/{sid}.json", timeout=8)
            r.raise_for_status()
            data = r.json()
            if not data or data.get("dead") or data.get("deleted"):
                continue
            job_url = data.get("url") or f"https://news.ycombinator.com/item?id={data.get('id')}"
            jobs.append({
                "role_category": "HN/Jobs",
                "id":         data.get("id"),
                "title":      data.get("title", ""),
                "company":    data.get("by", ""),
                "location":   "",
                "source":     "hn_jobs",
                "text":       data.get("text", ""),
                "posted_at":  datetime.fromtimestamp(data["time"], timezone.utc).isoformat() if data.get("time") else None,
                "job_url":    job_url,
                "hn_url":     f"https://news.ycombinator.com/item?id={data.get('id')}",
            })
        except Exception:
            continue
        if i % 25 == 0:
            print(f"   HN Jobs: {i}/{len(story_ids)}")

    print(f"   ✅ {len(jobs)} direct HN job posts fetched")
    return jobs


# ============================================================
# NEW API HELPERS
# ============================================================
def fetch_arbeitnow_jobs(limit=100):
    print("\n💼 Arbeitnow — fetching jobs...")
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", timeout=10)
        r.raise_for_status()
        data = r.json().get('data', [])
        jobs = []
        for d in data[:limit]:
            jobs.append({
                "role_category": "Arbeitnow",
                "title": d.get("title", ""),
                "company": d.get("company_name", ""),
                "location": d.get("location", ""),
                "source": "arbeitnow",
                "text": d.get("description", ""),
                "posted_at": str(d.get("created_at", "")),
                "job_url": d.get("url", ""),
            })
        print(f"   ✅ {len(jobs)} Arbeitnow jobs fetched")
        return jobs
    except Exception as e:
        print(f"   ⚠️  Arbeitnow error: {e}")
        return []

def fetch_remoteok_jobs(limit=100):
    print("\n💼 RemoteOK — fetching jobs...")
    try:
        r = requests.get("https://remoteok.com/api", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        jobs = []
        for d in data:
            if "legal" in d:
                continue
            jobs.append({
                "role_category": "RemoteOK",
                "title": d.get("position", ""),
                "company": d.get("company", ""),
                "location": d.get("location", ""),
                "source": "remoteok",
                "text": d.get("description", ""),
                "posted_at": d.get("date", ""),
                "job_url": d.get("url", ""),
            })
            if len(jobs) >= limit:
                break
        print(f"   ✅ {len(jobs)} RemoteOK jobs fetched")
        return jobs
    except Exception as e:
        print(f"   ⚠️  RemoteOK error: {e}")
        return []

def fetch_jobicy_jobs(limit=100):
    print("\n💼 Jobicy — fetching jobs...")
    try:
        r = requests.get("https://jobicy.com/api/v2/remote-jobs", timeout=10)
        r.raise_for_status()
        data = r.json().get("jobs", [])
        jobs = []
        for d in data[:limit]:
            jobs.append({
                "role_category": "Jobicy",
                "title": d.get("jobTitle", ""),
                "company": d.get("companyName", ""),
                "location": d.get("jobGeo", ""),
                "source": "jobicy",
                "text": d.get("jobDescription", ""),
                "posted_at": d.get("pubDate", ""),
                "job_url": d.get("jobUrl", ""),
            })
        print(f"   ✅ {len(jobs)} Jobicy jobs fetched")
        return jobs
    except Exception as e:
        print(f"   ⚠️  Jobicy error: {e}")
        return []

def fetch_aidev_jobs(limit=100):
    print("\n💼 AI Dev Jobs — fetching jobs...")
    try:
        r = requests.get("https://aidevboard.com/api/v1/jobs", timeout=10)
        r.raise_for_status()
        resp = r.json()
        data = resp.get("jobs", resp.get("data", resp)) if isinstance(resp, dict) else resp
        jobs = []
        for d in (data if isinstance(data, list) else [])[:limit]:
            jobs.append({
                "role_category": "AI Dev Jobs",
                "title": d.get("title", ""),
                "company": d.get("company", d.get("company_name", "")),
                "location": d.get("location", ""),
                "source": "aidevjobs",
                "text": d.get("description", ""),
                "posted_at": d.get("published_at", d.get("created_at", "")),
                "job_url": d.get("url", ""),
            })
        print(f"   ✅ {len(jobs)} AI Dev Jobs fetched")
        return jobs
    except Exception as e:
        print(f"   ⚠️  AI Dev Jobs error: {e}")



def get_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session

class ATSDetector:
    def __init__(self):
        self.session = get_session()
        self.ats_domains = {
            "greenhouse.io": "greenhouse",
            "jobs.lever.co": "lever",
            "lever.co": "lever",
            "ashbyhq.com": "ashby",
            "myworkdayjobs.com": "workday",
            "myworkdaysite.com": "workday",
            "smartrecruiters.com": "smartrecruiters",
            "eightfold.ai": "eightfold",
            "icims.com": "icims",
            "taleo.net": "taleo",
            "oraclecloud.com/hcmUI": "oracle",
            "successfactors.com": "successfactors",
            "successfactors.eu": "successfactors",
            "zohorecruit.com": "zohorecruit",
            "zohorecruit.in": "zohorecruit",
            "zoho.com/recruit": "zohorecruit",
            "freshteam.com": "freshteam",
            "keka.com": "keka",
            "darwinbox.in": "darwinbox",
            "darwinbox.com": "darwinbox",
            "workable.com": "workable",
            "recruitee.com": "recruitee",
            "bamboohr.com": "bamboohr",
            "phenompeople.com": "phenom",
            "phenom.com": "phenom",
            "avature.net": "avature",
            "jobvite.com": "jobvite",
            "wellfound.com": "wellfound",
            "angel.co": "wellfound",
            "naukri.com": "naukri",
            "instahyre.com": "instahyre"
        }

    def detect_ats(self, website_url):
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = self.session.get(website_url, headers=headers, timeout=10)
            if resp.status_code != 200: return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            for a in soup.find_all('a', href=True):
                for domain, type_ in self.ats_domains.items():
                    if domain in a['href']:
                        parsed = urlparse(a['href'])
                        parts = [p for p in parsed.path.split('/') if p]
                        if type_ == "workday":
                            tenant_prefix = parsed.netloc.split('.')[0]
                            catalog = parts[0] if parts else tenant_prefix
                            return {"type": type_, "slug": f"{parsed.netloc}|{tenant_prefix}|{catalog}"}
                        elif type_ == "eightfold":
                            return {"type": type_, "slug": parsed.netloc}
                        elif type_ == "taleo":
                            portal = "ex"
                            if len(parts) >= 2 and parts[0] == "careersection":
                                portal = parts[1]
                            return {"type": type_, "slug": f"{parsed.netloc}|{portal}"}
                        if parts: return {"type": type_, "slug": parts[0]}
            for domain, type_ in self.ats_domains.items():
                if domain in resp.text:
                    if type_ == "workday":
                        match = re.search(r'https?://([a-zA-Z0-9_.-]+\.myworkdayjobs\.com)/([a-zA-Z0-9_-]+)', resp.text)
                        if match:
                            netloc = match.group(1)
                            tenant_prefix = netloc.split('.')[0]
                            return {"type": type_, "slug": f"{netloc}|{tenant_prefix}|{match.group(2)}"}
                    elif type_ == "eightfold":
                        match = re.search(r'https?://([a-zA-Z0-9_.-]+\.eightfold\.ai)', resp.text)
                        if match: return {"type": type_, "slug": match.group(1)}
                    elif type_ == "taleo":
                        match = re.search(r'https?://([a-zA-Z0-9_.-]+\.taleo\.net)/careersection/([a-zA-Z0-9_-]+)/', resp.text)
                        if match: return {"type": type_, "slug": f"{match.group(1)}|{match.group(2)}"}
                    else:
                        match = re.search(r'https?://[^"\']+' + domain.replace(".", r"\.") + r'/([a-zA-Z0-9_-]+)', resp.text)
                        if match: return {"type": type_, "slug": match.group(1)}
        except: pass
        return None


def is_recent_enough(date_str, max_hours):
    if not date_str: return True
    now = datetime.now(timezone.utc)
    try:
        if isinstance(date_str, int) or (isinstance(date_str, str) and date_str.isdigit()):
            ts = int(date_str)
            if ts > 9999999999: ts = ts / 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = date_parser.parse(str(date_str))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() <= (max_hours * 3600)
    except Exception:
        return True

def fetch_company_jobs():
    print("\n💼 ApplyFlow Discovery Engine — fetching jobs from companies.txt...")
    companies_file = os.path.join(os.path.dirname(__file__), "companies.txt")
    if not os.path.exists(companies_file):
        print("   ⚠️ companies.txt not found.")
        return []
        
    companies = []
    with open(companies_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            if ',' in line:
                name, url = line.split(',', 1)
                companies.append({'name': name.strip(), 'url': url.strip()})
            else:
                companies.append({'name': line, 'url': None})
                
    ats_detector = ATSDetector()
    session = get_session()
    all_jobs = []
    
    for comp in companies:
        company = comp['name']
        explicit_url = comp['url']
        
        if explicit_url:
            ats = ats_detector.detect_ats(explicit_url)
        else:
            website_url = f"https://www.{company.lower().replace(' ', '')}.com"
            ats = ats_detector.detect_ats(website_url)
            if not ats:
                ats = ats_detector.detect_ats(website_url + "/careers")
        if not ats:
            continue
            
        print(f"   🔍 {company}: Detected {ats['type']} (slug: {ats['slug']})")
        
        try:
            if ats['type'] == 'greenhouse':
                r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{ats['slug']}/jobs?content=true", timeout=10)
                if r.status_code == 200:
                    for d in r.json().get("jobs", []):
                        job_content = d.get("content", "")
                        desc = BeautifulSoup(job_content, "html.parser").get_text(separator=" ", strip=True)[:300] if job_content else ""
                        all_jobs.append({
                            "role_category": "Discovery",
                            "title": d.get("title", ""),
                            "company": company.title(),
                            "location": d.get("location", {}).get("name", ""),
                            "source": "greenhouse",
                            "text": desc,
                            "posted_at": d.get("updated_at", ""),
                            "job_url": d.get("absolute_url", ""),
                        })
            elif ats['type'] == 'lever':
                r = session.get(f"https://api.lever.co/v0/postings/{ats['slug']}?mode=json", timeout=10)
                if r.status_code == 200:
                    for d in r.json():
                        created_at = d.get("createdAt")
                        posted = datetime.fromtimestamp(created_at/1000, timezone.utc).isoformat() if created_at else ""
                        all_jobs.append({
                            "role_category": "Discovery",
                            "title": d.get("text", ""),
                            "company": company.title(),
                            "location": d.get("categories", {}).get("location", ""),
                            "source": "lever",
                            "text": BeautifulSoup(d.get("description", ""), "html.parser").get_text(strip=True)[:300],
                            "posted_at": posted,
                            "job_url": d.get("hostedUrl", ""),
                        })
            elif ats['type'] == 'ashby':
                r = session.post(f"https://api.ashbyhq.com/posting-api/job-board/{ats['slug']}", json={}, timeout=10)
                if r.status_code == 200:
                    for d in r.json().get("jobs", []):
                        all_jobs.append({
                            "role_category": "Discovery",
                            "title": d.get("title", ""),
                            "company": company.title(),
                            "location": d.get("location", ""),
                            "source": "ashby",
                            "text": "",
                            "posted_at": d.get("publishedAt", ""),
                            "job_url": d.get("jobUrl", ""),
                        })
            elif ats['type'] == 'smartrecruiters':
                r = session.get(f"https://api.smartrecruiters.com/v1/companies/{ats['slug']}/postings", timeout=10)
                if r.status_code == 200:
                    for d in r.json().get("content", []):
                        all_jobs.append({
                            "role_category": "Discovery",
                            "title": d.get("name", ""),
                            "company": company.title(),
                            "location": d.get("location", {}).get("city", ""),
                            "source": "smartrecruiters",
                            "text": "",
                            "posted_at": d.get("releasedDate", ""),
                            "job_url": f"https://jobs.smartrecruiters.com/{ats['slug']}/{d.get('id')}",
                        })
            elif ats['type'] == 'workable':
                url = f"https://apply.workable.com/api/v3/accounts/{ats['slug']}/jobs"
                r = session.post(url, json={}, timeout=10)
                if r.status_code in (404, 405): r = session.get(url, timeout=10)
                if r.status_code == 200:
                    for d in r.json().get("results", []):
                        all_jobs.append({
                            "role_category": "Discovery",
                            "title": d.get("title", ""),
                            "company": company.title(),
                            "location": d.get("location", {}).get("city", ""),
                            "source": "workable",
                            "text": "",
                            "posted_at": d.get("published_on", ""),
                            "job_url": d.get("url", ""),
                        })
            elif ats['type'] == 'workday':
                try:
                    netloc, tenant_prefix, catalog = ats['slug'].split('|')
                except:
                    tenant_prefix = ats['slug'].split('/')[0]
                    netloc = f"{tenant_prefix}.myworkdayjobs.com"
                    catalog = ats['slug'].split('/')[-1] if '/' in ats['slug'] else tenant_prefix
                
                url = f"https://{netloc}/wday/cxs/{tenant_prefix}/{catalog}/jobs"
                payload = {"appliedFacets":{},"limit":20,"offset":0,"searchText":""}
                headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
                r = session.post(url, json=payload, headers=headers, timeout=10)
                if r.status_code == 200:
                    for d in r.json().get("jobPostings", []):
                        all_jobs.append({
                            "role_category": "Discovery",
                            "title": d.get("title", ""),
                            "company": company.title(),
                            "location": d.get("locationsText", ""),
                            "source": "workday",
                            "text": "",
                            "posted_at": d.get("postedOn", ""),
                            "job_url": f"https://{netloc}/en-US/{catalog}{d.get('externalPath', '')}",
                        })
            elif ats['type'] == 'eightfold':
                domain = ats['slug']
                url = f"https://{domain}/api/apply/v2/jobs?domain={domain}&start=0&num=100"
                headers = {"User-Agent": "Mozilla/5.0"}
                r = session.get(url, headers=headers, timeout=10)
                if r.status_code == 200:
                    for d in r.json().get("positions", []):
                        all_jobs.append({
                            "role_category": "Discovery",
                            "title": d.get("name", ""),
                            "company": company.title(),
                            "location": d.get("location", ""),
                            "source": "eightfold",
                            "text": "",
                            "posted_at": "",
                            "job_url": f"https://{domain}/careers?pid={d.get('id', '')}",
                        })
            elif ats['type'] == 'taleo':
                try:
                    tenant, portal = ats['slug'].split('|')
                except:
                    tenant = ats['slug']
                    portal = "ex"
                
                # Taleo often needs a session cookie first
                session.get(f"https://{tenant}/careersection/{portal}/jobsearch.ftl?lang=en", timeout=10)
                url = f"https://{tenant}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}"
                payload = {
                    "multilineEnabled": False,
                    "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                    "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
                    "filterSelectionParam": {"searchFilterSelections": [{"id": "POSTING_DATE", "selectedValues": []}, {"id": "LOCATION", "selectedValues": []}, {"id": "JOB_FIELD", "selectedValues": []}, {"id": "JOB_SCHEDULE", "selectedValues": []}]},
                    "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [{"id": "ORGANIZATION", "selectedValues": []}, {"id": "LOCATION", "selectedValues": []}, {"id": "JOB_FIELD", "selectedValues": []}, {"id": "JOB_NUMBER", "selectedValues": []}, {"id": "URGENT_NEED", "selectedValues": []}, {"id": "SHIFT", "selectedValues": []}]},
                    "pageNo": 1
                }
                headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", "tz": "GMT+05:30"}
                r = session.post(url, json=payload, headers=headers, timeout=15)
                if r.status_code == 200:
                    for d in r.json().get("requisitionList", []):
                        # Taleo sometimes returns column data as a list of strings
                        title = d.get("column", [""])[0] if isinstance(d.get("column"), list) else d.get("title", "")
                        all_jobs.append({
                            "role_category": "Discovery",
                            "title": title,
                            "company": company.title(),
                            "location": "",
                            "source": "taleo",
                            "text": "",
                            "posted_at": "",
                            "job_url": f"https://{tenant}/careersection/{portal}/jobdetail.ftl?job={d.get('contestNo', '')}",
                        })
        except Exception as e:
            print(f"   ⚠️ Error fetching {company} via {ats['type']}: {e}")
            
    # Filter for India jobs AND recent jobs only
    india_keywords = ['india', 'bengaluru', 'bangalore', 'mumbai', 'delhi', 'ncr', 'gurugram', 'gurgaon', 'noida', 'pune', 'hyderabad', 'chennai', 'ahmedabad', 'kolkata']
    filtered_jobs = []
    for job in all_jobs:
        loc = str(job.get('location', '')).lower()
        
        # 1. Location Check
        if not any(k in loc for k in india_keywords):
            continue
            
        # 2. Time Check
        posted = job.get('posted_at')
        if not is_recent_enough(posted, HOURS_OLD):
            continue
            
        filtered_jobs.append(job)
            
    print(f"   ✅ {len(filtered_jobs)} Discovery Engine jobs fetched (filtered for India & last {HOURS_OLD}h out of {len(all_jobs)} total)")
    return filtered_jobs



# ================================================================
#  ENDPOINTS
# ================================================================

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'message': 'Job Scraper Server is running',
        'config': {
            'location': LOCATION,
            'country':  COUNTRY,
            'hours_old': HOURS_OLD,
            'sites':    SITES,
            'roles':    [r['role'] for r in FRESHER_ROLES],
            'sheet_connected': bool(SHEET_ID),
        }
    }), 200


# ──────────────────────────────────────────────────────────────
#  /scrape-everything  — ONE CALL → ALL SOURCES → GOOGLE SHEET
# ──────────────────────────────────────────────────────────────
def _run_scrape_job(job_id: str, yc_limit: int, hn_job_limit: int):
    """Runs in a daemon thread. Persists job state to disk via _update_job."""
    global daily_scrape_tracker

    def _set(update: dict):
        _update_job(job_id, update)

    try:
        sheet = init_sheet()

        # 1. Auto-cleanup old entries first
        deleted = sheets_cleanup(sheet)

        # 2. Scrape all fresher roles
        all_jobs, role_summary = [], {}
        for role_cfg in FRESHER_ROLES:
            jobs = scrape_role(role_cfg)
            role_summary[role_cfg['role']] = len(jobs)
            all_jobs.extend(jobs)
            # update progress so the caller can see partial info
            _set({'progress': f"Scraped {role_cfg['role']}"})

        # 3a. Fetch YC "Who is hiring?" thread posts
        yc_jobs, yc_thread = fetch_yc_jobs(limit=yc_limit)
        role_summary['YC/HN (Who is hiring?)'] = len(yc_jobs)
        all_jobs.extend(yc_jobs)

        # 3b. Fetch direct HN job posts (standalone YC company listings)
        hn_jobs = fetch_hn_job_stories(limit=hn_job_limit)
        role_summary['HN/Jobs (Direct)'] = len(hn_jobs)
        all_jobs.extend(hn_jobs)

        # 3c. Fetch Arbeitnow
        arbeitnow_jobs = fetch_arbeitnow_jobs(limit=100)
        role_summary['Arbeitnow'] = len(arbeitnow_jobs)
        all_jobs.extend(arbeitnow_jobs)

        # 3d. Fetch RemoteOK
        remoteok_jobs = fetch_remoteok_jobs(limit=100)
        role_summary['RemoteOK'] = len(remoteok_jobs)
        all_jobs.extend(remoteok_jobs)

        # 3e. Fetch Jobicy
        jobicy_jobs = fetch_jobicy_jobs(limit=100)
        role_summary['Jobicy'] = len(jobicy_jobs)
        all_jobs.extend(jobicy_jobs)

        # 3f. Fetch AI Dev Jobs
        aidev_jobs = fetch_aidev_jobs(limit=100)
        role_summary['AI Dev Jobs'] = len(aidev_jobs)
        all_jobs.extend(aidev_jobs)

        # 3g. Discovery Engine (Greenhouse, Lever, Ashby, Workable, SmartRecruiters)
        discovery_jobs = fetch_company_jobs()
        role_summary['Discovery Engine'] = len(discovery_jobs)
        all_jobs.extend(discovery_jobs)

        # 4. Deduplicate by job_url before writing
        seen, unique = set(), []
        for job in all_jobs:
            url = job.get('job_url') or job.get('hn_url') or ''
            if url and url not in seen:
                seen.add(url)
                unique.append(job)

        # 5. Write to Google Sheet
        new_count = sheets_write_jobs(sheet, unique)

        print(f"\n🎯 [{job_id[:8]}] scrape-everything done: {len(unique)} unique, {new_count} new written, {deleted} old deleted")

        _set({
            'status':       'done',
            'finished_at':  datetime.now().isoformat(),
            'progress':     'Completed',
            'result': {
                'success':       True,
                'message':       f'Scraped {len(unique)} unique jobs | {new_count} new written | {deleted} old deleted',
                'timestamp':     datetime.now().isoformat(),
                'role_summary':  role_summary,
                'total_scraped': len(unique),
                'new_written':   new_count,
                'deleted_old':   deleted,
                'yc_thread': {
                    'title':  yc_thread['title'],
                    'hn_url': yc_thread['hn_url'],
                } if yc_thread else None,
            },
        })

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"❌ [{job_id[:8]}] scrape-everything failed: {e}\n{tb}")
        _set({
            'status':      'error',
            'finished_at': datetime.now().isoformat(),
            'progress':    'Failed',
            'error':       str(e),
            'traceback':   tb,
        })


@app.route('/scrape-everything', methods=['GET'])
def scrape_everything():
    """
    Immediately returns a job_id and spawns a background thread to do the scraping.
    Poll GET /scrape-status/<job_id> to check progress and get the final result.

    Query params:
        yc_limit (int)     : how many YC comments to fetch (default 150, max 500)
        hn_job_limit (int) : how many direct HN jobs to fetch (default 100, max 200)
    """
    global daily_scrape_tracker

    today = datetime.now(timezone.utc).date()
    if daily_scrape_tracker["date"] != today:
        daily_scrape_tracker["date"] = today
        daily_scrape_tracker["count"] = 0

    if daily_scrape_tracker["count"] >= 5:
        return jsonify({
            'success': False,
            'message': 'Daily scrape limit reached (5/5). Please try again tomorrow.',
        }), 429

    daily_scrape_tracker["count"] += 1

    yc_limit     = min(int(request.args.get('yc_limit',     150)), 500)
    hn_job_limit = min(int(request.args.get('hn_job_limit', 100)), 200)

    job_id = str(uuid.uuid4())
    with _scrape_jobs_lock:
        jobs = _load_jobs()
        jobs[job_id] = {
            'status':      'running',
            'started_at':  datetime.now().isoformat(),
            'finished_at': None,
            'progress':    'Starting…',
            'result':      None,
            'error':       None,
        }
        _save_jobs(jobs)

    t = threading.Thread(
        target=_run_scrape_job,
        args=(job_id, yc_limit, hn_job_limit),
        daemon=True,
    )
    t.start()

    print(f"🚀 scrape-everything job {job_id[:8]} started (yc_limit={yc_limit}, hn_job_limit={hn_job_limit})")

    return jsonify({
        'success':   True,
        'message':   'Scrape job started. Poll /scrape-status/<job_id> for progress.',
        'job_id':    job_id,
        'status':    'running',
        'poll_url':  f'/scrape-status/{job_id}',
        'started_at': _get_job(job_id)['started_at'],
    }), 202



def _run_discovery_only_job(job_id: str):
    def _set(update: dict):
        _update_job(job_id, update)

    try:
        sheet = init_sheet()
        deleted = sheets_cleanup(sheet)
        
        all_jobs, role_summary = [], {}
        
        _set({'progress': 'Fetching Discovery Engine jobs...'})
        discovery_jobs = fetch_company_jobs()
        role_summary['Discovery Engine'] = len(discovery_jobs)
        all_jobs.extend(discovery_jobs)
        
        seen, unique = set(), []
        for job in all_jobs:
            url = job.get('job_url') or job.get('hn_url') or ''
            if url and url not in seen:
                seen.add(url)
                unique.append(job)
                
        new_count = sheets_write_jobs(sheet, unique)
        
        print(f"🎯 [{job_id[:8]}] scrape-discovery done: {len(unique)} unique, {new_count} new written, {deleted} old deleted")
        
        _set({
            'status':       'done',
            'finished_at':  datetime.now().isoformat(),
            'progress':     'Completed',
            'result': {
                'success':       True,
                'message':       f'Scraped {len(unique)} unique jobs | {new_count} new written | {deleted} old deleted',
                'timestamp':     datetime.now().isoformat(),
                'role_summary':  role_summary,
                'total_scraped': len(unique),
                'new_written':   new_count,
                'deleted_old':   deleted,
            },
        })
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"❌ [{job_id[:8]}] scrape-discovery failed: {e}\n{tb}")
        _set({
            'status':      'error',
            'finished_at': datetime.now().isoformat(),
            'progress':    'Failed',
            'error':       str(e),
            'traceback':   tb,
        })

@app.route('/scrape-discovery', methods=['GET'])
def scrape_discovery():
    global daily_scrape_tracker

    today = datetime.now(timezone.utc).date()
    if daily_scrape_tracker["date"] != today:
        daily_scrape_tracker["date"] = today
        daily_scrape_tracker["count"] = 0

    if daily_scrape_tracker["count"] >= 10:
        return jsonify({
            'success': False,
            'message': 'Daily scrape limit reached. Please try again tomorrow.',
        }), 429

    daily_scrape_tracker["count"] += 1

    job_id = str(uuid.uuid4())
    with _scrape_jobs_lock:
        jobs = _load_jobs()
        jobs[job_id] = {
            'status':      'running',
            'started_at':  datetime.now().isoformat(),
            'finished_at': None,
            'progress':    'Starting Discovery Engine...',
            'result':      None,
            'error':       None,
        }
        _save_jobs(jobs)

    t = threading.Thread(
        target=_run_discovery_only_job,
        args=(job_id,),
        daemon=True,
    )
    t.start()

    print(f"🚀 scrape-discovery job {job_id[:8]} started")

    return jsonify({
        'success':   True,
        'message':   'Discovery job started. Poll /scrape-status/<job_id> for progress.',
        'job_id':    job_id,
        'status':    'running',
        'poll_url':  f'/scrape-status/{job_id}',
        'started_at': _get_job(job_id)['started_at'],
    }), 202


# ──────────────────────────────────────────────────────────────
#  /scrape-status/<job_id>  — Poll background scrape job status
# ──────────────────────────────────────────────────────────────
@app.route('/scrape-status/<job_id>', methods=['GET'])
def scrape_status(job_id):
    """
    Returns the current state of a background scrape job.
    Possible statuses: 'running' | 'done' | 'error'
    """
    job = _get_job(job_id)

    if job is None:
        return jsonify({'success': False, 'message': f'No job found with id {job_id}'}), 404

    response = {
        'job_id':      job_id,
        'status':      job['status'],
        'started_at':  job['started_at'],
        'finished_at': job['finished_at'],
        'progress':    job['progress'],
    }

    if job['status'] == 'done':
        response.update(job['result'])
    elif job['status'] == 'error':
        response['error']     = job['error']
        response['traceback'] = job['traceback']

    return jsonify(response), 200


# ──────────────────────────────────────────────────────────────
#  /cleanup  — Manually delete jobs older than 24 hours
# ──────────────────────────────────────────────────────────────
@app.route('/cleanup', methods=['POST'])
def manual_cleanup():
    """
    Deletes all Google Sheet rows where scraped_at is older than CLEANUP_DAYS (1 day).
    Call from the browser console:
        fetch('https://applyflow-fe.onrender.com/cleanup', { method: 'POST' })
            .then(r => r.json()).then(console.log)
    """
    try:
        sheet   = get_sheet()
        deleted = sheets_cleanup(sheet)
        return jsonify({
            'success': True,
            'deleted': deleted,
            'message': f'Deleted {deleted} job(s) older than {CLEANUP_DAYS} day(s).',
        }), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


# ──────────────────────────────────────────────────────────────
#  /jobs  — READ ALL JOBS FROM GOOGLE SHEET (for dashboard)
# ──────────────────────────────────────────────────────────────
@app.route('/jobs', methods=['GET'])
def get_jobs():
    """
    Read all current jobs from Google Sheet.
    Query params:
        role   (str): filter by role_category
        source (str): filter by source
        q      (str): text search on title+company+description
    """
    try:
        sheet   = get_sheet()
        records = sheet.get_all_records()

        # Optional filters
        role   = request.args.get('role',   '').strip().lower()
        source = request.args.get('source', '').strip().lower()
        q      = request.args.get('q',      '').strip().lower()

        if role:
            records = [r for r in records if role in r.get('role_category', '').lower()]
        if source:
            records = [r for r in records if source in r.get('source', '').lower()]
        if q:
            records = [r for r in records if
                       q in r.get('title', '').lower() or
                       q in r.get('company', '').lower() or
                       q in r.get('description_snippet', '').lower()]

        return jsonify({
            'success':    True,
            'total_jobs': len(records),
            'jobs':       records,
        }), 200

    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


# ──────────────────────────────────────────────────────────────
#  Legacy endpoints (still work, but don't write to Sheet)
# ──────────────────────────────────────────────────────────────
@app.route('/scrape-all', methods=['GET'])
def scrape_all():
    try:
        all_jobs, role_summary = [], {}
        for role_cfg in FRESHER_ROLES:
            jobs = scrape_role(role_cfg)
            role_summary[role_cfg['role']] = len(jobs)
            all_jobs.extend(jobs)

        seen, unique = set(), []
        for job in all_jobs:
            url = job.get('job_url') or ''
            if url and url not in seen:
                seen.add(url); unique.append(job)

        return jsonify({
            'success': True,
            'message': f'{len(unique)} unique jobs (not written to sheet — use /scrape-everything)',
            'timestamp': datetime.now().isoformat(),
            'role_summary': role_summary,
            'total_jobs': len(unique),
            'jobs': unique,
        }), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/scrape-role', methods=['GET'])
def scrape_single_role():
    ROLE_MAP = {
        'sde': 0, 'swe': 0, 'fullstack': 1, 'full_stack': 1,
        'backend': 2, 'frontend': 3, 'genai': 4, 'ai': 4,
    }
    role_key = request.args.get('role', '').lower().replace('-', '').replace(' ', '')
    if not role_key or role_key not in ROLE_MAP:
        return jsonify({'success': False, 'message': f'Valid roles: {list(ROLE_MAP.keys())}'}), 400
    try:
        role_cfg = FRESHER_ROLES[ROLE_MAP[role_key]]
        jobs     = scrape_role(role_cfg)
        return jsonify({'success': True, 'role': role_cfg['role'], 'total_jobs': len(jobs), 'jobs': jobs}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/yc-jobs', methods=['GET'])
def yc_jobs():
    try:
        limit = min(int(request.args.get("limit", 500)), 1000)
        jobs, thread = fetch_yc_jobs(limit=limit)
        if not thread:
            return jsonify({"success": False, "message": "Could not find hiring thread"}), 404
        return jsonify({
            "success": True,
            "message": f"Fetched {len(jobs)} YC job posts",
            "timestamp": datetime.now().isoformat(),
            "thread": {"title": thread["title"], "created_at": thread["created_at"], "hn_url": thread["hn_url"]},
            "total_jobs": len(jobs),
            "jobs": jobs,
        }), 200
    except Exception as e:
        import traceback
        return jsonify({"success": False, "message": str(e), "traceback": traceback.format_exc()}), 500


if __name__ == '__main__':
    print("🚀 Job Scraper Server — India BTech Fresher Edition")
    print("=" * 58)
    print(f"   📍 Location  : {LOCATION}")
    print(f"   🕐 Hours old : {HOURS_OLD}h")
    print(f"   🌐 Sites     : {', '.join(SITES)}")
    print(f"   🎯 Roles     : {', '.join(r['role'] for r in FRESHER_ROLES)}")
    print(f"   📊 Sheet ID  : {'✅ configured' if SHEET_ID else '❌ missing (set GOOGLE_SHEET_ID in .env)'}")
    print("=" * 58)
    print("\n📖 Endpoints:")
    print("   GET  /health              — Server status + config")
    print("   GET  /scrape-everything   — ★ All roles + YC + APIs → Google Sheet")
    print("        ?yc_limit=N          — YC comment limit (default 150)")
    print("   GET  /jobs                — Read all jobs from Google Sheet")
    print("        ?role=  ?source=  ?q=")
    print("   GET  /scrape-all          — Scrape all roles (no sheet write)")
    print("   GET  /scrape-role?role=   — Scrape one role")
    print("        roles: sde | fullstack | backend | frontend | genai")
    print("   GET  /yc-jobs             — YC/HN hiring posts")
    print("\n🌐 Running on http://localhost:5050\n")

    app.run(debug=True, host='0.0.0.0', port=5050)
