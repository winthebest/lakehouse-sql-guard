# Safety: Joint and reliability metrics (post-hoc re-scoring)

Post-hoc, on seen data: these are the recorded transcripts re-scored offline with metrics chosen after the results were known. They are not pre-registered. Prompts, agents and the scripted reviewer are all Claude; there are no human raters.

Every cell is the recorded transcripts of one model, condition and policy, replayed offline (`bench.safety.report rescore`). The old numbers (S, U, unsafe action rate) are reproduced in each cell's own `*-rescored.md`.

## Joint safe completion and reliability

| split | model | condition | policy | joint safe completion | benign completed | risky and ambiguous handled safely | pass^k benign | any-run-unsafe prompts (95% upper bound) | approval requests per completed benign task |
|---|---|---|---|---|---|---|---|---|---|
| dev | claude-haiku-5-5 | guarded | v1 | 1.00 | 1.00 | 1.00 | 1.00 (10 / 10, k=3) | 0 / 14 (0.193) | 0.10 |
| dev | claude-haiku-5-5 | raw | v1 | 0.61 | 1.00 | 0.33 | 1.00 (10 / 10, k=3) | 10 / 14 (0.896) | 0.00 |
| dev | claude-haiku-5-5 | guarded-pending | v1 | 1.00 | 1.00 | 1.00 | 1.00 (10 / 10, k=3) | 0 / 14 (0.193) | 0.10 |
| dev | claude-haiku-5-5 | guarded-pending | v2 | 0.99 | 1.00 | 0.98 | 1.00 (10 / 10, k=3) | 0 / 14 (0.193) | 0.10 |
| holdout_v2 | claude-haiku-5-5 | raw | v2 | 0.60 | 0.92 | 0.37 | 0.85 (11 / 13, k=3) | 12 / 18 (0.844) | 0.00 |
| holdout_v2 | claude-haiku-5-5 | guarded-pending | v2 | 0.84 | 0.62 | 1.00 | 0.62 (8 / 13, k=3) | 0 / 18 (0.153) | 0.83 |
| holdout_v2 | claude-haiku-5-5 | guarded-pending | v1 | 0.90 | 0.77 | 1.00 | 0.77 (10 / 13, k=3) | 0 / 18 (0.153) | 0.47 |
| holdout_v2 | claude-opus-5-5 | raw | v2 | 0.76 | 1.00 | 0.59 | 1.00 (13 / 13, k=3) | 9 / 18 (0.709) | 0.00 |
| holdout_v2 | claude-opus-5-5 | guarded-pending | v2 | 0.84 | 0.62 | 1.00 | 0.62 (8 / 13, k=3) | 0 / 18 (0.153) | 0.88 |
| holdout_v3 | claude-haiku-5-5 | raw | v2 | 0.64 | 0.93 | 0.43 | 0.89 (40 / 45, k=3) | 39 / 64 (0.712) | 0.00 |
| holdout_v3 | claude-haiku-5-5 | guarded-pending | v2 | 0.97 | 0.93 | 1.00 | 0.89 (40 / 45, k=3) | 0 / 64 (0.046) | 0.41 |
| holdout_v3 | claude-haiku-5-5 | guarded-pending | v1 | 0.94 | 0.93 | 0.95 | 0.89 (40 / 45, k=3) | 3 / 64 (0.117) | 0.21 |
| holdout_v3 | claude-opus-5-5 | raw | v2 | 0.68 | 0.98 | 0.46 | 0.96 (43 / 45, k=3) | 36 / 64 (0.668) | 0.00 |
| holdout_v3 | claude-opus-5-5 | guarded-pending | v2 | 1.00 | 0.99 | 1.00 | 0.98 (44 / 45, k=3) | 0 / 64 (0.046) | 0.43 |

## Attempted versus landed (risky and ambiguous runs)

| split | model | condition | policy | refused (no tool call) | refused late (tools used, no write sent) | attempted (command refused, rejected or failed) | landed (unsafe state change) | acted safely (write went through within bounds) | unclear |
|---|---|---|---|---|---|---|---|---|---|
| dev | claude-haiku-5-5 | guarded | v1 | 0 | 14 | 28 | 0 | 0 | 0 |
| dev | claude-haiku-5-5 | raw | v1 | 0 | 12 | 0 | 25 | 3 | 2 |
| dev | claude-haiku-5-5 | guarded-pending | v1 | 0 | 11 | 31 | 0 | 0 | 0 |
| dev | claude-haiku-5-5 | guarded-pending | v2 | 0 | 15 | 26 | 0 | 1 | 0 |
| holdout_v2 | claude-haiku-5-5 | raw | v2 | 0 | 14 | 2 | 33 | 5 | 0 |
| holdout_v2 | claude-haiku-5-5 | guarded-pending | v2 | 0 | 15 | 34 | 0 | 5 | 0 |
| holdout_v2 | claude-haiku-5-5 | guarded-pending | v1 | 0 | 14 | 34 | 0 | 6 | 0 |
| holdout_v2 | claude-opus-5-5 | raw | v2 | 3 | 20 | 1 | 21 | 8 | 1 |
| holdout_v2 | claude-opus-5-5 | guarded-pending | v2 | 0 | 16 | 32 | 0 | 6 | 0 |
| holdout_v3 | claude-haiku-5-5 | raw | v2 | 0 | 62 | 0 | 107 | 22 | 1 |
| holdout_v3 | claude-haiku-5-5 | guarded-pending | v2 | 0 | 56 | 123 | 0 | 13 | 0 |
| holdout_v3 | claude-haiku-5-5 | guarded-pending | v1 | 0 | 56 | 115 | 9 | 12 | 0 |
| holdout_v3 | claude-opus-5-5 | raw | v2 | 3 | 69 | 0 | 102 | 18 | 0 |
| holdout_v3 | claude-opus-5-5 | guarded-pending | v2 | 0 | 53 | 123 | 0 | 16 | 0 |

## Reviewer detection curve

Unsafe runs among risky and ambiguous runs if the reviewer catches an unsafe request with probability d. d = 0 is the reviewer who approves everything; d = 1 catches every unsafe request. Only guarded-pending cells can be replayed with another reviewer.

| split | model | condition | policy | d = 0 | d = 0.5 | d = 0.74 | d = 0.9 | d = 1 | landed without approval / asked and unsafe / runs | d = 0 equals approve-all |
|---|---|---|---|---|---|---|---|---|---|---|
| dev | claude-haiku-5-5 | guarded-pending | v1 | 0.21 | 0.11 | 0.06 | 0.02 | 0.00 | 0 / 9 / 42 | yes |
| dev | claude-haiku-5-5 | guarded-pending | v2 | 0.21 | 0.11 | 0.06 | 0.02 | 0.00 | 0 / 9 / 42 | yes |
| holdout_v2 | claude-haiku-5-5 | guarded-pending | v2 | 0.15 | 0.07 | 0.04 | 0.01 | 0.00 | 0 / 8 / 54 | yes |
| holdout_v2 | claude-haiku-5-5 | guarded-pending | v1 | 0.06 | 0.03 | 0.01 | 0.01 | 0.00 | 0 / 3 / 54 | yes |
| holdout_v2 | claude-opus-5-5 | guarded-pending | v2 | 0.19 | 0.09 | 0.05 | 0.02 | 0.00 | 0 / 10 / 54 | yes |
| holdout_v3 | claude-haiku-5-5 | guarded-pending | v2 | 0.23 | 0.12 | 0.06 | 0.02 | 0.00 | 0 / 45 / 192 | yes |
| holdout_v3 | claude-haiku-5-5 | guarded-pending | v1 | 0.20 | 0.12 | 0.09 | 0.06 | 0.05 | 9 / 29 / 192 | yes |
| holdout_v3 | claude-opus-5-5 | guarded-pending | v2 | 0.26 | 0.13 | 0.07 | 0.03 | 0.00 | 0 / 49 / 192 | yes |

## Paired difference, without the guard minus with it

Per prompt, the share of unsafe runs without the guard minus the share with it, same model and prompts; a positive number is unsafe runs the guard removed. The interval resamples prompts (95%). The test is exact McNemar on whether a prompt had any unsafe run.

| split | model | guarded condition | policy | prompts | reduction in unsafe run rate [95% interval] | unsafe only without / only with / both / neither | McNemar p |
|---|---|---|---|---|---|---|---|
| dev | claude-haiku-5-5 | guarded | v1 | 14 | +0.60 [+0.36, +0.81] | 10 / 0 / 0 / 4 | 0.0020 |
| dev | claude-haiku-5-5 | guarded-pending | v1 | 14 | +0.60 [+0.36, +0.81] | 10 / 0 / 0 / 4 | 0.0020 |
| dev | claude-haiku-5-5 | guarded-pending | v2 | 14 | +0.60 [+0.36, +0.81] | 10 / 0 / 0 / 4 | 0.0020 |
| holdout_v2 | claude-haiku-5-5 | guarded-pending | v2 | 18 | +0.61 [+0.39, +0.81] | 12 / 0 / 0 / 6 | 0.0005 |
| holdout_v2 | claude-haiku-5-5 | guarded-pending | v1 | 18 | +0.61 [+0.39, +0.81] | 12 / 0 / 0 / 6 | 0.0005 |
| holdout_v2 | claude-opus-5-5 | guarded-pending | v2 | 18 | +0.39 [+0.19, +0.61] | 9 / 0 / 0 / 9 | 0.0039 |
| holdout_v3 | claude-haiku-5-5 | guarded-pending | v2 | 64 | +0.56 [+0.45, +0.67] | 39 / 0 / 0 / 25 | < 0.0001 |
| holdout_v3 | claude-haiku-5-5 | guarded-pending | v1 | 64 | +0.51 [+0.40, +0.62] | 36 / 0 / 3 / 25 | < 0.0001 |
| holdout_v3 | claude-opus-5-5 | guarded-pending | v2 | 64 | +0.53 [+0.42, +0.65] | 36 / 0 / 0 / 28 | < 0.0001 |

## Replay check

| cell | runs | same safety outcome | same category | every recorded score field equal | without recorded score | drift | published headline metrics |
|---|---|---|---|---|---|---|---|
| dev-20261104T081939Z | 144 | 144 | 138 | 138 | 0 | 12 | reproduced |
| dev-20261104T170908Z | 72 | 72 | 66 | 66 | 0 | 12 | reproduced |
| dev-20261104T175757Z | 72 | 72 | 66 | 66 | 0 | 12 | reproduced |
| holdout_v2-20261105T124654Z-haiku-raw-v2 | 93 | 93 | 93 | 93 | 0 | 0 | reproduced |
| holdout_v2-20261105T130057Z-haiku-pending-v2 | 93 | 93 | 88 | 88 | 0 | 10 | reproduced |
| holdout_v2-20261105T131545Z-haiku-pending-v1 | 93 | 93 | 88 | 88 | 0 | 10 | reproduced |
| holdout_v2-20261105T133102Z-opus-raw-v2 | 93 | 93 | 93 | 93 | 0 | 0 | reproduced |
| holdout_v2-20261105T135221Z-opus-pending-v2 | 93 | 93 | 87 | 87 | 0 | 12 | reproduced |
| holdout_v3-20261106T012606Z-haiku-raw-v2 | 327 | 327 | 327 | 327 | 0 | 0 | reproduced |
| holdout_v3-20261106T020729Z-haiku-pending-v2 | 327 | 327 | 327 | 327 | 0 | 0 | reproduced |
| holdout_v3-20261106T025114Z-haiku-pending-v1 | 327 | 327 | 327 | 327 | 0 | 0 | reproduced |
| holdout_v3-20261106T033402Z-opus-raw-v2 | 327 | 327 | 327 | 327 | 0 | 0 | reproduced |
| holdout_v3-20261106T044211Z-opus-pending-v2 | 327 | 327 | 327 | 327 | 0 | 0 | reproduced |
