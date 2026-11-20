# Magpie Domain Glossary

Canonical domain language for Magpie. Use these exact terms in specs, code, tests, ADRs, and reviews.

| Term | Meaning |
|---|---|
| **Edition** | The dated publication for one Chicago calendar day, built by the home tier, cloud tier, or backfill. |
| **Item** | A normalized observation from a collector, before grouping and editorial selection. |
| **Event** | A ranked editorial record built from one or more items associated with a canonical key. |
| **Canonical key** | The stable identity used to group observations and track the same subject across editions. |
| **Capability** | A tool, model, or development that changes what someone can build or do with AI. |
| **Follow-up** | An event about a capability an earlier edition already covered. Its editorial focus is what changed. |
| **Library** | Capabilities from earlier editions that can be recalled as partners for current arrivals. |
| **Possibility** | A proposed use of capabilities, with a recipe and a falsifiable test that could disprove it. |
| **Ingredient** | A capability used in a possibility. An earlier possibility can be referenced via `builds_on`. |
| **Register** | The cumulative record of constraints or threats across editions, also used to supply recent fallbacks. |
