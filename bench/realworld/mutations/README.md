# Mutated fraud-project models (deliberately leaky)

Each file is a model of [SarrangTech/fraud-detection-feature-store](https://github.com/SarrangTech/fraud-detection-feature-store)
(`dbt/` subdirectory, commit `f7d8a259af379a3c39bacd36235e53fb65eb271c`, MIT license, copyright (c)
2026 B. Sarang; the license text is in [LICENSE](LICENSE)) after one mutation operator of
[../mutation-operators.yaml](../mutation-operators.yaml) was applied. The operators were registered
before these files were generated. Every file starts with a header naming the original file, the
commit and the operator.

`<operator>/<model>.sql` replaces `<model>` in a copy of the project; `python -m bench.realworld.mutate`
regenerates the files, checks each mutant and the unmutated control, and writes
`results/realworld/mutations*.json|csv`.

These files leak on purpose and must not be used as feature code.
