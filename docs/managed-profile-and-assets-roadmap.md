# Managed Profiles, Hotkeys, and Graphics Assets

## Implementation status

The safe implementation baseline is now present: global hotkey ownership modes,
native import/refresh hashes, structured profile controls, foreground executable
path matching, a content-addressed asset store, cached official release providers,
manual non-executing imports, x64 PE checks, safe ZIP quarantine, LS-root-only
graphics resolution, and manifest-owned transactional deployment with rollback.
LosslessProxy and LSP-NeuralRender are resolved as a complete chain, including an
immutable snapshot of the original `Lossless.dll`, the user-supplied DLSSNR
runtime, and a merge-preserving `addons/config.json` overlay.

The experimental ReShade/Feeder and ReShade/Special K paths deliberately require
a package-contained `lossless-scaling-deployment.json` compatibility recipe. No
upstream game installer is run and no destinations are inferred. Hardware/runtime
compatibility probes, signed recipe publication, health-log verification, and a
fully guided row-based proxy-chain editor remain validation/release work rather
than being represented as proven support.

## Decision summary

The companion should be the control plane for profile selection, scaling activation,
and files deployed exclusively into Lossless Scaling. Lossless Scaling remains the
renderer and hotkey receiver, but its native Auto Scale stays disabled. The
companion detects the foreground target without modifying it, selects a helper
profile, makes the Lossless Scaling directory state transactional, starts or
restarts Lossless Scaling when binaries changed, and then emits the synchronized
scaling hotkey.

The game/application boundary is absolute: target executables and domains are
detection inputs only. The helper must never copy, link, inject, configure, or
remove anything in a target application's directory or process.

The implementation should not reuse the two discarded remote commits directly.
They contain useful product ideas, but they mix unverified downloads, arbitrary
elevated filesystem operations, destructive cleanup, checked-in third-party
binaries, and automatic two-way profile merging.

## Current foundation

The repository already has:

- Foreground-process matching and browser fullscreen events.
- Helper-owned focus, blur, fullscreen, and manual scaling triggers.
- Native Lossless Scaling Auto Scale suppression.
- A profile creator/editor with running-process discovery.
- A generic editor for existing Lossless Scaling XML profiles.
- Per-profile Lossless Scaling ReShade preset-path switching and reload hotkeys.
- Copied LS-owned DLL overrides with backup and restoration; legacy `symlink`
  values normalize to copy mode.
- A `lossless_scaling` deployment target that stops, updates, and restarts Lossless
  Scaling around binary changes.
- Local WebSocket origin restrictions, validated profile identifiers, constrained
  DLL filenames, and an administrator manifest for the compiled application.

The main gaps are hotkey authority, safe native-profile import/update semantics,
friendly structured scaling controls, managed graphics assets, verified downloads,
and transaction/ownership manifests for multi-file deployments.

## Target runtime flow

```text
foreground/browser event
        |
        v
resolve helper profile + native LS profile link
        |
        v
calculate desired deployment manifest
        |
        +-- no binary/config delta --> keep LS running
        |
        `-- delta --> stop LS -> transactional file update -> restart LS
                                                |
                                                v
                               apply live ReShade preset reload
                                                |
                                                v
                               send synchronized LS hotkey if requested
```

Only a change to files loaded at process initialization should cause a restart.
Changing a ReShade preset path can remain a live operation. Switching to a profile
whose resolved deployment manifest is identical should not restart anything.

## 1. Hotkey synchronization

### Authority

Use the helper configuration as the source of truth and write the same global
activation hotkey into Lossless Scaling's `Settings.xml`. Lossless Scaling exposes
one global activation hotkey, so profile-specific scaling hotkeys are misleading
unless separate LS instances are introduced later.

Recommended model:

```json
{
  "hotkey_sync_mode": "helper_controls_lossless",
  "global_hotkey": {
    "modifiers": ["ctrl", "alt"],
    "key": "s",
    "hold_delay_ms": 50,
    "activation_delay_ms": 350
  }
}
```

Supported modes should be:

- `helper_controls_lossless` (default): update the native hotkey while LS is
  stopped and use it for every scaling activation.
- `follow_lossless`: import the native hotkey into the helper at startup.
- `warn_only`: report a mismatch without changing either side.

### Behavior

1. Canonicalize modifier names and order (`ctrl`, `alt`, `shift`, `win`).
2. Parse `Hotkey` and `HotkeyModifierKeys` from `Settings.xml`.
3. Compare canonical values before writing.
4. Coalesce native Auto Scale suppression, hotkey updates, and LS-owned DLL changes
   into one stop/write/deploy/restart transaction.
5. Make profile hotkeys inherit the global hotkey in the UI. Retain legacy profile
   values during migration, but show a conflict warning and offer one-click
   normalization.
6. Verify that `SendInput` succeeded before updating helper scaling state.

### Tests

- Modifier parsing and serialization are round-trippable.
- Ordering/casing differences do not produce a false mismatch.
- A mismatch causes exactly one restart when LS is running.
- Auto Scale, hotkey, and DLL changes are coalesced into one restart.
- `warn_only` never writes.
- Unsupported keys block Save with a useful validation error.

## 2. Native profile import and update

### Import direction

Avoid automatic bidirectional synchronization. Treat native Lossless Scaling
profiles as an import/link source and helper profiles as the automation source.
Imports should always show a preview and must never delete either side implicitly.

Since native profiles do not appear to expose a stable identifier, derive a key
from normalized `Title` plus normalized `Path`, and store it explicitly:

```json
{
  "lossless_profile": {
    "title": "Cyberpunk 2077",
    "path": "D:\\Games\\Cyberpunk2077.exe",
    "last_imported_hash": "sha256:..."
  }
}
```

### Import workflow

1. Read all native profiles without modifying them.
2. Present `Create helper profile`, `Link to existing`, and `Ignore` per entry.
3. Match suggestions by exact normalized executable path first, exact executable
   filename second, and title only as a low-confidence suggestion.
4. Import known scaling fields into a typed structure while preserving all unknown
   XML fields in an `advanced_settings` map.
5. Preserve helper-only data such as domains, focus behavior, asset selections,
   notes, and deployment targets during refresh.
6. Use `last_imported_hash` to detect concurrent/native edits and show a three-way
   diff instead of silently overwriting.
7. Never propagate deletions automatically. Provide explicit `Delete helper only`
   and `Delete helper and native profile` actions with separate confirmation.

### Native update rules

- Lossless Scaling must be stopped before native XML writes.
- Writes remain atomic and retain a first-original backup.
- Unknown elements remain untouched.
- `AutoScale` is always forced to `false` while helper ownership is enabled.
- Multiple changes are committed in one XML write and one LS restart.

### Verification spike

Before implementing native profile activation, test whether Lossless Scaling still
selects the matching `Path` profile for a manual hotkey when that profile's
`AutoScale` is false. If it does, linking by native profile is sufficient. If it
does not, the companion needs an explicit, documented way to select/copy the
desired native settings before launch. Do not assume this behavior from XML alone.

## 3. Profile creator/editor and scaling controls

The current dashboard already creates and edits profiles, so extend it rather than
replace it. Convert the long form and raw DLL JSON into a guided editor:

### Wizard sections

1. **Target**
   - Select a running window or browse for an executable.
   - Show executable name, resolved path, architecture, and current profile match.
   - Optional browser domain with hostname-boundary validation.
2. **Activation**
   - Scale on focus, stop on blur, scale on browser fullscreen, and stop on exit.
   - Explain that LS native Auto Scale is disabled and the helper owns these rules.
3. **Lossless Scaling**
   - Link/import a native profile.
   - Friendly controls for commonly used scaling type, frame generation,
     multiplier, capture API, HDR, FPS display, cursor, latency, and fit mode.
   - An Advanced JSON/XML field editor for unknown/future settings.
4. **Lossless Scaling graphics stack**
   - LosslessProxy, LSP-NeuralRender, ReShade version/build, preset and shader
     bundle, Special K, and any custom LS add-ons/DLLs.
   - Render the resolved proxy/chain plan and flag conflicts.
   - Every destination is resolved beneath the configured Lossless Scaling
     installation. The selected game's path is never offered as a destination.
5. **Review and apply**
   - Show files added/replaced/restored, processes restarted, settings changed,
     warnings, and the final activation hotkey.
   - Offer `Save`, `Save and apply`, and `Dry run`.

### Usability requirements

- Replace the DLL JSON textarea with repeatable rows and an Advanced JSON escape
  hatch.
- Validate source existence, target ownership, DLL filename, architecture, and
  duplicate proxy names before Save.
- Distinguish validation errors from compatibility warnings.
- Show active, pending, degraded, and restart-required status per profile.
- Never perform filesystem changes merely by opening/editing the modal.

## 4. Download and import architecture

### Provider interface

Implement providers behind one service contract:

```text
list_versions()
resolve_assets(version, architecture, game)
download_to_staging(asset)
verify(staged_asset, metadata)
extract_to_quarantine(staged_asset)
import_verified_asset(quarantined_asset)
```

No WebSocket request may supply an arbitrary URL. Each provider owns an allowlist
of HTTPS hosts, repositories, asset-name patterns, size limits, and content types.
Manual import remains available when an official machine-readable feed is absent.

### Shared download pipeline

1. Require an explicit user click and show source/version/license information.
2. Stream into a uniquely named `.partial` file with timeout and size limits.
3. Compute SHA-256 while streaming.
4. Compare against a maintained digest or the release asset's published digest.
5. Validate archive paths before extraction: reject absolute paths, drive paths,
   `..`, links, devices, and unexpected file types.
6. Extract into quarantine, never directly into an application directory.
7. Validate PE architecture and expected filenames; record Authenticode status when
   applicable.
8. Atomically move verified content into the immutable asset store.
9. Never execute a downloaded installer from the elevated companion.

GitHub release assets expose version, size, download URL, and may expose a SHA-256
`digest`, making the official Releases API suitable for providers hosted there.

### ReShade provider

- Obtain ReShade only from `reshade.me`, or let the user select an installer they
  downloaded from that site.
- Extract and deploy the selected build only into the configured Lossless Scaling
  directory. ReShade presets apply to that instance and therefore affect the final
  LS output rather than an individual game's render pipeline.
- Do not commit or redistribute ReShade binaries or shader packages. The official
  site specifically directs users to link to the site rather than share them.
- Offer Standard and Full Add-on builds distinctly. Full add-on support is unsigned
  and should display a prominent multiplayer/anti-cheat warning.
- Prefer user-assisted official download/import if a stable authenticated metadata
  endpoint is unavailable; do not scrape a fragile HTML link silently.
- Extraction may parse a verified installer payload but must not execute it.

### LosslessProxy and LSP-NeuralRender providers

- Use `FrankBarretta/LosslessProxy` as the LS-native add-on host. Its proxy replaces
  `Lossless.dll`, retains the original as `Lossless_original.dll`, and loads add-ons
  from the LS `addons` directory. This replacement always requires a full LS stop
  and restart.
- Resolve LSP-NeuralRender releases from the official
  `andreiday/LSP-NeuralRender` GitLab release API. Import the complete release
  package and pin `LSP_NeuralRender.dll`, `nvngx.dll_lspnr.dll`, and `addon.json`
  together.
- LSP-NeuralRender is the preferred DLSS 5 path. It reads captured frames and
  LSFG optical flow inside Lossless Scaling, processes them on the LSFG GPU, and
  composes the result into LS-presented real and generated frames. It requires no
  ReShade, RenoDX, DLSS5-Feeder, game files, game hooks, or game API detection.
- Treat `nvngx_dlssnr.dll` as a user-supplied runtime. NVIDIA has not publicly
  released the required snippet and LSP-NeuralRender intentionally neither ships
  nor links to it. The helper may validate and content-address a user import, but
  must not discover, download, redistribute, or patch unofficial copies.
- Surface the actual limitations: LSFG must be active, an NVIDIA RTX GPU must run
  LSFG, HDR frames are currently not processed, and this is neural
  post-processing—not DLSS Super Resolution/upscaling.
- Expose the add-on's useful per-profile controls through its LosslessProxy config:
  style, intensity, local/skin structure, local tone, working scale, apply
  strength, highlight protection, optical flow, and watchdog. Preserve unknown
  fields during updates.

### Experimental Lossless Scaling ReShade/Feeder recipe

Retain an optional DLSS5-Feeder recipe for users who specifically want that stack,
but deploy every component into Lossless Scaling—not the detected game:

- Install ReShade Full Add-on support beside `LosslessScaling.exe`, then deploy the
  pinned Feeder add-on/effect, motion-vector shader, neural consumer/runtime, and
  LS-specific ReShade configuration beneath the same canonical LS root.
- Use LosslessProxy plus LSP-ReShade only where needed to make the ReShade overlay
  controllable through the Lossless Scaling overlay.
- Resolve Feeder releases from `jlrouzies-fr/DLSS5-Feeder` and pin the complete
  package. Generic RenoDX management remains out of scope; any required neural
  consumer/runtime without a stable official release source is manual-import only.
- Label this recipe experimental and version-lock the whole chain. The official
  Feeder documentation describes application/game ReShade deployment rather than
  Lossless Scaling, while the LS deployment is currently community-demonstrated.
- Profiles may select either `lsp_neural_render` or `ls_reshade_feeder`, never both.
  Switching recipes is one transactional LS stop, cleanup, deployment, and restart.

### Special K provider

- Resolve releases from the official `SpecialKO/SpecialK` release feed.
- Import the published archive as a versioned package rather than checking a DLL
  into this repository.
- Do not assume Special K can coexist with ReShade by copying both as `dxgi.dll`.
  Compatibility and chain-loading configuration must be resolved explicitly.

### Update policy

Profiles should select an update policy independently for LosslessProxy,
LSP-NeuralRender, DLSS5-Feeder, ReShade, and Special K:

- `pinned`: never change the selected version automatically.
- `notify` (default): report a compatible update and show its release notes and
  deployment diff.
- `stage`: download and verify a compatible update into the immutable store, but
  wait for approval before changing the profile lock or application directory.

Do not support unattended apply initially. Checking for an update and activating
it are separate operations. A provider check should use cached metadata and ETags,
run at most once per configured interval, and retain the last known-good package
for rollback.

Each resolved component records provider, channel, release/tag, release asset ID,
source URL, published timestamp, SHA-256, architecture, and compatibility group.
The generated `graphics.lock.json` pins the complete working graph. Updating one
component re-runs the resolver against all pinned dependencies; `latest` is never
copied blindly over an active installation.

LosslessProxy, DLSS5-Feeder, and Special K can use their official GitHub release
feeds; LSP-NeuralRender can use its official GitLab release feed. ReShade updates
should use an official, authenticated metadata source if one becomes available;
otherwise the helper detects the version advertised on the official site and
performs a user-assisted download/import. The elevated companion never executes
installers.

Applying an approved update follows the normal transaction: stage side-by-side,
verify, stop the process that loads the affected files, deploy atomically, launch,
verify health/log evidence where possible, and retain a one-click rollback. Every
managed binary/configuration update targets and restarts Lossless Scaling only.

## 5. Asset directories

Do not store mutable assets beside the PyInstaller executable and do not commit
third-party binaries. Use an administrator-protected machine store:

```text
%ProgramData%\LosslessScalingHelper\
  assets\
    packages\<provider>\<version>\<sha256>\...
    imports\<sha256>\...
  profiles\
    <profile-id>\manifest.json
    <profile-id>\overlay\...
  deployments\
    <target-id>\active-manifest.json
  backups\
    <target-id>\<transaction-id>\...
  staging\
    <transaction-id>\...
  logs\
```

Keep user preferences and profile metadata under
`%LOCALAPPDATA%\LosslessScalingHelper`; keep machine-wide executable assets under
`%ProgramData%`. Apply ACLs so ordinary users can read deployed assets but cannot
replace code later loaded by an elevated process.

Packages are immutable and content-addressed. A profile overlay is a manifest that
references packages; it is not another uncontrolled copy of the package library.
Production deployment should copy verified files. Symlink mode remains an explicit
development-only option and must reject sources outside trusted roots.

### Manifest shape

```json
{
  "schema_version": 1,
  "profile_id": "cyberpunk-cinematic",
  "target": "lossless_scaling",
  "files": [
    {
      "relative_path": "dxgi.dll",
      "asset_sha256": "...",
      "source_package": "reshade/6.8.0-addon/...",
      "role": "primary_proxy"
    }
  ],
  "graphics_chain": {
    "primary_proxy": "reshade",
    "host": "lossless-proxy/<pinned-version>",
    "addons": ["lsp-neural-render/<pinned-version>"],
    "neural_runtime": "manual-import/<sha256>",
    "chainloaded": ["special-k"]
  }
}
```

## 6. Graphics-stack resolution

Model roles instead of a flat list of files:

- `primary_proxy`: owns `dxgi.dll`, `d3d11.dll`, `dinput8.dll`, etc.
- `chainloaded_proxy`: loaded by a configured primary proxy when supported.
- `lossless_proxy`: owns the `Lossless.dll` forwarding/backup recipe.
- `lossless_addon`: loaded by LosslessProxy from `addons\<addon>`.
- `reshade_addon`: `.addon64` loaded by the LS ReShade instance.
- `neural_runtime`, `shader_bundle`, `preset`, `config`, and `support_file`.

The resolver must reject two components claiming the same proxy filename unless a
tested chain-loading recipe exists. Every resolved destination must be beneath the
canonical configured LS directory. Special K/ReShade order must be a versioned
compatibility recipe rather than an ad-hoc INI file.

Profiles should store the user's intent; the resolver produces the concrete file
plan. This lets compatibility logic evolve without rewriting every profile.

## 7. Transactional deployment

For each profile activation:

1. Resolve and validate the desired manifest.
2. Compare its hash to the active deployment manifest.
3. If identical, skip filesystem work and restart.
4. Acquire a per-target deployment lock.
5. Stop the process that loads changed initialization-time files.
6. Revalidate every destination beneath the canonical Lossless Scaling root;
   reject junction/reparse escapes and any path derived from the detected game.
7. Stage files on the destination volume.
8. Back up only files not already owned by the helper.
9. Atomically replace files where Windows permits it.
10. Write the active manifest only after every operation succeeds.
11. Roll back the whole transaction on failure.
12. Restart the process and then perform live preset/hotkey actions.

Cleanup uses the active manifest. It removes only files whose current hashes still
match what the helper deployed; user-modified or unknown files are preserved and
reported. Failed cleanup remains tracked and retryable.

## 8. Elevated-service boundaries

The dashboard talks to an administrator process, so mutation endpoints need more
than path validation:

- Retain strict loopback binding and origin checks.
- Issue a random per-launch capability token to the companion-hosted dashboard.
- Native clients without an `Origin` must not automatically receive mutation
  authority.
- Separate read-only and mutating messages.
- Require an operation ID and confirmation for download, deploy, remove, native
  profile deletion, and restart.
- Resolve paths and enforce allowed roots server-side; never trust UI paths.
- Do not expose arbitrary URLs, commands, archive members, or destination names.
- Log the plan and result of every privileged transaction without logging secrets.

## 9. Delivery phases

### Phase 0: behavior verification

- Test native profile selection with `AutoScale=false` and manual hotkey activation.
- Establish supported proxy names and ReShade/Special K chain recipes.
- Prove the LosslessProxy + LSP-NeuralRender lifecycle, config, log-based health
  check, upgrade, and exact rollback against supported Lossless Scaling versions.
- Separately prove and version-lock the experimental LS ReShade/Feeder recipe;
  never reuse its upstream game-directory installation script.
- Add a regression test proving a malicious/legacy profile cannot redirect DLL,
  ReShade, preset, configuration, backup, or cleanup writes into a game directory.
- Decide whether asset scope is per-user or machine-wide; machine-wide is preferred
  for the elevated build.

### Phase 1: hotkey and native profile authority

- Add hotkey sync modes and mismatch UI.
- Add read-only import preview, stable links, hashes, and refresh conflict handling.
- Batch XML edits with existing Auto Scale enforcement.

### Phase 2: profile editor

- Add wizard sections and structured common LS controls.
- Replace raw DLL JSON with validated rows plus dry-run review.
- Preserve the advanced editor for forward compatibility.

### Phase 3: asset store and local import

- Implement directories, ACL setup, immutable package metadata, PE inspection,
  quarantine extraction, and manual imports.
- Implement manifests, target allowlists, transaction logs, rollback, and ownership
  cleanup before adding network downloads.

### Phase 4: verified providers

- Add LosslessProxy, DLSS5-Feeder, and Special K GitHub providers plus the
  LSP-NeuralRender GitLab provider using release metadata/digests, channels, ETag
  caching, and compatibility locks.
- Add ReShade official download handoff/import, and automate only if a stable,
  policy-compliant official source can be verified.
- Add update notification, staged download, release notes, approval, rollback,
  progress, cancellation, retry, and offline-cache behavior.

### Phase 5: graphics resolver and activation integration

- Add typed graphics roles and compatibility recipes.
- Integrate manifest comparison with profile activation and the existing LS restart
  lifecycle.
- Coalesce native settings, DLLs, and hotkey changes into one restart.

## Acceptance criteria

- Profile import is idempotent and never deletes data implicitly.
- Helper/native hotkeys cannot silently diverge.
- Unknown LS XML fields survive every update.
- Native Auto Scale remains disabled under helper ownership.
- A profile with an unchanged deployment manifest causes no restart.
- A changed LS-owned binary set causes exactly one stop/restart.
- Failed deployment restores the exact prior state and remains retryable.
- Cleanup never deletes unowned or user-modified files.
- Downloads cannot target arbitrary URLs or directories.
- Wrong hashes, wrong architecture, unsafe archives, and proxy conflicts fail closed.
- ReShade Full Add-on/multiplayer risk requires explicit acknowledgement.
- No profile or request can deploy, inject, configure, back up, or remove anything
  in a detected game/application directory or process.
- LSP-NeuralRender release components remain pinned and deployed together.
- The experimental ReShade/Feeder recipe is LS-root-only and cannot coexist with
  LSP-NeuralRender in one resolved profile.
- The user-supplied DLSSNR runtime is never downloaded, patched, or redistributed.
- Updating one graphics component revalidates the entire pinned graphics lock.
- No update replaces active files until the user approves the deployment diff.
- Third-party binaries, downloads, user settings, staging, and backups remain out of
  Git.

## References

- [ReShade official site and download guidance](https://reshade.me/)
- [LosslessProxy official repository](https://github.com/FrankBarretta/LosslessProxy)
- [LSP-NeuralRender official repository](https://gitlab.com/andreiday/LSP-NeuralRender)
- [LSP-NeuralRender user guide](https://gitlab.com/andreiday/LSP-NeuralRender/-/blob/main/docs/user-guide.md)
- [DLSS5-Feeder official repository](https://github.com/jlrouzies-fr/DLSS5-Feeder)
- [LSP-ReShade official repository](https://github.com/FrankBarretta/LSP-ReShade)
- [Special K official releases](https://github.com/SpecialKO/SpecialK/releases)
- [GitHub release API](https://docs.github.com/en/rest/releases/releases)
- [GitHub release asset metadata and digests](https://docs.github.com/en/rest/releases/assets)
