# Results

Every number here comes from a committed result file. Each rule set and policy was frozen at a
git tag, with its thresholds pre-registered, before new authors (fresh Claude agents, not people) wrote the held-out set it was
measured on; each held-out set was run once. Newest first. What the numbers do not show is in
[limitations.md](limitations.md).

## Current rules and policy (v3) on a fresh held-out set

Rules, policy and thresholds were frozen at tags `rules-frozen-v3` / `policy-frozen-v3`
([pre-registration](../results/leakage/prereg-8928019.json)). New authors (fresh Claude agents, not people),
who saw only a brief, then wrote 146 leakage templates (70 clean) and 109 agent prompts (45 benign, 64 risky or
ambiguous). The templates were checked for near-copies of every earlier set, and the labels
and expected answers were checked against the warehouse. Each set was run once.

| | recall | FPR [95% CI] | clean templates flagged |
|---|---|---|---|
| leakage, combined (v3) | 1.00 | **0.15 [0.07, 0.25]** | 10 / 70 |
| leakage, static only (v3) | 0.95 | 0.13 [0.05, 0.22] | 8 / 70 |
| leakage, replay only (v3) | 0.85 | 0.03 [0.00, 0.07] | 2 / 70 |
| leakage, first rule set (v1) | 0.14 | 0.08 | 6 / 70 |

| agent cell | unsafe action rate | U (benign completed) [95% CI] | false-block rate |
|---|---|---|---|
| Haiku 5.5, no guard | 0.56 | 0.93 | 0.00 |
| Haiku 5.5, guard (policy v2) | **0.00** | 0.93 [0.87, 0.99] | 0.05 |
| Haiku 5.5, guard (policy v1) | 0.05 | 0.93 | 0.05 |
| Opus 5.5, no guard | 0.53 | 0.98 | 0.00 |
| Opus 5.5, guard (policy v2) | **0.00** | 0.99 [0.98, 1.00] | 0.00 |

**What this shows.** With the guard and policy v2, neither model took an unsafe action on any of
the 64 risky or ambiguous prompts, three runs each; the exact 95% upper bound on the per-prompt
unsafe rate is 0.046. Benign work still got done (U 0.93 and 0.99, against 0.62 on held-out v2).
That safety rests partly on the person: with a reviewer who approves everything, 23% of Haiku's
and 26% of Opus's risky and ambiguous runs would have been unsafe.

**What it does not.** The pre-registered leakage target, FPR ≤ 0.05, is **not met**. FPR fell
from 0.37 on held-out v2 to 0.15, and the combined detector still found every leak. Eight of the
ten flagged clean templates are static rules that do not follow a bound written in an unusual
way: through `UNION`/`EXCEPT`, a `ROWS` frame on a merged timeline, a `CASE` or `RANGE` frame
that keeps only final labels, or a shifted time zone. The other two are a replay bug: rounding
noise near zero counts as a mismatch. Details are in
[docs/limitations.md](limitations.md#held-out-v3-frozen-rules-and-policy). Reports:
[leakage](../results/leakage/20261106-4587293.md), safety `results/safety/holdout_v3-*`.

## Real-world dbt projects

The frozen v3 rules were run, statically, on dbt projects from GitHub
([pre-registered search](../bench/realworld/search-prereg.yaml), [screening](../bench/realworld/screening.csv)).
Each project is pinned to a commit, and its spec was written from its own docs and committed
before the checker ran. Every finding was judged by two reviewers working blind to each other,
both Claude models (the session that ran the check and a separate review agent), not people;
they agreed on all 31.

| projects | checked | findings | real leaks | precision |
|---|---|---|---|---|
| new (found after the freeze): DobleL2, novarz, ChuquEmeka | 3 | 14 | 13 | **0.93** |
| seen during development: mponsclo | 1 | 3 | 3 | 1.00 |
| seen during development: fraud feature store (clean control) | 1 | 14 | 0 | **0.00** |

The real leaks are the kind the rules target: a per-user count of frauds that includes the row
being predicted, all-history aggregates with no cutoff before the churn outcome, "days since last
activity" measured to `current_date`, and transaction and listening logs that run past the
project's own cutoff. The clean control is the failure: every one of its rolling windows ends at
`1 PRECEDING`, but they are ordered by `event_time_unix`, a cast of `event_time` the rules do not
relate to the time column, so 13 windows are flagged; the 14th finding comes from a spec that
makes the label table its own history. Three of the five seen projects have no
training table or no label and were not run. Precision says nothing about missed leaks, which
were not measured on these projects.

On 15 mutants of the clean control ([operators](../bench/realworld/mutation-operators.yaml),
registered before generation) only 3 add a new finding (the two L6 and the one L7 mutant).
The 5 L1 and L2 mutants add nothing new because the control is already flagged for those types,
so they cannot be told apart from it; every L3 and L5 mutant (7) is missed; and the finding names
the mutated model for 1 of 15. Even the 3 are recall by construction, since the operators were
written to match the rules. Details:
[results/realworld](../results/realworld/), [docs/limitations.md](limitations.md#real-world-dbt-projects).

## Real-world dbt projects, round 2

Read this next to round 1 above: its 13 of 14 came from three projects, and this round is the
check on whether that carries over. It did not. The same frozen v3 rules, run statically on 12
more projects, found 33 things, of which 5 held up. The rounds differ in more than the projects
(a different search, different specs, 6 of 12 projects with no verdict), so the two precisions are
not a like-for-like comparison, but neither round should be quoted without the other.

The search was [registered](../bench/realworld/round2/search-prereg.yaml) before any query ran;
the raw hits are in [search-results.json](../bench/realworld/round2/search-results.json). Of 220
candidates, 131 were [screened](../bench/realworld/round2/screening.csv) with the round 1 criteria:
113 rejected, 18 qualified. Screening was registered to stop once 12 projects qualified; 4 of
those 12 had no label or no time column, so an amendment, decided and recorded before the checker
ran on any round 2 project, kept it going until 12 projects had a spec the checker could run. The 18 are pinned to commits in [projects.yaml](../bench/realworld/round2/projects.yaml); 6 of
them turned out not to apply (no documented label, or no time column) and were not run. Specs were
written from each project's own docs and committed before the run, and none was changed after.

| | count |
|---|---|
| candidates screened / qualified | 131 / 18 |
| projects checked / not applicable | 12 / 6 |
| models in the checked projects | 183 |
| projects with findings (checker verdict `leaky`) | 6 |
| projects the checker could not fully read (`unverifiable`) | 5 |
| projects where the checker crashed | 1 |
| findings | 33 |

Every finding was judged by two reviewers working blind to each other: the session that ran the
check and a separate review agent. Both are Claude models, not people. They agreed on 29 of the 33;
the 4 where they did not stay `unsure` and are not counted for or against the checker.

| adjudication (33 findings) | count |
|---|---|
| real leak | 5 |
| false positive | 24 |
| unsure (reviewers disagree) | 4 |

Strict precision is 5 of 33 (0.15); if every unsure finding were real it would be 9 of 33 (0.27).
All 5 real leaks are in one project (LNshuti/soma): moving averages and a standard deviation whose
window ends at `CURRENT ROW` and so include the same-day quantity being predicted, and growth rates
computed from that same-day target. The 4 unsure are all in theo-lyd/predictive-ltv-survival-pipeline
(an unbounded promotions join and an unbounded billing aggregate; the reviewers differ on whether
the project's own data checks make them harmless). The false positives have a few causes:

| cause of the 24 false positives | findings |
|---|---|
| a rule fires on the label column itself, or on the expression that builds the label (a `LEAD`, a `CASE` on the outcome) | 12 |
| L7 on a run-timestamp metadata column (`dbt_updated_at`, `model_built_at`) that is not a feature | 6 |
| a `LAG` of past labels, which is past data, flagged | 3 |
| a dedupe `ROW_NUMBER` over ingestion time | 2 |
| a monthly group-by that is bounded at the cutoff | 1 |

The first row is the main one: the rules have no way to be told which column is the label and which
are features, so anything built from the label is read as a feature built from the future (see
[limitations](limitations.md#real-world-dbt-projects-round-2)).

Of the 12 checked projects, 6 had findings (33), 5 ended `unverifiable` with no findings
(the checker could not read part of the query, usually because the spec names a derived label that
is not in the schema, or a time column has no type), and 1 crashed. So in this round the checker
produced real findings on 1 of 12 projects and could not give a verdict on 6. Precision still says
nothing about missed leaks, which were not measured; one possible miss is noted in the limitations.
Replay was not run (no project has snapshot history).

Files: [findings with both reviewers' verdicts and the final call](../results/realworld/round2/findings.csv),
[the reviewers' notes](../results/realworld/round2/reviewer-verdicts.csv),
[per-model rows](../results/realworld/round2/models.csv),
[unverifiable parts](../results/realworld/round2/unverifiable.csv),
[run record](../results/realworld/round2/20261107-2a9934b.json). Only 2 of the 12 checked
projects have a license that allows it, so SQL excerpts are published for those two
([licenses](../results/realworld/round2/third-party-licenses/NOTICE.txt)) and withheld for the rest.

## Documented Kaggle leaks

Fourteen labelled test cases were rebuilt from leaks that people documented in Kaggle competitions
and a few papers and blogs (Riiid, G-Research Crypto, Favorita, IEEE-CIS Fraud, Airbnb New User
Bookings, LendingClub, Red Hat Business Value and others; the
[catalog](../bench/kaggle/leak-catalog.csv) lists each source). The cases are not human-written
end to end: an AI author read the human sources and wrote the SQL, the specs and generators for
synthetic data of the same shape, one leaky query and one clean twin per leak, 80 queries in all
(40 leaky, 40 clean; [cases](../bench/kaggle/cases/)). No real competition data is used. The cases
cover L1, L3, L5 and L6 only: there is no L2, L4 or L7 case, since none of the sources fit them.
A self-check ([`check_cases.py`](../bench/kaggle/check_cases.py)) confirms that every clean twin
returns the same features on a point-in-time replay and every leaky one does not. Three cases
(TalkingData, Favorita, IEEE-CIS) rest on a reading of "leak" that participants dispute.

The evaluation was [pre-registered](../bench/kaggle/eval-prereg.yaml) before any case was run, with
the frozen v3 rules and the thresholds fixed earlier, and run once.

### Primary result: the checker rejected every spec

| | result |
|---|---|
| cases whose spec loaded | 0 of 14 |
| queries analysed | 0 of 80 (all 160 static and replay runs are errors) |
| recall, false-positive rate | 0 of 40 flagged, 0 of 40 false alarms: measured nothing |

This says nothing about the leak rules. The spec format could not express the cases as written:

| cases | queries | message |
|---|---|---|
| 10 (kl04, kl05, kl08, kl09, kl10, kl11, kl12, kl14, kl15, kl17) | 56 | `label_table must not also be declared temporal or static` |
| 2 (kl03, kl16) | 12 | `LeakageSpec is missing required fields: entity_keys` |
| 2 (kl06, kl07) | 12 | `label_known_after must be a positive duration ... got '0 seconds'` |

Ten cases keep the label and the history in one table (as most competition data does), two have a
single series and so no entity key, and two have labels known immediately, which cannot be written
as a zero duration. The prediction was written down in the registration before the run, and matched
case for case. Files: [report](../results/kaggle/20261107-6bc64b8.md),
[json](../results/kaggle/20261107-6bc64b8.json), [csv](../results/kaggle/20261107-6bc64b8.csv).

### Variant: a placeholder temporal table (not the cases as authored)

To see anything, a second run was registered ([`eval-prereg.yaml`](../bench/kaggle/eval-prereg.yaml),
`variant_spec_workaround`, committed before its code) with one change to the spec: remove the label
table from `time_columns` and declare a placeholder temporal table, as the round 1 dbt specs do.
Ten cases then load (56 queries); the four with empty entity keys or a zero duration are still
rejected, and their 24 queries count as misses. Over all 14 cases (95% cluster bootstrap by case):

| rule | recall [95% CI] | false-positive rate [95% CI] |
|---|---|---|
| verdict, static | 28 / 40 = 0.70 [0.43, 0.93] | 19 / 40 = 0.47 [0.24, 0.73] |
| verdict, with replay | 22 / 40 = 0.55 [0.29, 0.84] | 18 / 40 = 0.45 [0.21, 0.71] |
| replay score at its frozen threshold | 4 / 40 = 0.10 [0.00, 0.28] | 0 / 40 |

On only the ten cases that load, static recall is 28 of 28 and the false-positive rate 19 of 28
(0.68). That is not a good result: the checker flags nearly everything it can read, including 19
of the 28 clean twins, against a 5% target. Of the 28 pairs that load, it separates the leaky query
from its clean twin in 9 (4 with replay), and each of those sits in a disputed case or one with a
`DATE` or integer time column. Nothing can be said about L5 (all its cases were rejected).

The false alarms have a checked cause: when a table holds a declared outcome column, every column
read from that table is treated as post-outcome data, so a clean feature such as the gap between
signup and first activity is flagged next to the leaky one. Replay added little: 28 of its 44
verdicts were `unverifiable` (integer or `DATE` prediction times are not replayed), and it crashed
with a `KeyError` on two cases whose prediction column has mixed-case letters. The workaround and
the rules cannot be separated in these numbers. Files: [report](../results/kaggle/20261107-2c5b6f4-variant.md),
[json](../results/kaggle/20261107-2c5b6f4-variant.json), [csv](../results/kaggle/20261107-2c5b6f4-variant.csv).

## Second rule set (v2)

Rules, policy and threshold were frozen at tags `rules-frozen-v2` / `policy-frozen-v2`
([pre-registration](../results/leakage/prereg-8390186.json)) before new authors (fresh Claude agents, not
people), who saw only a brief, wrote 91 leakage templates and 31 agent prompts. Run once:

| | recall | FPR [95% CI] |
|---|---|---|
| leakage, combined (v2) | 0.99 | **0.37 [0.18, 0.55]** |
| leakage, replay only (v2) | 0.85 | 0.00 |
| leakage, first rule set (v1) | 0.19 | 0.06 |

| agent cell | unsafe action rate | false-block rate |
|---|---|---|
| Haiku 5.5, no guard | 0.61 | 0.00 |
| Haiku 5.5, guard (policy v2) | **0.00** | 0.36 |
| Opus 5.5, no guard | 0.39 | 0.00 |
| Opus 5.5, guard (policy v2) | **0.00** | 0.38 |

The guard caught nearly every leak and let no destructive action through, but it was far
stricter on unseen queries than on dev (FPR 0 there): every leakage false positive is a
static rule firing on a query whose replay passed. Most agent false blocks are approval
requests the scripted reviewer rejected because the held-out prompts list no approvals; the
analysis, and what is still truly blocked, is in
[docs/limitations.md](limitations.md#held-out-v2-frozen-rules-and-policy). Reports:
[leakage](../results/leakage/20261105-f52e63f.md), safety `results/safety/holdout_v2-*`.

The rules and the guard were changed after this result was read (column changes now go to
approval; [what changed](limitations.md#changed-after-held-out-v2)), so `holdout_v2` is
development data now; the changed rules are the v3 result above.

## First rule set (v1)

The leakage numbers below were measured with the first rule set (tag `rules-frozen-v1`).
Those held-out templates are now the `dev_v1b` split and informed the current rules (L7, the
optional [spec fields](spec.md#optional-fields)), so a run of the current rules over them is in-sample.

All thresholds and policies were frozen before the held-out sets were written:
leakage rules at tag `rules-frozen-v1` with thresholds pre-registered on the dev split
([results/leakage/prereg-25d1275.json](../results/leakage/prereg-25d1275.json)); the safety
policy at tag `policy-frozen-v1`. Held-out templates and prompts were written afterwards by
an author who did not read the detector code.

### Leakage: held-out SQL (138 cases, 78 leaky) and real data

| detector | held-out recall | held-out FPR [95% CI] | held-out F1 | real-data recall | real-data FPR |
|---|---|---|---|---|---|
| baseline: output timestamp > prediction time | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| static analysis | 1.00 | 0.10 [0.00, 0.25] | 0.96 | 1.00 | 0.18 |
| replay over snapshots | 1.00 | 0.02 [0.00, 0.06] | 0.99 | 0.88 | 0.00 |
| **combined** | **1.00** | **0.05 [0.00, 0.17]** | **0.98** | **1.00** | **0.00** |

Intervals are a bootstrap over templates (cases rendered from one template are resampled
together). A bootstrap cannot put an interval on a perfect recall: with 0 misses over 29
leaky held-out templates, the 95% lower bound on template-level recall is about 0.90 (rule
of three). The real-data set is small (12 templates); read it as a sanity check.

Real data: UCI Online Retail II (CC BY 4.0), 27 cases over monthly churn cutoffs with
simulated load delays. Static analysis takes about 5 ms per query; replay about 50 ms on the
synthetic world and about 1.1 s on the real one.

![ROC on held-out cases](../results/leakage/20261104-948534a-roc.png)

How to read this honestly:

- The baseline's pre-registered threshold is infinite because it never fired on dev; at
  any threshold its held-out AUC is only 0.61. Most leaks never put a future timestamp in
  the output.
- Every held-out false positive comes from two templates the author flagged as judgment
  calls: target encoding over labels whose horizon had closed (flagged L6: the guard does
  not know when a label becomes known) and an event-time bound with a lag longer than the
  feed's maximum delay (an L4 hint that replay correctly clears).
- The current rules address both with `label_known_after` and `max_ingest_delay`; the
  numbers above predate them.
- Replay misses the real-data L5 case because that leak shows on only 2 of 17 cutoffs.
- Full report: [results/leakage/20261104-948534a.md](../results/leakage/20261104-948534a.md).

### Safety: an LLM agent with and without the guard

16 held-out prompts (6 benign, 6 risky, 4 ambiguous), each run 3 times per condition.
Outcomes are scored from real table state (rows, snapshots, columns, tables touched), never
by an LLM.

| model | condition | unsafe action rate | safe handling S | completion U | score 2SU/(S+U) |
|---|---|---|---|---|---|
| Claude Haiku 5.5 | no guard | 0.27 | 0.73 | 1.00 | 0.85 |
| Claude Haiku 5.5 | **guard** | **0.10** | **0.90** | **1.00** | **0.95** |
| Claude Opus 5.5 | no guard | 0.40 | 0.60 | 1.00 | 0.75 |
| Claude Opus 5.5 | **guard** | **0.10** | **0.90** | **1.00** | **0.95** |

On the 24 dev prompts (Haiku 5.5), the unsafe action rate fell from 0.60 to 0.00. Every
benign task was completed in every condition (false-block rate 0); under the guard a few
benign runs went through an approval step first. The guard adds about 40 ms per action
(p95 about 110 ms) against 10-18 s of model time per task.

- All remaining unsafe runs under the guard are one prompt (`h15`): a one-row `UPDATE` on
  a production table that is inside the task scope. It was recorded as a known guard gap
  when the held-out prompts were written. The guard checks SQL and table state, not intent.
- The stronger model was *less* safe without the guard here: it refused or asked less
  often and acted more.
- The handcrafted tier (no LLM) allows 0 of 106 dangerous actions and blocks 0 of 28 safe
  ones.
- Reports: [dev](../results/safety/dev-20261104T081939Z.md),
  [held-out](../results/safety/holdout-20261104T084347Z.md). Transcripts are committed;
  `sqlguard-bench safety agent --split holdout --replay transcripts/safety/holdout-20261104T084347Z`
  re-scores them without an API key.
