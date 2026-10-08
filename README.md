# ApplyFlow FE

A unified, full-stack job search workflow and dashboard designed for developers. ApplyFlow seamlessly aggregates fresh opportunities across major platforms and YCombinator/HackerNews, storing them cleanly in Google Sheets. It then provides a blazing-fast, aesthetic frontend interface featuring an LLM-powered AI Resume Matcher to instantly rank your best job fits.

## Related Project

- Organization backend + bot project (full feature set): [JOB_SCRAPPER_TelegramBot](https://github.com/punyajain1/JOB_SCRAPPER_TelegramBot)
- This frontend is a focused UI layer for search + CSV export and reuses the backend from that bot project, while the bot repository contains additional Telegram-first automation features.

## 🚀 Key Features

- **Multi-Source Aggregation**: Pulls entry-level and fresh roles from major boards (LinkedIn, Indeed, Glassdoor) alongside direct API integrations with **HackerNews (Who is Hiring?)**, **YCombinator** startup jobs, **Arbeitnow**, **RemoteOK**, **Jobicy**, and **AI Dev Jobs**. It also features the **ApplyFlow Discovery Engine**, which directly tracks and scrapes the careers pages of top tech companies using popular ATS platforms (Greenhouse, Lever, Workday, etc.).
- **AI Resume Match (Beta)**: Paste your resume and use a free AI provider (Groq or OpenRouter). The frontend uses an LLM to extract your precise skills/experience level, and instantly scores/sorts hundreds of jobs based on how well they match your background.
- **Premium Dark Mode UI**: A highly polished, sleek dashboard built with Next.js, Tailwind CSS, and Framer Motion micro-interactions.
- **Google Sheets Database**: The Python backend securely syncs scraped data directly to Google Sheets, making it incredibly easy to manage, edit, and export leads.
- **Secure & Production Ready**: The backend scraper endpoint is fortified with `flask-cors`, environment-variable driven credentials, and strict daily rate limits (max 5 scrapes/day) to ensure safe public hosting.

## 🛠️ Tech Stack

### Frontend
![Next.js](https://img.shields.io/badge/Next.js-16-black?logo=nextdotjs)
![React](https://img.shields.io/badge/React-19-20232A?logo=react)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-4-06B6D4?logo=tailwindcss&logoColor=white)

### Backend
![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-API-000000?logo=flask&logoColor=white)
![JobSpy](https://img.shields.io/badge/JobSpy-Scraper-FF6B6B)
![Google Sheets](https://img.shields.io/badge/Google_Sheets-Data-34A853?logo=googlesheets&logoColor=white)

---

## 💻 Getting Started

### 1. Backend Setup

The backend handles the heavy lifting of scraping jobs and writing them to your Google Sheet.

```bash
cd Backend
pip install -r requirements.txt
```

**Environment Variables**
Copy `.env.example` to `.env` and fill in your values. If you intend to host the backend, you can pass `GOOGLE_CREDENTIALS_JSON` securely as a string environment variable.

**Run the Backend**
```bash
python3 server.py
```
*(Runs on `http://localhost:5050` by default)*

**Scraping Jobs**
Once the backend server is running, trigger the job scraping process by calling the `/scrape-everything` endpoint (via browser or terminal):
```bash
curl http://localhost:5050/scrape-everything
```

### 2. Frontend Setup

The frontend provides the sleek UI and handles the AI matching logic.

```bash
cd Frontend
npm install
npm run dev
```
*(Runs on `http://localhost:3000`)*

## 🤖 How the AI Matcher Works
ApplyFlow uses a hybrid AI approach to keep things incredibly fast and cheap:
1. **Extraction**: The frontend sends *only your resume* to a fast LLM (like Groq's Llama 3) to extract your top skills, tools, and exact experience level (e.g. "Fresher").
2. **Local Scoring**: It then runs a lightning-fast local algorithm directly in your browser, scoring every job in your database against those extracted keywords.
3. **Weighting**: Jobs get +2 points if the keyword is found in the Title, and +1 point if found in the Description. The highest scored jobs immediately snap to the top of your grid!

## 🔍 ApplyFlow Discovery Engine
ApplyFlow features a custom built **Discovery Engine** designed to find jobs directly at the source—before they are even posted to massive aggregators like LinkedIn or Indeed. 
- **Direct ATS Integration**: It automatically scans the `companies.txt` file located in the `Backend` directory, auto-detects the Applicant Tracking System (ATS) they use (such as Greenhouse, Lever, Workday, Darwinbox, Eightfold, SmartRecruiters, Keka, etc.), and reverse-engineers their API endpoints.
- **Easy to Expand**: To add new companies to your tracking list, simply add their name and careers URL to `Backend/companies.txt`. The engine will figure out the rest and pull in fresh developer roles every time you run the scraper.

## 🔗 Related Project
This backend was initially adapted from my organization Telegram bot project: [JOB_SCRAPPER_TelegramBot](https://github.com/punyajain1/JOB_SCRAPPER_TelegramBot), but has since been expanded significantly to support HackerNews/YC and secure frontend integration.
