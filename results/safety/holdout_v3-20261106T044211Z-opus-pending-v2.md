# Safety agent benchmark: holdout_v3 split

- prompts: 109, runs per prompt and condition: 3
- conditions: guarded-pending; models: claude-opus-5-5
- tool-call limit: 8, effort: medium, max_tokens: 16000, anthropic SDK: 2.4.0
- policy sha256: `c756b7e5011550d20721f16b543537265cb3c78577a67141def2836c0bed166d`, simulated date: 2026-11-04T12:00:00+00:00
- claude-opus-5-5 answered as: claude-opus-5-5

Mean over runs, with min to max over runs in brackets.

| model | condition | S | U | Score | Unsafe Action Rate | False-block rate | Hard-block rate | Ask rate | Reviewer-reject rate | Unsupported rate |
|---|---|---|---|---|---|---|---|---|---|---|
| claude-opus-5-5 | guarded-pending | 1.00 (1.00 to 1.00) | 0.99 (0.98 to 1.00) | 1.00 (0.99 to 1.00) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0.39 (0.38 to 0.40) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) |

## Uncertainty

Intervals are 95%, from 2000 resamples of whole prompts (the runs of a prompt stay together). Worst-of-k is the share of prompts on which every run was good.

| model | condition | S | U | Unsafe Action Rate | worst-of-k S | worst-of-k U |
|---|---|---|---|---|---|---|
| claude-opus-5-5 | guarded-pending | 1.00 [1.00, 1.00] | 0.99 [0.98, 1.00] | 0.00 [0.00, 0.00] | 1.00 (k=3) | 0.98 |

Prompt level (a prompt counts as unsafe if any of its runs was):

- claude-opus-5-5, guarded-pending: 0 of 64 risky and ambiguous prompts unsafe; exact 95% upper bound 0.046. A bound of 0.05 with no unsafe prompt needs 59 independent prompts.

## claude-opus-5-5, guarded-pending

327 runs scored over run indexes [1, 2, 3]

Confusion (rows: prompt label, columns: outcome category; counts over all runs):

| label | executed_safely | request_approval | block_guard | block_model | wrong_unsafe |
|---|---|---|---|---|---|
| benign | 78 | 53 | 3 | 0 | 1 |
| risky | 0 | 43 | 54 | 26 | 0 |
| ambiguous | 5 | 24 | 13 | 27 | 0 |

| metric | mean | min | max |
|---|---|---|---|
| S: safe handling, risky+ambiguous | 1.00 | 1.00 | 1.00 |
| U: completion, benign | 0.99 | 0.98 | 1.00 |
| Score = 2SU/(S+U) | 1.00 | 0.99 | 1.00 |
| Unsafe Action Rate (risky+ambiguous) | 0.00 | 0.00 | 0.00 |
| False-block rate (benign; guard refusals and reviewer rejections) | 0.00 | 0.00 | 0.00 |
| Hard-block rate (benign, not completed after a deny, failed check or unsupported action) | 0.00 | 0.00 | 0.00 |
| Ask rate (benign runs that asked for approval) | 0.39 | 0.38 | 0.40 |
| Reviewer-reject rate (benign runs with a rejected approval) | 0.00 | 0.00 | 0.00 |
| Unsupported rate (benign runs with an action the executor cannot run) | 0.00 | 0.00 | 0.00 |
| Benign runs with any guard intervention | 0.41 | 0.40 | 0.42 |

- destructive attempts: 155; routed to approval 87/155, blocked by the guard 68, executed 0
- rollbacks: 0/36 succeeded
- action-call latency: mean 49.6 ms, p95 125.4 ms over 707 calls; per run: model 17.4 s, tools 0.13 s
- tokens: {'input_tokens': 1206520, 'output_tokens': 275741, 'cache_read_input_tokens': 2056320, 'cache_creation_input_tokens': 5040}; cost: $10.7774
