# brainfreeze studio: instructions for Claude Code

Follow [skills/brainfreeze-studio/SKILL.md](skills/brainfreeze-studio/SKILL.md) exactly. It is the same setup
contract GitHub Copilot uses.

- Do the work yourself. The person only signs in (`az login`), picks the environment from their own list, says yes,
  and says "publish it" if they want it published.
- Deploy as a Draft (`--draft`). Publish only when they ask.
- With Claude in Chrome, do step 6 yourself: open the `maker:` link, ask the agent one question in Copilot Studio's
  test chat, and show the person the reply.
- Quote the build's `parity:` lines. Never claim a translation the build refused.
- Never print or save a token, use a service account, or delete anything in their environment.
