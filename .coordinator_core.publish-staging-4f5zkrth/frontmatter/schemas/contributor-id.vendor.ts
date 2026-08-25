// opticon-source-path: src/lib/identity/contributor-id.ts
// opticon-source-pin: e3d5726bd021b5ffe97b4148ad93ceba9ec95b8d (2026-08-19, C1 transcription pin)
// Vendored-not-compiled: this file is a drift-watch comparison target for
// coordinator_core.frontmatter.schema_drift_watch, never imported or built. The
// header comments above are the machine-readable pointer scan_vendored_source_drift()
// reads to resolve the opticon-repo-relative path this file mirrors — see that
// function's docstring for the coverage-by-construction contract.
/**
 * Mints the opaque, short contributor id the pseudonymous-identity plan requires
 * (`docs/plans/2026-08-15-pseudonymous-contributor-identity.md` § C3).
 *
 * The id is keyed strictly on `User.databaseId` — GitHub's numeric, immutable user id — never
 * on `login` (the handle) or any display name. `databaseId` survives a handle rename; a login
 * does not. Minting over the mutable string would reproduce the exact instability this plan
 * exists to remove (see the plan's Problem section and Anti-scope).
 *
 * This id **is** the `contributor_slug` value (`^[a-z0-9][a-z0-9-]*$`,
 * `docs/wiki/ownership-axes-contract.md:42`) for GitHub-sourced humans — the same ratified slot,
 * not a parallel namespace. See the plan's "Forced articulation" § Convergence note.
 *
 * Collision handling is explicitly NOT this module's job. Truncating a SHA-256 digest to 9
 * base36 characters makes a collision between two distinct `databaseId`s possible, and this
 * function has no way to detect one — it is a pure, stateless function of its input. Chunk C4
 * puts a UNIQUE constraint on the opaque-id column in the join table, so a colliding insert
 * fails loudly instead of silently merging two people's history. Do not "fix" that here by
 * making this function collision-tolerant (e.g. widening the id, retrying, or consulting prior
 * state) — that responsibility belongs to C4's schema, not to minting.
 *
 * Negative spec — both forbidden by the plan, not merely undesirable:
 *   - Do NOT add a `login`/handle parameter to the minting function. Determinism must hold
 *     across a handle rename, which is only possible if the login is never consulted.
 *   - Do NOT derive the id from anything other than `databaseId` — not a display name, not an
 *     email, not a login. Any of those would mint an identity for a string anyone can set.
 */
import { createHash } from "node:crypto";

/** Length of a minted contributor id, matching the PM's `ncc1701d` example. */
export const CONTRIBUTOR_ID_LENGTH = 9;

/**
 * Mints a deterministic, opaque contributor id for a GitHub `User.databaseId`.
 *
 * Absent input (`null`/`undefined`), non-finite or non-integer input, and non-positive input
 * (`0` or negative) all return `null` — an explicit absent value, never `""`. This is the
 * required response for a commit whose `GitActor.user` is null, or a PR authored by a non-`User`
 * `Actor` (Bot, Organization, Mannequin): no linkable GitHub account means no id (plan AC4). A
 * non-positive `databaseId` can never be a genuine GitHub id, so it is treated the same as absent
 * input rather than minted into a valid-looking id.
 *
 * Derivation: SHA-256 the decimal string form of `databaseId`, take the first 12 hex characters
 * of the digest, parse those as a base16 integer, and reduce it modulo `36^9` so every input maps
 * uniformly onto the full 9-digit base36 space (`36^9 ≈ 2^46.5` — the id space's actual entropy;
 * reducing a wider digest into it by modulo is standard practice and does not itself provide
 * collision resistance). Base36 digits are already within the ratified `contributor_slug`
 * alphabet (`0-9a-z`), so no character substitution is needed. The result is left-padded with
 * `"0"` to exactly `CONTRIBUTOR_ID_LENGTH` characters — the modulo reduction guarantees the
 * unpadded rendering is never longer than 9 characters, so no truncation is needed or performed.
 * Collision *detection*, not avoidance, is C4's job (see above), not this function's.
 *
 * The alphabet's leading-character rule (`^[a-z0-9][a-z0-9-]*$`) is satisfied by construction:
 * base36 digits are only ever `0-9a-z`, and this function never emits `-`, so a leading `-` is
 * impossible regardless of the input.
 */
export function mintContributorId(databaseId: number | null | undefined): string | null {
  if (databaseId === null || databaseId === undefined) {
    return null;
  }
  if (!Number.isFinite(databaseId) || !Number.isInteger(databaseId) || databaseId <= 0) {
    return null;
  }

  // Review: code-reviewer — modulo reduction into the full 9-digit base36 space keeps the
  // mapping uniform across all inputs, replacing the prior high-order slice that quotiented
  // 10-char renderings by 36 (P2).
  const digest = createHash("sha256").update(String(databaseId)).digest("hex");
  const truncatedHex = digest.slice(0, 12);
  const idSpace = 36n ** BigInt(CONTRIBUTOR_ID_LENGTH);
  const base36 = (BigInt(`0x${truncatedHex}`) % idSpace).toString(36);

  return base36.padStart(CONTRIBUTOR_ID_LENGTH, "0");
}
