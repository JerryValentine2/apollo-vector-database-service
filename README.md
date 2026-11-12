# Apollo Vector Database Service

Source-code repository for the existing Apollo backend deployed on Google Cloud Run in `harmonia-dev-20260204` (`us-central1`). Apollo owns its Firestore/vector-search implementation and document storage; consumers such as Morpheus access Apollo through its HTTP API.

## Recovery checkpoint

The recovered Cloud Run source archive (`apollo-source.zip`) contains `app.py`, `apollo_v12.py`, `Dockerfile`, and `requirements.txt`. The archive was recovered from the deployed service source, not authored as a replacement. **The full recovered source is not yet imported to this repository.** Do not deploy from this repository until all four original files are committed and their hashes verified against the archive.

Recovered archive SHA-256: `03ba0bf02e0b384502cf8ea57f756a232895b60914cfc52a7eb46a03fddb3bdd`

Recovered `app.py` SHA-256: `e871603cc24bf64db772b1a1510fbe8bf18952e9111178a8e970104a2cd80b96`

## Governance

GitHub source → automated tests → reviewed merge → Google Cloud Run deployment → runtime verification. Never commit credentials or product knowledge. No production deployment from unvalidated recovery files.
