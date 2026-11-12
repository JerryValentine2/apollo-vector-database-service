# Apollo exact-content live integration gate

## Current decision: STOP for production deployment
The regular GitHub Actions workflow runs offline tests only. A green CI check does **not** prove that the new route can retrieve real Apollo documents.

## Safe live verification
1. Deploy the **reviewed PR commit** to a separate Cloud Run staging service, preserving the current production service and its traffic.
2. Configure staging with access to the same Firestore/GCS project only after verifying least-privilege service identity and no write tests. Provide `APOLLO_WRITE_KEY` via Secret Manager, not GitHub source.
3. Choose a non-sensitive, existing Markdown/JSON document whose product ID and document ID are known. The test performs only GET requests.
4. Set `APOLLO_BASE_URL`, `APOLLO_WRITE_KEY`, `APOLLO_TEST_PRODUCT_ID`, `APOLLO_TEST_DOCUMENT_ID` in a secured runner environment. Run `python -m unittest discover -s integration_tests -v`.
5. Require an executed (not skipped) PASS for unauthorized access rejection, authorized exact content, matching IDs, and SHA-256. Record staging revision, source commit, run evidence, and decision in Apollo.
6. Only then approve merge and production deployment, followed by production smoke tests. Never copy keys into a PR or console transcript.

## Known limitations
- Existing shared write key is reused for read access; a separate read-only key is deferred.
- Exact-read does not provide deterministic product enumeration or product approval. Morpheus activation remains STOP.
- Morpheus client code must send `X-Apollo-Write-Key`, not `X-API-Key`.
- Live integration is **NOT VERIFIED** until the staging test is executed successfully.
