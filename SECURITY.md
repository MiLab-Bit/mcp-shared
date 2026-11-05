# mcp-shared — Security Review Policy

Shared threats: missing/invalid bearer authentication, key fingerprint leakage, untrusted provider URL and HTTP response, Supabase tenant privilege, credentials in logs, unintended data writes, insecure usage metering and permissive /health routing.

Do not use real provider tokens or prod endpoints in tests. Failure of telemetry does not grant access. Strictly protect token parsing and SSRF guard semantics. Report SHA/file and demonstrated effect using synthetic fixtures. No exploitable details in public PR; coordinate private disclosures with owner or GitHub private advisory if available. No certification or production authorization is implied.
