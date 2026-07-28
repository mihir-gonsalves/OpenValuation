# OpenValuation - Frontend

The frontend is **React 19 + Vite + TypeScript**, styled with **Tailwind CSS v4** and a
heavily re-themed **radix ui** component layer. Server state is managed with **TanStack Query**. 
The results table includes inline SVG sparklines for each multiple's trend across the 12 TTM 
periods. All design tokens live in `frontend/src/index.css`. 

Presentation only, the FastAPI backend owns all computation (see the repo root `README.md` and 
`PHASE_4_SPEC.md`).

## Develop
```bash
npm install
npm run dev          # http://localhost:5173, proxies /api -> :8000 (backend must be running)
```

## Quality Gates
```bash
npm run build        # type-check + production build
npm run typecheck    # tsc only
npm run lint         # ESLint
npm run test         # Vitest (unit + component, MSW-mocked)
npm run test:e2e     # Playwright (mocked API, run `npx playwright install` first)
``` 

## Config
Copy `.env.example` to `.env`. Leave `VITE_API_BASE` empty in dev (Vite proxies
`/api`), set it to the Render backend URL in production.