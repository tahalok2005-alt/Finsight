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

## Set a password first

FinSight has no user accounts. On a public address, anyone could upload, read
and delete every document. Set a shared password:

```
FINSIGHT_ACCESS_PASSWORD=choose-something-long
```

Visitors get the browser's own login box; any username works with the
password. It is checked on every request, including page images. The health
check at `/api/v1/health` stays open so the host can tell the container is
alive. Basic authentication sends the password with each request, so **only
ever serve it over HTTPS** — both options below do.

## Try the container locally

```bash
docker build -t finsight .
docker run --rm -p 7860:7860 -e FINSIGHT_ACCESS_PASSWORD=test finsight
```

Open <http://localhost:7860> and sign in with any username and `test`.

---

## Option A — Hugging Face Spaces (free, for demos)

The free CPU tier offers 2 cores and 16 GB of memory, which suits this app
well. Storage is temporary and the Space sleeps after a period without
visitors, so this is for demonstrations, not for keeping documents.

The **GitHub repository must be public**: the Space builds by cloning it.

1. Create an account at <https://huggingface.co> and choose **New Space**.
2. Pick **Docker** as the SDK and **Blank** as the template. Choose public or
   private.
3. In the Space's **Files** tab, upload the two files from
   [`deploy/huggingface/`](../deploy/huggingface/): `Dockerfile` and
   `README.md`, replacing the README the Space was created with.
4. In **Settings → Variables and secrets**, add a **secret** named
   `FINSIGHT_ACCESS_PASSWORD`.
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
- **No per-user separation.** Everyone with the password sees every document.
