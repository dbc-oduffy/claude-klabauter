

import { createHash } from "node:crypto";


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

  
  const digest = createHash("sha256").update(String(databaseId)).digest("hex");
  const truncatedHex = digest.slice(0, 12);
  const idSpace = 36n ** BigInt(CONTRIBUTOR_ID_LENGTH);
  const base36 = (BigInt(`0x${truncatedHex}`) % idSpace).toString(36);

  return base36.padStart(CONTRIBUTOR_ID_LENGTH, "0");
}
