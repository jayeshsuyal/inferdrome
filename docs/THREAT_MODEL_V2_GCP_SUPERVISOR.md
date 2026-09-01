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
  image, runtime image, request, quote, capacity input, cap, deadline,
  operator approval, startup projection, or opaque execution-payload digest is
  rejected by canonical digest-bound validation before a future SDK/ADC factory
  can be invoked. The execution-payload binding contains a digest only; it
  neither interprets bytes nor grants shell, transfer, credential, evidence,
  or execution authority.
- A missing, stale, malformed, duplicate-key, unknown-field, or expired
  approval/rate/guard artifact is rejected. Pricing and capacity inputs are
  deliberately read-only and cannot themselves authorize a create. The same
  expiring inputs are rechecked at the final activation/create edge. After a
  capability is activated, post-create work is bounded by its provider-runtime
  and watchdog-cleanup horizons rather than by the frozen v1 arm timestamp.
- A watchdog that cannot produce an exact independently-durable receipt with
  controller-death and hung-work coverage prevents `CREATE_INTENT`. The
  receipt must remain valid through the v2 cleanup and exact-absence tail, not
  merely through provider runtime. The sidecar persists `ARM_CONSUMED` and
  `WATCHDOG_READY` before the future create boundary; no live watchdog is
  armed by this local-only implementation.
- Delayed setup cannot silently consume runtime: v2 setup and authorization
  horizons are checked only at activation, then the actual activation time
  starts the explicit provider-runtime clock and independently durable
  watchdog-cleanup clock. An overlong or substituted horizon is rejected.
- Journal replacement, symlink traversal, partial writes, corruption,
  noncanonical content, timestamp regression, or state-transition regression
  is rejected. The sidecar uses exact controller file names, no-follow opens,
  locking, fsync, a hash chain, and bounded event/file sizes. Initial partial
  prefixes, no-provider crashes, and core/sidecar terminal mismatches have
  explicit local reconciliation; remaining unresolved state blocks later
  reservation until authoritative cleanup.
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
- The file watchdog persists the full exact disk binding before cleanup intent,
  uses a lease before retrying a dead worker, kills a bounded worker process
  group on timeout, and stores authoritative instance-inventory plus named-disk
  absence facts before terminal confirmation. A terminal core journal durably
  fences a still-active watchdog intent, prevents a restart from initializing a
  cleanup supplier, and settles it locally after its bounded lease.

## Boundaries and remaining limitations

Provider labels cannot supply an atomic server-side deletion precondition, so
the concrete provider path retains a documented ownership TOCTOU residual that
must fail closed if the exact observed state changes. The local watchdog receipt
is an activation prerequisite, not proof that a real future watchdog or
provider TTL has run.

The frozen v1 arm equates controller duration with provider runtime and remains
unchanged historical evidence. The additive v2 contract supplies a separate
setup margin, authorization horizon, provider-runtime horizon, and watchdog
cleanup horizon; it does not reinterpret the frozen arm or keep a full future
runtime under that historical deadline. The default provider gate remains
disabled pending separate review and explicit operator approval.

The rate basis and hard ceiling are exact local arithmetic over a supplied
estimate. They are not provider/account billing enforcement and do not prove
an invoice. Compute Engine maximum runtime and delete scheduling are cleanup
backstops, not invoice controls. No provider execution, evidence collection,
receipt issuance, artifact publication, or external claim is produced here.
