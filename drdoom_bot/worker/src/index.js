// Entry point: bundles the character art (a binary import Node's test runner can't do)
// and hands it to the bot in worker.js.
import refImage from "./ref.jpg";
import worker from "./worker.js";

const withArt = (env) => ({ ...env, REF_IMAGE: refImage });

export default {
  fetch: (request, env, ctx) => worker.fetch(request, withArt(env), ctx),
  scheduled: (event, env, ctx) => worker.scheduled(event, withArt(env), ctx),
};
