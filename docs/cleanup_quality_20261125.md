# Cleanup And Quality: November 25, 2026

Consolidated existing stock-shadow recovery, manual forward journal, source qualification,
refresh jobs, turnover diagnostics and dashboard work. The frozen primary ETF strategy remains
monthly momentum; rejected fast variants are still diagnostic, not promoted replacements.

Session handling no longer treats a partial file tail as month end. Prospective records
require an explicit timezone and actual NYSE close plus the existing 15-minute feed buffer,
including early-close sessions. Holiday-labelled decisions are ineligible even if recorded
later. Recovery states preserve observed paper accounting without inventing historical signals.

Portfolio exports reject missing/nonfinite targets or cash rather than imputing them; source,
package, lock and runtime identities are retained. No private journal or local dataset is
included in source control. The common-close comparison is distinct from native next-open
performance and does not remove universe or prospective-evidence limitations.

Cross-system verification and the full assessment are maintained in Portfolio Lab at
`scripts/verify_workspace.py` and `docs/system_assessment_20261125.md`.
