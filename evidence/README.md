# Prospective shadow evidence

The `shadow-evidence` branch is the durable append-only ledger for scheduled research decisions.
Its `evidence/records/` directory contains content-hashed schema-v2 JSON records created before
later market outcomes are known. Each record binds the frozen strategy/configuration hash, the
complete Python-package implementation hash, the exact input-data snapshot hash, data-gate state,
and hypothetical action.

The daily workflow publishes a record only after the local provenance and integrity audit passes.
A failed independent-data gate is still archived because operational failures are part of honest
prospective evidence, but the workflow remains visibly failed and the record authorizes no action.

Git history supplies durable timestamps and makes deletion or revision visible. The main branch
contains no generated records; it contains only this format description.
