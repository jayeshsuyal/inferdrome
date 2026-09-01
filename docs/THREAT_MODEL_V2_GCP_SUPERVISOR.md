# GCP supervisor threat supplement v0.2

This is an additive development-cycle supplement to the frozen v0.1 threat
model. It neither rewrites historical claims nor enables a live Compute Engine
path.

## Assets and trust boundaries

The protected local artifacts are the immutable v1 plan/arm/request/lease, the
v2 approval, rate basis, cost/cleanup guard, supervisor event chain, watchdog
receipt, and optional exact local kill marker. Provider SDK construction,
credential discovery, network calls, resource mutation, SSH, GPU access, and
billing access remain outside this implementation and disabled by default.

## Principal threats and fail-closed responses

- A substituted plan, project, region, zone, A100 profile, boot image, runner
  image, runtime image, request, quote, capacity input, cap, deadline, or
  operator approval is rejected by canonical digest-bound validation before a
  future SDK/ADC factory can be invoked.
- A missing, stale, malformed, duplicate-key, unknown-field, or expired
  approval/rate/guard artifact is rejected. Pricing and capacity inputs are
  deliberately read-only and cannot themselves authorize a create. The same
  expiring inputs are rechecked at the final create edge and before work.
- A watchdog that cannot produce an exact independently-durable receipt with
  controller-death and hung-work coverage prevents `CREATE_INTENT`. The
  receipt must remain valid through the quote-bound cleanup and exact-absence
  tail, not merely through provider runtime. The sidecar persists
  `ARM_CONSUMED` and `WATCHDOG_READY` before the future create boundary; no
  live watchdog is armed by this local-only implementation.
- Delayed setup, insert, reconciliation, or readback cannot silently consume
  an absolute execution deadline. Create requires the complete immutable
  provider runtime and watchdog cleanup horizon still to remain; work repeats
  absolute controller/watchdog deadline checks and fails into cleanup instead
  of starting after expiry.
- Journal replacement, symlink traversal, partial writes, corruption,
  noncanonical content, timestamp regression, or state-transition regression
  is rejected. The sidecar uses exact controller file names, no-follow opens,
  locking, fsync, a hash chain, and bounded event/file sizes. Any unresolved
  sidecar lease blocks every later reservation until authoritative cleanup.
- A kill marker that is unsafe, malformed, modified while read, or bound to a
  different approval/request/controller fails closed. An active exact marker
  is durably recorded as `KILLED` before cleanup proceeds.
- Incomplete pagination, invalid owned-resource inventory, ambiguous create or
  delete, label mismatch, a residual boot disk, or failed absence confirmation
  cannot become cleanup success. Discovery is read-only, bounded, and exact
  label/project/region/zone scoped; termination only targets the exact request
  identity. No hostname ownership inference or account-wide resource diff is
  used.
- Controller restart can resume exact cleanup only. It never resumes work,
  replays an unknown create, substitutes a resource identity, or converts an
  unconfirmed cleanup state into absence.

## Boundaries and remaining limitations

Provider labels cannot supply an atomic server-side deletion precondition, so
the concrete provider path retains a documented ownership TOCTOU residual that
must fail closed if the exact observed state changes. The local watchdog receipt
is an activation prerequisite, not proof that a real future watchdog or
provider TTL has run.

The frozen v1 arm equates controller duration with provider runtime and has no
separate setup margin. The v0.2 layer intentionally treats any elapsed setup
as a create-time safety failure rather than assuming extra time. This leaves
the path non-activatable by design until a future separately reviewed arm
version introduces an explicit margin while retaining the cleanup watchdog
tail.

The rate basis and hard ceiling are exact local arithmetic over a supplied
estimate. They are not provider/account billing enforcement and do not prove
an invoice. Compute Engine maximum runtime and delete scheduling are cleanup
backstops, not invoice controls. No provider execution, evidence collection,
receipt issuance, artifact publication, or external claim is produced here.
