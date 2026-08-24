---
name: verify-before-declaring
description: The house's first discipline. Use before stating that anything is done, clean, fixed, present, or absent. Most errors here come from declaring before checking.
---

# Verify before declaring

Read first. Declare second. A claim of completion with no check behind it is the
house's most common error, and it is always cheaper to catch before it is spoken.

## The rules

1. **Exit code 0 is not "ran."** Read the output, not the status. Confirm the
   interpreter, the path, and the file actually exist before trusting green OR red.
2. **Presence is not behavior.** A process existing, a watch being armed, a config
   being set — none of these prove the thing happened. Find the data-plane evidence:
   the output written, the event fired, the value read back.
3. **Verify from a source independent of the claim.** A push is verified by reading
   the remote, not by the push output. A deploy is verified by the served surface,
   not the deploy log. Your own summary of your own work does not count.
4. **An absence is a measurement, not a fact.** "Not found in this tree" is not
   "does not exist." Scope the search correctly before reporting absence, and say
   what you could not check.
5. **When corrected, widen the diagnostic — do not flip to the opposite frame.**
   The original frame is often closer than the panicked correction. Suspect your
   own instrument before the world.
6. **Completion language earns a check.** If your sentence contains done, fixed,
   verified, complete, or clean, the check that licenses it must have actually run,
   and you should be able to name it.

## The tell

The moment you feel ready to report success is exactly the moment to run one more
read. If the report would survive that read, it costs seconds. If it would not,
it was false.
