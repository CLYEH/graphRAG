"""Entity resolution & review carry-forward primitives.

P3 freezes the review-side contracts (DESIGN §17/§27.3): stable fingerprints
(`fingerprints`) and the review state machine + ledger precedence (`review`).
The ER pipeline itself (blocking/similarity/merge) lands with C4.
"""
