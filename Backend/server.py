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

# Load .env
load_dotenv()

# Add JobSpy to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'JobSpy-main'))
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
HOURS_OLD       = 24
RESULTS_WANTED  = 200
DISTANCE        = 100
VERBOSE         = 0
LINKEDIN_FETCH  = False   # Disabled: fetching full descriptions per job is the #1 cause of timeout/OOM on Render
IS_REMOTE       = True    # Works for LinkedIn/Glassdoor/Naukri; Indeed: use 'remote' in search_term instead
JOB_TYPE        = None    # None = all types (full-time + internship)
SITES           = ["indeed", "linkedin"]  # google=429 blocked, glassdoor=403 blocked, naukri=406 recaptcha

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
    'role_category', 'title', 'company', 'location',
    'source', 'job_url', 'posted_at', 'scraped_at', 'description_snippet'
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
    """Delete rows where scraped_at is older than `days` days."""
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

    for row_idx in sorted(to_delete, reverse=True):
        sheet.delete_rows(row_idx)

    if to_delete:
        print(f"🗑️  Deleted {len(to_delete)} jobs older than {days} days")
    return len(to_delete)


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
        desc     = ' '.join(desc.split())[:200]

        new_rows.append([
            str(job.get('role_category', 'YC/HN'))[:50],
            str(job.get('title',   '') or '')[:150],
            str(job.get('company', '') or job.get('by', '') or '')[:100],
            str(job.get('location','') or '')[:100],
            str(job.get('site',    '') or job.get('source', '') or 'hn')[:50],
            url[:500],
            str(job.get('date_posted','') or job.get('posted_at','') or '')[:50],
            now,
            desc,
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
    print(f"   🕐 Hours old : {HOURS_OLD}h (last 24 hours only)")
    print(f"   🌐 Sites     : {', '.join(SITES)}")
    print(f"   🎯 Roles     : {', '.join(r['role'] for r in FRESHER_ROLES)}")
    print(f"   📊 Sheet ID  : {'✅ configured' if SHEET_ID else '❌ missing (set GOOGLE_SHEET_ID in .env)'}")
    print("=" * 58)
    print("\n📖 Endpoints:")
    print("   GET  /health              — Server status + config")
    print("   GET  /scrape-everything   — ★ All roles + YC → Google Sheet")
    print("        ?yc_limit=N          — YC comment limit (default 150)")
    print("   GET  /jobs                — Read all jobs from Google Sheet")
    print("        ?role=  ?source=  ?q=")
    print("   GET  /scrape-all          — Scrape all roles (no sheet write)")
    print("   GET  /scrape-role?role=   — Scrape one role")
    print("        roles: sde | fullstack | backend | frontend | genai")
    print("   GET  /yc-jobs             — YC/HN hiring posts")
    print("\n🌐 Running on http://localhost:5050\n")

    app.run(debug=True, host='0.0.0.0', port=5050)
