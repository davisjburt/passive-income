// Cloudflare Worker — reliably triggers the GitHub wheel workflows on a cron.
//
// GitHub's own scheduled (cron) events are best-effort and frequently delayed or
// dropped, especially for short intervals. A workflow_dispatch, by contrast, runs
// promptly. So this Worker fires on Cloudflare's (reliable) 5-minute cron and
// pokes GitHub to dispatch workflows during market hours.
//
// The trading workflows (wheel.yml, wheel-aggressive.yml) don't need 5-minute
// resolution -- positions move on a DTE timescale of days to weeks, so they're
// only dispatched every WHEEL_INTERVAL_MIN minutes. recap.yml (market open/close
// summary) stays on every tick so it still catches the open/close moment
// precisely. This is a software throttle, not a separate cron: Cloudflare cron
// triggers can't easily overlap two intervals without double-firing on the
// shared minutes, so instead every 5-minute tick runs and the code below decides
// which workflows are due.
//
// New account = new trading workflow -- add its filename to WHEEL_WORKFLOWS (and
// remember to `npx wrangler deploy`, since nothing does that automatically).
// recap.yml reports on every account at once, so it never needs to grow with
// new accounts.

const REPO = "davisjburt/passive-income";
const WHEEL_WORKFLOWS = ["wheel.yml", "wheel-aggressive.yml"];
const RECAP_WORKFLOW = "recap.yml";
const WHEEL_INTERVAL_MIN = 15; // must be a multiple of the 5-min cron tick below

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

async function dispatch(env, workflows) {
  // Independent workflows/accounts -- one failing shouldn't block the other.
  const results = await Promise.allSettled(workflows.map((w) => dispatchOne(env, w)));
  const failures = results
    .map((r, i) => (r.status === "rejected" ? `${workflows[i]}: ${r.reason}` : null))
    .filter(Boolean);
  if (failures.length) {
    throw new Error(failures.join(" | "));
  }
}

// recap.yml fires every tick; the wheel workflows only on ticks that land on a
// WHEEL_INTERVAL_MIN boundary (:00, :15, :30, :45 for the default 15).
function workflowsDueAt(scheduledTimeMs) {
  const minute = new Date(scheduledTimeMs).getUTCMinutes();
  return minute % WHEEL_INTERVAL_MIN === 0
    ? [...WHEEL_WORKFLOWS, RECAP_WORKFLOW]
    : [RECAP_WORKFLOW];
}

export default {
  // Cron Trigger entrypoint (see wrangler.toml [triggers]).
  async scheduled(event, env, ctx) {
    const workflows = workflowsDueAt(event.scheduledTime);
    ctx.waitUntil(
      dispatch(env, workflows)
        .then(() => console.log(`dispatched: ${workflows.join(", ")}`))
        .catch((e) => console.log(String(e))),
    );
  },

  // Visiting the Worker URL triggers every workflow immediately, regardless of
  // the 15-minute throttle above — handy for testing.
  async fetch(request, env) {
    try {
      await dispatch(env, [...WHEEL_WORKFLOWS, RECAP_WORKFLOW]);
      return new Response("dispatched all workflows\n");
    } catch (e) {
      return new Response(String(e) + "\n", { status: 502 });
    }
  },
};
