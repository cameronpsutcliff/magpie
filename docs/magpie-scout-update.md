Magpie Scout: prompt v4

Magpie now has a constraints lane: official actions that make AI harder, slower or costlier to train, publish, host, use or buy. Read the updated prompt; it replaces v3. Both files are public, so no token is needed to read them:

- https://raw.githubusercontent.com/cameronpsutcliff/magpie/main/docs/magpie-scout-prompt.md
- https://raw.githubusercontent.com/cameronpsutcliff/magpie/main/config/scout.json

What changed:

1. **Politics stays out, with one exception.** Positions, op-eds, predictions, hearings and the fight over who is right are still skipped. An official action with a document is now in scope: a law or bill text, a rule, an executive order, an export-control list, a procurement ban, a regulator or court decision, or a company's own notice that it is restricting access to comply with one. See the new Constraints section.
2. **New item kind `constraint`.** Put the document (or the company's notice) first in `links` and add `jurisdiction`, `issuer` and `status` (proposed, draft, final, in_force, withdrawn). Magpie routes these to its constraints section instead of the capability list. Report what the action does, never take a side.
3. **Job 3: Sunday price check** (cron `0 9 * * 0` Central). For each device in `config/hardware.json`, record the maker's list price and the lowest new, in-stock price at one major US retailer, with the page you read it on, by appending to `inbox/hardware-prices.json`. No X reads. Skip any site that shows a sign-in, CAPTCHA or bot check.
4. **One more file you may write:** `inbox/hardware-prices.json`. Everything else under "Workspace and credentials" is unchanged.
5. Write `"prompt": "v4"` in sweep and status files from now on.

Please do not edit the prompt or `config/` yourself. If something in v4 does not work as written, add it under `prompt_feedback` in `inbox/x-status.json` and keep going.
