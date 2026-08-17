# MidTerm positive-method transfer ablation (S001-S010 rebalanced)

The benchmark, ShortTerm/LongTerm traces, C3 Pages, P2 Queries, and A4 rankings are frozen. 
A0-A3 only perform offline embedding/ranking; A4 is copied from the existing C3 ranking artifact.

| Config | Gold@5 | R@5 | Δ vs A0 | Δ vs Previous | Δ vs C3 | R@10 | R@20 | MRR | Short+Mid Mean | All Memory Mean |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A0 | 238/386 | 61.66% | +0 | — | +59 | 77.98% | 89.38% | 0.4290 | 81.40% | 85.18% |
| A1 | 229/386 | 59.33% | -9 | -9 | +50 | 75.65% | 89.12% | 0.4116 | 80.26% | 84.16% |
| A2 | 217/386 | 56.22% | -21 | -12 | +38 | 71.50% | 87.31% | 0.3679 | 78.50% | 82.28% |
| A3 | 182/386 | 47.15% | -56 | -35 | +3 | 63.47% | 82.38% | 0.3566 | 74.06% | 78.57% |
| A4 | 179/386 | 46.37% | -59 | -3 | +0 | 61.66% | 80.05% | 0.3336 | 73.45% | 78.37% |

| Config | Mean Gold Rank | Mid Mean | Short+Mid 100% | All Memory 100% | Three-layer union | Promoted / Demoted / Net vs Previous |
|---|---:|---:|---:|---:|---:|---:|
| A0 | 7.9275 | 31.27% | 594/742 (80.05%) | 625/742 (84.23%) | 669/786 | 0 / 0 / +0 |
| A1 | 8.2668 | 30.12% | 585/742 (78.84%) | 617/742 (83.15%) | 661/786 | 2 / 11 / -9 |
| A2 | 9.0492 | 28.37% | 573/742 (77.22%) | 603/742 (81.27%) | 647/786 | 13 / 25 / -12 |
| A3 | 10.5000 | 23.92% | 538/742 (72.51%) | 575/742 (77.49%) | 619/786 | 28 / 63 / -35 |
| A4 | 11.5104 | 23.32% | 535/742 (72.10%) | 574/742 (77.36%) | 618/786 | 7 / 10 / -3 |

## Conclusions

1. P2 Query Resolution: A1 vs A0 is -9 Gold@5; not positive on the new dataset.
2. Removing Raw User under Production Add: A2 vs A1 is -12 Gold@5; under Context Add, A4 vs A3 is -3.
3. Context Add: A3 vs A2 is -35 Gold@5; the full-page controlled comparison A3 vs A1 is -47.
4. Strict A0→A1→A2→A3→A4 positive progression: NO (A0->A1 -9, A1->A2 -12, A2->A3 -35, A3->A4 -3).
5. Best tested main configuration by Gold@5: A0 (238/386).
   A0, A1, A2, and A3 exceed frozen C3 by +59, +50, +38, and +3 Gold@5 respectively; these differences are reported as observed and were not tuned away.

Promoted/demoted/net Gold counts are recorded in `main_metrics.csv` and `gold_transitions.csv`.

## Generalization classification

- Stable generalization on the primary Gold@5 metric: none of the transferred incremental optimizations.
- Failed on the new dataset: P2 Query Resolution, Production Summary+Keywords, Context Add, and Context Summary+Keywords all reverse their old positive stage delta.
- Partial only: P0 loses 2 Gold@5 but preserves A0's R@20 and three-layer union; this is tail/union robustness, not a positive routed-R@5 transfer.

## Frozen-source audit

- Historical offline A0 and the end-to-end Production trace have identical ordered Top5 for 385/386 routed Queries.
- The 1 mismatch changes no requirement-level Gold@5 hit. A0 follows the old offline C0 candidate contract; the Production trace is retained only as an audit.

## Secondary P0/P1 transfer

| Config | Gold@5 | R@5 | Δ vs A0 | R@10 | R@20 | MRR |
|---|---:|---:|---:|---:|---:|---:|
| P0 | 236/386 | 61.14% | -2 | 77.46% | 89.38% | 0.4232 |
| P1 | 226/386 | 58.55% | -12 | 73.83% | 89.12% | 0.4111 |
