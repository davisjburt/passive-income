# wheel-cron — Cloudflare Worker that reliably triggers the wheel workflow

GitHub's scheduled (cron) workflow runs are best-effort and often delayed/dropped.
This Worker runs on Cloudflare's reliable Cron Trigger and calls GitHub's
`workflow_dispatch` API (which runs promptly) to fire the wheel workflow every
15 minutes during market hours.

## 1. Create a GitHub token (least privilege)

GitHub → **Settings → Developer settings → Fine-grained tokens → Generate new token**:
- **Repository access:** Only select repositories → `passive-income`
- **Permissions:** **Actions → Read and write** (this is what allows dispatch)
- Generate and copy the token (starts with `github_pat_`).

## 2. Deploy the Worker

### Option A — Wrangler CLI (from this `worker/` directory)
```bash
npx wrangler login                 # opens browser to authorize Cloudflare
npx wrangler deploy                # deploys the Worker + cron trigger
npx wrangler secret put GH_TOKEN   # paste the GitHub token when prompted
```

### Option B — Cloudflare dashboard
1. **Workers & Pages → Create → Create Worker**, name it `wheel-cron`, deploy the
   placeholder, then **Edit code** and paste `src/index.js`. Save & deploy.
2. **Settings → Triggers → Cron Triggers → Add**: `*/15 13-21 * * 1-5`.
3. **Settings → Variables and Secrets → Add → Secret**: name `GH_TOKEN`, value =
   your GitHub token. Save.

## 3. Test it

Visit the Worker's URL (e.g. `https://wheel-cron.<your-subdomain>.workers.dev`) —
it dispatches the workflow immediately and replies `dispatched wheel workflow`.
Then check the repo's **Actions** tab for a new `wheel` run within seconds.

## Notes
- The token only needs Actions read/write on the one repo; it cannot touch your
  Alpaca account (those keys live in GitHub Actions secrets, not here).
- Once this is confirmed working, you can delete the `schedule:` block from
  `.github/workflows/wheel.yml` to rely solely on the Worker (or leave it as a
  harmless best-effort fallback — runs are idempotent).
