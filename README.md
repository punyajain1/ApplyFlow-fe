# Job FE

Frontend for the job search workflow used by my Telegram automation stack.
It repurposes the backend from my organization Telegram bot project, collects search filters from the UI, and lets users generate and download results as CSV files.

## Related Project

- Organization backend + bot project (full feature set): [JOB_SCRAPPER_TelegramBot](https://github.com/punyajain1/JOB_SCRAPPER_TelegramBot)
- This frontend is a focused UI layer for search + CSV export and reuses the backend from that bot project, while the bot repository contains additional Telegram-first automation features.

## Tech Stack

![Next.js](https://img.shields.io/badge/Next.js-16-black?logo=nextdotjs)
![React](https://img.shields.io/badge/React-19-20232A?logo=react)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-4-06B6D4?logo=tailwindcss&logoColor=white)
![Framer Motion](https://img.shields.io/badge/Framer_Motion-Animation-0055FF?logo=framer&logoColor=white)
![Axios](https://img.shields.io/badge/Axios-HTTP-5A29E4)
![PapaParse](https://img.shields.io/badge/PapaParse-CSV-2F855A)

## Architecture

```mermaid
flowchart LR
		U[User in Browser] --> FE[Next.js App Router Frontend]
		FE -->|POST /job-search| BE[Hosted JOB_SCRAPPER_TelegramBot Backend]
		BE -->|JSON jobs[]| FE
		FE -->|Export| CSV[CSV Download via PapaParse]
```

### Request Flow

1. User enters filters such as search term, location, recency, remote, and job type.
2. Frontend maps camelCase form fields to backend API params.
3. Frontend sends a POST request to the backend job-search endpoint.
4. Backend returns normalized job results.
5. Frontend displays count and allows CSV export.

## Setup

### Prerequisites

- Node.js 20+
- npm 10+

### Install

```bash
npm install
```

### Run in Development

```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

### Lint

```bash
npm run lint
```

### Production Build

```bash
npm run build
npm run start
```

## Project Structure

```text
src/
	app/
		layout.tsx      # Root layout + metadata
		page.tsx        # Main UI, API call, CSV export
		globals.css     # Global styles and theme vars
```

## API Notes

- Frontend currently calls the repurposed hosted endpoint from the bot project backend.
- Ensure the backend service from the organization project [JOB_SCRAPPER_TelegramBot](https://github.com/punyajain1/JOB_SCRAPPER_TelegramBot) is running and reachable.
