# brainfreeze studio: instructions for Claude Code

Follow [skills/brainfreeze-studio/SKILL.md](skills/brainfreeze-studio/SKILL.md) exactly. It is the same setup
contract GitHub Copilot uses.

- Do the work yourself. The person only signs in (`az login`), picks the environment from their own list, approves
  the plan, and says "publish it" if they want it published.
- Say that the build runs the agents' Python on this computer to prove translations. It isn't a sandbox, so build
  only code they trust.
- Build once. Plan that build with `--plan` (it changes nothing), show them what it would create, update or remove,
  then deploy exactly that build with the plan's `--expect <digest>`. Never rebuild in between.
- Deploy as a Draft (`--draft`). Publish only when they ask.
- With Claude in Chrome, do step 6 yourself: open the `maker:` link, ask the agent one question in Copilot Studio's
  test chat, and show the person the reply you saw. Without a browser, give them the link and the question, and say
  you haven't seen the reply.
- Quote the build's `parity:` lines. Never claim a translation the build refused.
- Never print or save a token, use a service account or someone else's connection, take over an agent that isn't
  theirs, or change anything in their environment beyond what the plan lists.
