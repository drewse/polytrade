# Polymarket Copy Lab — Frontend

React + Vite single-page app. The backend (FastAPI) runs separately; see the
[root README](../README.md) for the full architecture.

## Local development

```bash
cd frontend
npm install
npm run dev
```

The dev server runs on http://localhost:5173 and proxies `/api` to the FastAPI
backend on http://127.0.0.1:8000 (configured in [vite.config.js](vite.config.js)),
so no environment variables are required locally.

## Environment variables

| Variable             | Required | Description                                                                 |
| -------------------- | -------- | --------------------------------------------------------------------------- |
| `VITE_API_BASE_URL`  | Prod only | Backend API origin, no trailing slash (e.g. `https://your-backend.onrender.com`). Leave unset locally — the dev proxy handles `/api`. |

Copy [.env.example](.env.example) to `.env` for local overrides. In production,
set these in the Vercel project settings instead.

## Build

```bash
npm run build      # outputs static assets to dist/
npm run preview    # serve the production build locally
```

## Deploying to Vercel

The frontend is a static SPA and deploys cleanly to Vercel. The backend must be
hosted separately (Render / Railway / Fly / VPS) — Vercel only serves the static
build here.

1. **Import the repo** into Vercel (New Project → import this Git repository).
2. **Set the Root Directory** to `frontend` (Project Settings → General → Root
   Directory). This makes Vercel build only the SPA.
3. **Framework / build settings** are picked up from [vercel.json](vercel.json):
   - Framework preset: **Vite**
   - Build command: `npm run build`
   - Output directory: `dist`
4. **Add the environment variable** (Project Settings → Environment Variables):
   - `VITE_API_BASE_URL = https://your-backend-host` (the deployed backend origin)

   Vite inlines `VITE_*` variables at **build time**, so trigger a redeploy after
   changing it.
5. **Deploy.** [vercel.json](vercel.json) rewrites all non-`/api` routes to
   `index.html` so client-side React Router routes work on refresh / deep links.

### Backend CORS

The backend must allow the Vercel domain as a CORS origin. Set the backend's
`PCL_CORS_ORIGINS` to include your Vercel URL, e.g.:

```
PCL_CORS_ORIGINS=https://your-app.vercel.app,http://localhost:5173
```
