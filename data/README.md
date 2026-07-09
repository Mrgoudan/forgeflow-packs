# forgeflow knowledge (data repo)

Git-tracked projection of the living forgeflow DB (`run/state/forgeflow.db`).

- Regenerate: `run-bsc.sh export` (writes `forgeflow.knowledge.sql`)
- Rebuild a DB: `run-bsc.sh import`

**PRIVATE — holds security findings (compiler vulns) + reviewed-code snippets.**
Encrypt before pushing to any remote (git-crypt / age). Contains no secrets
(verified: GLM key + forge token never appear here).
