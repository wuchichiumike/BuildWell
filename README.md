**BuildWell**

A collaborative management MVP for materials and worker welfare in construction projects. Business facts, inventory balances, and audit records are managed by a FastAPI backend; the frontend provides a project workbench; the on-chain component defaults to an explicitly labeled Mock Evidence Adapter.

**Local Execution**

Commands:

* make install
* make backend (Access at http://localhost:8000/docs)
* make frontend (Access at http://localhost:5173)

The make command must be executed inside the project directory. If your terminal is not currently in the project directory, run:
make -C "/Users/mikewu/Desktop/海之子" dev

The backend defaults to a local SQLite demo database, loading reproducible demo data for P001 upon startup. The current storage layer uses an SQLite demo adapter; production deployments require replacing it with a PostgreSQL/SQLAlchemy adapter and replacing file storage and evidence adapters with their actual implementations.

Additional commands:

* make test
* make reset-demo (Clears the demo namespace only when DEMO_MODE=true)

The API prefix is /api/v1. Demo login selects fixed users via X-Demo-User or X-User-Id request headers (owner@example.test, procurement@example.test, supplier@example.test, site@example.test, inspector@example.test, auditor@example.test); the server ignores forged role fields in requests. Production deployments should integrate OIDC/Bearer sessions. All write requests require an Idempotency-Key; confirmation actions also require a one-time confirmation_token.

The frontend uses the business API by default; offline fixtures from frontend/src/data.ts are loaded only when VITE_USE_MOCK=true is explicitly set. Offline mode is flagged at the top of the page and does not execute business writes. File download links are signed by the backend with expiration times; set an independent DOCUMENT_SIGNING_SECRET in production.

**Demo Acceptance Workflow**

By default, project P001 contains a partially shipped order alongside pending inspection and handover records. Switch roles in the top-right corner to process the workflow based on the real state machine:

1. Supplier: Submits remaining order shipments under Procurement Collaboration -> Orders & Shipments.
2. Site Manager: Registers arrival within the same order details and confirms using the one-time token returned by the system.
3. Inspection Supervisor: Registers passed and failed quantities and confirms; only passed quantities generate inventory check-in records, while failed quantities enter quarantine.
4. Project Owner & Supplier: Separately confirm their respective sides on the handover form under Evidence Traceability; transfer records and handover evidence events are generated only after the second confirmation.

The AI Site Assistant restores persisted threads based on the active account; clearing the current thread resets only the session view, whereas runs, tool calls, and audit logs remain preserved in the backend. Each source entry in daily reports links directly to business records, inventory, or calculation results.

**Scope Boundaries**

AI reads and calculates exclusively through controlled tools to generate draft proposals; formal procurement, receiving, inspection, and evidence anchoring still require manual confirmation by authorized personnel. The database preserves complete business records, whereas the evidence adapter submits only summaries and SHA-256 hashes; the demo environment does not pretend to be a real Fabric network.