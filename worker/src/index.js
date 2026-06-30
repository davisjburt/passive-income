// Cloudflare Worker — reliably triggers the GitHub "wheel" workflow on a cron.
//
// GitHub's own scheduled (cron) events are best-effort and frequently delayed or
// dropped, especially for short intervals. A workflow_dispatch, by contrast, runs
// promptly. So this Worker fires on Cloudflare's (reliable) cron and pokes GitHub
// to dispatch the wheel workflow every 15 minutes during market hours.

const REPO = "davisjburt/passive-income";
const WORKFLOW = "wheel.yml";

async function dispatch(env) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
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
    throw new Error(`dispatch ${res.status}: ${await res.text()}`);
  }
}

export default {
  // Cron Trigger entrypoint (see wrangler.toml [triggers]).
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      dispatch(env)
        .then(() => console.log("wheel workflow dispatched"))
        .catch((e) => console.log(String(e))),
    );
  },

  // Visiting the Worker URL triggers a run manually — handy for testing.
  async fetch(request, env) {
    try {
      await dispatch(env);
      return new Response("dispatched wheel workflow\n");
    } catch (e) {
      return new Response(String(e) + "\n", { status: 502 });
    }
  },
};
