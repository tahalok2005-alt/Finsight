# Deploying FinSight

FinSight deploys as **one container**: the API serves the dashboard from the
same address, so there is one port, one process and no CORS to configure.

Three things decide where it can run:

| Need | Why |
|---|---|
| **Memory: 4 GB or more** | Each page read at once needs about 400 MB, and the number of pages read in parallel is chosen from *free* memory. With little memory it still works, just slowly. |
| **Long requests are fine** | A document takes seconds to minutes and is processed in the background by the same process. |
| **A disk that persists** | `data/` holds the database, uploads, page images and the OCR cache. Without a persistent disk, uploads disappear when the container is replaced. |

That rules out serverless platforms (Vercel, Netlify, AWS Lambda): their
requests time out in seconds and they have no disk. Free tiers with 512 MB of
memory (Render, Railway) are too small to start the OCR models.

## Decide who it is for

FinSight has no user accounts. Out of the box every visitor shares one pool of
documents, which is right on a laptop and wrong on a public address. Pick one
of two modes before going live.

### Public: anyone can use it, nobody sees anyone else's documents

```
FINSIGHT_WORKSPACES_ENABLED=true
FINSIGHT_RETENTION_HOURS=24
```

- **Private workspaces.** Each browser is given a random 256-bit token in an
  HttpOnly cookie and sees, searches, compares and deletes only the documents
  it uploaded. Another visitor's document answers exactly as one that does not
  exist would. The database stores a SHA-256 of the token, never the token.
- **Automatic deletion.** Every document is deleted the stated number of hours
  after upload, together with its page renders, evidence crops, and every
  cached OCR result and page image older than that. The dashboard tells
  visitors this where they upload.
- **Limits**, so one visitor cannot occupy the server: at most 2 documents in
  progress per browser, 20 uploads an hour per network address, and 10
  documents in progress across everyone, past which uploads are refused as
  "busy" rather than queued for an hour. All three are configurable below.
- **Changes need a header the dashboard sends.** A page on another site cannot
  make a visitor's browser upload or delete on their behalf.

There are no accounts, so there is nothing to sign up for and nothing to
recover: a visitor who clears their cookies or changes browser starts with an
empty workspace, and their earlier documents are deleted on schedule.

### Private: a team or a class that shares everything

```
FINSIGHT_ACCESS_PASSWORD=choose-something-long
```

Visitors get the browser's own login box; any username works with the
password. It is checked on every request, including page images. Everyone with
the password sees every document. The health check at `/api/v1/health` stays
open so the host can tell the container is alive.

Both modes can be combined. Basic authentication sends the password with each
request and the workspace cookie identifies a visitor, so **only ever serve
FinSight over HTTPS** — both options below do.

## Try the container locally

```bash
docker build -t finsight .
docker run --rm -p 7860:7860 -e FINSIGHT_ACCESS_PASSWORD=test finsight
```

Open <http://localhost:7860> and sign in with any username and `test`.

---

## Option A — Hugging Face Spaces (free, public)

The free CPU tier offers 2 cores and 16 GB of memory, which suits this app
well. Storage is temporary and the Space sleeps after a period without
visitors, so the recipe in [`deploy/huggingface/`](../deploy/huggingface/)
runs in **public mode** with 24-hour deletion: documents were never going to
outlive a restart, and this way visitors are told so in advance. Files are
limited to 25 MB and 60 pages, which keeps one document within a few minutes
on two cores.

The **GitHub repository must be public**: the Space builds by cloning it.

1. Create an account at <https://huggingface.co> and choose **New Space**.
2. Pick **Docker** as the SDK and **Blank** as the template. Choose public or
   private.
3. In the Space's **Files** tab, upload the two files from
   [`deploy/huggingface/`](../deploy/huggingface/): `Dockerfile` and
   `README.md`, replacing the README the Space was created with.
4. Nothing to configure for a public instance. To make it a private one
   instead, add a **secret** named `FINSIGHT_ACCESS_PASSWORD` under
   **Settings → Variables and secrets**; to change the limits or the retention
   window, add the variables from the table below there too.
5. The Space builds on its own (the first build takes several minutes) and
   then shows the dashboard.

Each rebuild picks up the latest commit on `main` automatically. To rebuild
without changing anything, use **Settings → Factory rebuild**.

If the password box does not appear inside the Space page, open the Space's
direct address instead: `https://<your-username>-<space-name>.hf.space`.

Expect a 3-page statement in seconds and a 33-page investor deck in roughly
8–10 minutes on two cores.

## Option B — your own server (always on, keeps its data)

Any Linux server with Docker and 4 GB of memory. A small plan with 2 cores and
4 GB from most providers costs a few dollars a month.

```bash
git clone https://github.com/tahalok2005-alt/Finsight.git
cd Finsight
FINSIGHT_ACCESS_PASSWORD=choose-something-long docker compose up -d --build
```

`docker-compose.yml` keeps `data/` in a named volume, restarts the container
if it stops, and listens on `127.0.0.1:7860` only — not on the public network
— so that HTTPS can be put in front of it. With [Caddy](https://caddyserver.com),
which obtains and renews the certificate on its own, the whole configuration
is:

```
finsight.example.com {
    reverse_proxy 127.0.0.1:7860
}
```

Point the domain's DNS at the server first. If you use nginx instead, raise
`client_max_body_size` to at least `200m`, or uploads over 1 MB are refused
before they reach FinSight.

To update:

```bash
git pull
docker compose up -d --build
```

## Configuration

Every setting in [`.env.example`](../.env.example) can be passed as an
environment variable. The ones that matter for a deployment:

| Variable | Default in the container | Purpose |
|---|---|---|
| `FINSIGHT_WORKSPACES_ENABLED` | `false` (`true` on the Space) | Public mode: a private workspace per browser |
| `FINSIGHT_RETENTION_HOURS` | unset (`24` on the Space) | Delete documents and their cached data this long after upload |
| `FINSIGHT_MAX_ACTIVE_PER_WORKSPACE` | `2` | Public mode: documents one browser may have in progress |
| `FINSIGHT_UPLOADS_PER_IP_PER_HOUR` | `20` | Public mode: uploads per network address per hour |
| `FINSIGHT_MAX_ACTIVE_DOCUMENTS` | `10` | Public mode: documents in progress across everyone |
| `FINSIGHT_ACCESS_PASSWORD` | unset (no login) | Shared password for the whole instance |
| `PORT` | `7860` | Port the container listens on |
| `FINSIGHT_PAGE_WORKERS` | chosen from free memory | Pages read in parallel; pin it to override |
| `FINSIGHT_MAX_UPLOAD_MB` | `200` | Largest accepted PDF |
| `FINSIGHT_MAX_PAGES` | `500` | Longest accepted PDF |
| `FINSIGHT_STATIC_DIR` | `/app/frontend/dist` | The built dashboard; unset, the API serves no pages |

## Limits worth knowing

- **One process.** Jobs queue inside the API process, so the container must
  run a single worker. Scale by giving it more cores and memory, not more
  replicas.
- **SQLite by default.** Right for one container. PostgreSQL works without
  schema changes — set `FINSIGHT_DATABASE_URL` (see [SETUP.md](SETUP.md)).
- **Workspaces are per browser, not per person.** There are no accounts, so
  nothing carries a workspace from one device to another.
- **The hourly upload limit counts network addresses.** Visitors behind one
  shared address — an office, a campus — share the allowance.
