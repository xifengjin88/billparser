---
type: integration
title: HTTP API, web app and sign-in
description: The FastAPI app (billparse.api with the webapi and auth routers), its endpoint groups, background parse tasks, optional Google sign-in with invite codes, the React web app and how its API types are generated, and the highlighted source viewer.
tags: [api, fastapi, web, auth, openapi]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-adb9568b7bddb4c8bc2d2760
    resource: repo://billparse/api.py
  - id: openwiki-source-5c4618c829f9e9c86fbd41b7
    resource: repo://billparse/auth.py
  - id: openwiki-source-548d6beff2809777fcaf627d
    resource: repo://billparse/highlight.py
  - id: openwiki-source-6493e5e062ec6f788b766acc
    resource: repo://billparse/webapi.py
  - id: openwiki-source-6638494c7925ab72c60cfc35
    resource: repo://scripts/export_openapi.py
  - id: openwiki-source-3f4cfcc66cce27668f418d8e
    resource: repo://web/nginx.conf
  - id: openwiki-source-14e56945b7c632a3b335dcec
    resource: repo://web/package.json
  - id: openwiki-source-4f2bc243fc968280d6dcdf63
    resource: repo://web/src/api/client.ts
  - id: openwiki-source-555215d7d2ba00c0f339b9b0
    resource: repo://web/src/router.tsx
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# HTTP API, web app and sign-in

One FastAPI app serves everything: `billparse/api.py` creates `app` and includes two routers, `auth.router`
(`/auth/...`) and `webapi.router` (the web app's endpoints). The paths and response models copy invoice-parser's, so
its web app and clients work unchanged. Run it with `uvicorn billparse.api:app` (`make api-local` on port 8000; in
Docker behind nginx on `/api`). `GET /parse` is the Swagger page.

## Middleware

- `auth.require_login`: with `AUTH=google`, every path except `/health` and `/auth/…` needs an admitted person (401
  otherwise).
- A signed session cookie (`billparse_session`, 7 days, `SESSION_SECRET`).
- GZip for responses over 2,000 bytes (a document view is mostly repeated text).

## Endpoint groups

| Group | Endpoints (selection) | Module |
|---|---|---|
| Health | `GET /health` (database check) | `api.py` |
| Parse in the request | `POST /parse` (status, lane, completeness, costs, and the strict contract document) | `api.py` |
| Background parse | `POST /parse/start`, `GET /parse/tasks`, `GET /parse/tasks/{sha256}`, `POST /documents/{sha}/reparse`, `/parse-current`, `/stop` | `webapi.py` |
| Documents | `GET /documents`, `GET /documents/{sha}`, `GET /documents/{sha}/view`, `/charges/{id}`, `DELETE /documents/{sha}` | both |
| Source viewer | `GET /documents/{sha}/source`, `/source/table`, `/source/lines`, `/source/page.png`, `GET /highlight/legend` | `webapi.py` |
| Review | `GET /review/queue`, `POST /documents/{sha}/review/preview`, `/review/approve` | `webapi.py` |
| Specs | `GET /specs`, `/specs/{id}`, `/specs/{id}/history`, `PUT /specs/{id}`, `PATCH /specs/{id}/status`, `POST /specs/verify`, `POST /specs/learn` | `api.py` |
| Learning jobs | `GET /learn/jobs`, `/learn/jobs/{id}`, `/learn/jobs/{id}/trace`, `POST …/retry`, `…/stop`, `POST /learn/backfill` | both |
| Learning loop | `POST /nightly`, `GET /metrics` | `api.py` |
| Costs and services | `GET /costs`, `GET /mapping`, `/mapping/resolve`, `GET/POST/DELETE /aliases` | `webapi.py` |
| Settings | `GET /settings` (model choices, limits for the upload page) | `webapi.py` |
| Sign-in | `GET /auth/me`, `/auth/login`, `/auth/callback`, `POST /auth/invite`, `/auth/logout` | `auth.py` |

The store and pipeline come from FastAPI dependencies (`webapi.get_store`, `get_pipeline`), so tests override them.
`get_store` also runs `recover_agent_jobs` once at start. Uploads are limited by `MAX_UPLOAD_MB` (500).

### Background parses

`webapi.start_parse` submits `Pipeline.parse_bytes` to a thread pool (`PARSE_WORKERS`, default 2) and keeps the task
in the in-memory `_tasks` dict by sha256. A second start of a file being parsed returns the same task.
`GET /parse/tasks/{sha256}` reports `running` with progress (read from the agent job's trace while it learns) or
`done` with status, lane, completeness, charges, gaps, timings, model calls and cost. Tasks are lost on restart; see
[Architecture overview](../architecture/overview.md).

The upload page may choose the spec route's model (`backend`) and the agent's (`fallback`) as `model:effort` from
`LLM_CHOICES`, or `none`.

### The document view

`GET /documents/{sha}/view` returns the contract document saved by the parse (or the approved "gold" version after
review) as one view with every charge, total, gap, note, fact and commitment, the routes tried and the confidence. See
[Output contract](../concepts/output-contract.md).

### Source viewer

The parse saves a source bundle (page lines with boxes, table rows). `source/page.png` renders a PDF page with each
line boxed and colored by what the parser made of it (`billparse/highlight.py`: charge, printed total, section
heading, fact, not counted, gap, flagged), and the focus line in yellow; table rows get the same colors as
backgrounds.

## Sign-in (`billparse/auth.py`)

Off by default (`AUTH=off`: everything open, for local development). With `AUTH=google`:

- Google OAuth (authlib), with `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`; the redirect URI is
  `<address>/api/auth/callback`;
- the Google account must be verified and of `ALLOWED_EMAIL_DOMAIN` (default `archera.ai`);
- the first sign-in needs one of the `INVITE_CODES` (at most 8 wrong tries per session); admitted users are stored in
  the `app_users` table;
- `next` redirects accept only local paths (no open redirect);
- `PUBLIC_URL` or the `X-Forwarded-*` headers give the public address.

## The web app (`web/`)

React + Vite + shadcn/ui + react-query, served by nginx in Docker; nginx forwards `/api/` to the API container.
Routes (`web/src/router.tsx`): Upload, Documents and a document, Review queue and a review, Jobs and a job (with its
trace), Costs, Specs and a spec, Mapping (services and aliases).

API types are generated, never written by hand: `make web-types` runs `pnpm gen:api`, which writes the OpenAPI
document with `scripts/export_openapi.py` to `web/openapi.json` and generates `web/src/api/schema.d.ts` with
openapi-typescript. `web/src/api/client.ts` re-exports the schema types and wraps `fetch`; a 401 answer sends an
`auth:required` event so the sign-in check runs again. Run `make typecheck` after an API change.
