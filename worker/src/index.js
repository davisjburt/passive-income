// Cloudflare Worker — reliably triggers the GitHub wheel workflows on a cron.
//
// GitHub's own scheduled (cron) events are best-effort and frequently delayed or
// dropped, especially for short intervals. A workflow_dispatch, by contrast, runs
// promptly. So this Worker fires on Cloudflare's (reliable) cron and pokes GitHub
// to dispatch every wheel workflow every 5 minutes during market hours.
//
// New account = new trading workflow -- add its filename here (and remember to
// `npx wrangler deploy`, since nothing does that automatically). recap.yml is
// the one workflow that reports on every account at once, so it's listed here
// same as any trading workflow but never needs to grow with new accounts.

const REPO = "davisjburt/passive-income";
const WORKFLOWS = ["wheel.yml", "wheel-aggressive.yml", "recap.yml"];

async function dispatchOne(env, workflow) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "wheel-cron-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  if (!res.ok) {
    throw new Error(`dispatch ${workflow} ${res.status}: ${await res.text()}`);
  }
}

async function dispatch(env) {
  // Independent workflows/accounts -- one failing shouldn't block the other.
  const results = await Promise.allSettled(WORKFLOWS.map((w) => dispatchOne(env, w)));
  const failures = results
    .map((r, i) => (r.status === "rejected" ? `${WORKFLOWS[i]}: ${r.reason}` : null))
    .filter(Boolean);
  if (failures.length) {
    throw new Error(failures.join(" | "));
  }
}

export default {
  // Cron Trigger entrypoint (see wrangler.toml [triggers]).
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      dispatch(env)
        .then(() => console.log("wheel workflows dispatched"))
        .catch((e) => console.log(String(e))),
    );
  },

  // Visiting the Worker URL triggers a run manually — handy for testing.
  async fetch(request, env) {
    try {
      await dispatch(env);
      return new Response("dispatched wheel workflows\n");
    } catch (e) {
      return new Response(String(e) + "\n", { status: 502 });
    }
  },
};
