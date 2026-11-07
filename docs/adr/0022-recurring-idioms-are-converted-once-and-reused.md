---
status: accepted
---

# Recurring idioms are converted once and reused, with every artefact still fully tested

Legacy estates are written in house patterns. The same sub-structure recurs across hundreds of artefacts: an audit-insert block, an error-logging `CATCH`, a paging query. Converting each occurrence afresh costs time and model spend, and gives different code for the same pattern. We decided that the tool deterministically finds **idioms**: sub-patterns of the intermediate representation that recur across an engagement, with identifiers and literals as placeholders, matched by exact structure only, never by similarity.

- **Convert once.** Each idiom is converted once. Its conversion is reused in every artefact that contains it, instantiated with that artefact's names and literals.
- **Verified before reuse.** An idiom's conversion is reused only once an artefact containing it has reached "Reconciled" and passed final validation.
- **Every artefact still fully tested.** Reuse saves conversion work, never verification.
- **The pilot learns house patterns first.** The most frequent idioms are included in the pilot's set cover.
- **Recorded and retired.** Each artefact records which idioms it used. An idiom whose conversion is found wrong is retired, and every artefact that used it is re-checked.
- **Client idioms stay client-scoped.** They live in the engagement's control database and never leave it.

We chose this because a pattern verified once and reused is both cheaper and more consistent than many independent conversions. Because every artefact is still tested in full, reuse can never let a wrong conversion through unchecked.

The full decision is in [Region-level idiom reuse](https://github.com/Bach-To-The-Future/data-migration/issues/80), a build ticket under [the build's starting issue](https://github.com/Bach-To-The-Future/data-migration/issues/35). Its effect is measured in the benchmark's development runs.

## Considered options

- **Whole-artefact clusters only**, as already decided. Kept, but they catch only artefacts identical apart from names. Idioms catch the shared parts of artefacts that differ elsewhere.
- **Similarity-based reuse** (embeddings). Rejected: a near match can differ in exactly the detail that matters, and the tool never decides what applies where by similarity.
- **Idioms as lessons.** Rejected: a lesson corrects a mistake; an idiom reuses a success. They share the exact-match machinery and the retirement path, but not the confirmation gate.

## Consequences

- **A conversion step can come from an idiom** instead of from lowering assist, raising or generation. It is recorded as such, and the report shows idiom use per artefact.
- **An idiom whose instance fails reconciliation** in some artefact is not retired automatically. The artefact fails as usual, and repair works on its region. The idiom is retired only when its conversion itself is found wrong, as worked examples are.
- **In measurement runs,** idioms drawn from release hold-out artefacts are discarded, as lessons are.
- **The benchmark's development runs measure the effect,** comparing speed and converted share with and without idiom reuse.

## Amendment, 7 November 2026: after the architecture review

**Deferred from v1** (finding A5). Reuse saves model calls only for AI-produced regions, while the house patterns it targets are mostly converted by rules, and it would skip the candidate cross-check. Its trigger: benchmark logs showing many repeated AI-produced regions. The design stays as recorded above.

Decided by the owner on [the architecture review](https://github.com/Bach-To-The-Future/data-migration/issues/35#issuecomment-6707883287).
