---
name: receipts-discipline
description: How claims become record in this house. Use whenever recording a finding, publishing an artifact, or citing prior work. Receipts beat claims, including your own.
---

# Receipts discipline

A claim is worth what its receipt is worth. The record is built so that a stranger,
in any year, can check any load-bearing statement without trusting its author.

## The rules

1. **Receipts beat claims** — including claims by the seat that wrote the receipt,
   and claims in the spec. When a summary and a primary record disagree, the record wins.
2. **Hash what must not drift.** A published artifact carries its sha256. The hash
   is not added to the thing; it is a fact about the thing — determined the instant
   the bytes are fixed, whether or not anyone computes it. Pin the bytes first:
   one apostrophe, different fingerprint.
3. **Round-trip after publishing.** Re-fetch what the public surface actually
   serves and re-hash it. What you pushed and what strangers receive must be
   byte-identical, proven, not assumed.
4. **Hash-frozen artifacts never get edited.** Errata are recorded alongside,
   never written into the frozen bytes.
5. **A hash is a weight; an identifier is an address.** A hash has no inside and
   can only answer same-or-different. An id exists to be cited and superseded.
   Never use one to do the other's job.
6. **Cite the source you actually checked**, at the version you checked it, and
   say plainly when a claim rests on the house's own word rather than an
   independently checkable surface.

## The tell

If a statement matters and you cannot point to its receipt, you do not have a
finding yet. You have a candidate.
