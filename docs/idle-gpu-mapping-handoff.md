# Idle GPU mapping: local validation handoff

Requested outcome: identify Lossless Scaling's numeric GPU dropdown slots and
remap existing explicit profile choices without starting scaling. Check once at
Companion startup; then run a fresh, separate DXGI-only process about every three
hours and run the slower LS check only if its layout, physical topology, LS build,
or LS process changes. Both the fast and slow checks defer during scaling,
editing, tests, and updates. The standalone DXGI helper uses Windows idle process
priority. Deferred work retries every 30 seconds once idle.

## Probe failure evidence

The startup guard writes `ls-idle-adapter-report-<pid>.json` as well as the
ordinary runtime report. Runtime refreshes cannot replace the idle result.
Both discovery identity checks still require the launched PID, creation time,
and request nonce. `lsAdapterStatus` distinguishes an unloaded engine, missing
export, getter exception, and empty list; `lsAdapterError` records the native
error. Mapping status includes this evidence and the next retry interval.
Repeated failures back off from five minutes to three hours; an explicit
refresh resets that backoff. Saved mappings remain unchanged on failure.

Live diagnostics retain benchmark isolation but authorize starts only for their
temporary PID, HWND, and profile. ReShade and Special K checks require the actual
deployed module paths plus fresh runtime evidence. NR validation uses the
selected recipe and current host telemetry, including processed model frames.
The saved-map diagnostic reports consistency separately from live GPU proof.

## Machine and remaining uncertainty

The user's home machine has a 3090 as the Windows primary GPU, a 5050, Parsec's
virtual display driver, and Vibeshine attached to the 3090. LS/DXGI shows a varying
number of 3090 entries and usually one 5050. Virtual displays are a suspected
contributor, not a proven explanation. Preserve the raw duplicate list/order;
deduplicating it would destroy the slot numbering we need.

This workspace does not have that live installation available. Native fixture and
Python regression tests exercise the bridge and transactions, but do not prove
the installed LS build's live dropdown order, thread compatibility, startup
timing, or behavior with the user's virtual display drivers. Finish those checks
locally before describing hardware mapping as verified.

## Implementation to inspect

- `companion/services/gpu_index_refresh.py`: startup/three-hour scheduler,
  conservative identity-to-slot plan, idle process restart, native XML/config
  transaction, confirmation restart, rollback, shutdown cancellation.
- `native/adapter_report/src/probe.cpp`: standalone fresh DXGI snapshot; no
  rendering device and no LS engine loaded into the probe process.
- `native/startup_guard/adapter_probe.cpp`: bounded worker inside LS, launched by
  the existing in-context WPF startup guard. Waits for LS's own `Lossless.dll`;
  never loads or replaces it. Writes an atomic PID/token-tagged JSON report.
- `companion/services/adapter_report.py`: exact configured executable/PID lookup,
  report age/process-birth filtering; runtime report users now request that PID.
- `companion/services/ls_settings.py`: one atomic batch of explicit GPU-index
  changes; unknown XML, output-display selections, and Auto stay unchanged.
- `companion/services/server.py`: owned background task, queued dashboard
  requests during the transaction, worker cancellation/draining at shutdown.
- `LosslessCompanion.spec` and the Windows build workflow package the new
  `tools/LSC_AdapterProbe.exe` plus the updated startup guard.

## Inspected LS contract and compatibility gate

Offline inspection of a locally available **managed UI assembly**, version
**3.2.2.0**, found:

1. `UI.Core.GetAdapterNames` P/Invokes `Lossless.dll`, cdecl, returning an owned
   BSTR (managed return marshal type `BStr`, metadata native type `0x13`).
2. `UI.Core.GetAdapters` splits on `;`, removing empty entries.
3. `ProfilePage` inserts Auto at dropdown index zero. When the native list has
   more than one name, it appends those names in their returned order.
4. `PreferredGpu_SelectionChanged` saves `SelectedIndex` verbatim.

Thus an explicit slot is the returned name's zero-based position **plus one**.
Both raw DXGI arrays can disagree; neither alone is a substitute for this list.
Repeated names for one uniquely identified GPU model are aliases; a retained
valid slot is preferred, otherwise the first matching slot is chosen. Identical
physical GPU model names and unknown identities are left ambiguous.

Automatic native probing currently requires this managed UI SHA-256:

```text
e4ea2dbb1371ea1920d73c87202f19b6f095ef6f521a8a7a139d19f34f1fe866
```

An unrecognized build returns `needs_validation` without stopping LS or writing
its profile indexes. Do not simply add its hash to bypass the gate: inspect its
P/Invoke ABI and dropdown-to-XML indexing, then validate it with the live UI.
Do not run or redistribute DLLs from the workspace's ignored archive as a
substitute for the user's installed LS engine.

## Build and isolated tests

From the repository root in a Visual Studio x64 developer terminal:

```powershell
cmake -S native/adapter_report -B build/adapter_report -A x64
cmake --build build/adapter_report --config Release
ctest --test-dir build/adapter_report -C Release --output-on-failure
cmake -S native/startup_guard -B build/startup_guard -A x64
cmake --build build/startup_guard --config Release
$env:LSC_TEST_NATIVE_STARTUP_GUARD = '1'
python -m unittest tests.test_gpu_index_refresh tests.test_startup_guard tests.test_adapter_report tests.test_gpu_router tests.test_gpu_ghosts tests.test_gpu_selections tests.test_ls_settings tests.test_server tests.test_automatic_updates
```

Use a fresh CMake directory if the existing directory uses a different generator.
The WPF fixture's fake `Lossless.dll` exists only under
`build/native/startup-guard-wpf-test`; it must never be copied to an LS install or
packaged as production content. The native idle report test requires
`hasDevice=false`, the exact child PID and request token, intact duplicate names,
and both raw enumerations.

## Live validation procedure

1. Record the configured `LosslessScaling.exe` path, UI/engine file versions and
   hashes, and Companion's settings directory. Back up Companion's settings and
   the configured LS XML. Leave the user's driver setup and Steam files intact.
   Do not enable the old experimental runtime clone or old proxy/render add-ons.
2. Confirm Companion has control configured and native Auto Scale disabled.
   Close profile editors, stop scaling, and finish any benchmark/update. Start
   Companion from this checkout with the newly built native files. Watch the
   `LSCompanion.GpuIndexRefresh` logger and `gpuIndexMapping` in websocket
   INITIAL_STATE/FULL_DATA_UPDATE/STATE_UPDATE payloads. States include deferred,
   checking, needs_validation, mapped_idle, partial, and error.
3. At startup, observe the hidden idle LS launch/restart. Confirm there is no
   scaling hotkey, blue border/toast, rendering session, or game focus grab.
   If LS was closed initially, the check must leave it closed; if it was already
   open and idle, it must leave an idle instance running.
4. In the configured Companion directory, inspect
   `diagnostics/ls-adapter-report-<PID>.json`. Check its `requestId`, PID, file time,
   `hasDevice=false`, `lsAdapterNames`, and the LUIDs/order of both DXGI arrays.
   Verify the PID's executable path. The probe requires a fresh report for its
   own child; an old PID, reused PID, or newer foreign report must not be used.
5. Open the actual LS GPU dropdown on that same running instance and compare
   **every entry in order**, including duplicates and Auto. Do not start scaling
   to discover indices. Select entries only in a temporary profile, close LS so
   it saves, and inspect that temporary profile's `PreferredGpuId` against its
   selected dropdown position. Restore/remove only the temporary test profile
   afterward while LS is closed. Merely identifying a GPU index is distinct from
   proving which GPU renders successfully; retain runtime device verification.
6. Use one temporary explicit 5050 profile and one Auto profile. With five 3090
   aliases before the 5050, expect 5050 slot 6. Reconnect a Parsec/Vibeshine display
   normally and repeat with fewer/more aliases. The 5050 should follow its saved
   identity; Auto stays zero; output-display IDs and unrelated settings stay as
   saved. Unknown/duplicate physical models should be reported and preserved.
7. Exercise an already running scaling session, an open profile editor, a test,
   and an update. Pending slow work must defer and retry shortly after they end.
   A profile save arriving during the short transaction should queue and complete
   afterward without losing the draft. No automatic focus grab is allowed.
8. Check the three-hour path with an injected clock/probe in a test harness, not
   by changing the OS clock or production interval: unchanged raw snapshots only
   run the fast probe; reordered entries, changed LUIDs, duplicate count changes,
   or a new LS PID require the slow idle check. Confirm a new native helper is
   actually included when testing a frozen Companion build.
9. Repeat across several LS restarts. If LS changes its order between the probe
   launch and the confirmation launch, expect rollback/error, never acceptance
   of a guessed map. Record both reports. A fresh idle map cannot promise that
   drivers will keep the order unchanged in a later process or display change.

The native name getter is presently called from a bounded worker, rather than
the WPF thread. If live inspection shows thread affinity or a startup race, marshal
that call to the already-subclassed LS window thread after initialization; keep
the process/PID/token checks and module-lifetime guarantees. Do not solve missing
reports by falling back to guessed DXGI indices or a scaling-session calibration.

## Recovery and evidence to leave

XML writes retain the ordinary untouched `Settings.xml.bak`. A transaction also
writes `Settings.xml.gpu-refresh-recovery` before its batch edit; it removes that
file after success or a successful rollback. If a process becomes active or a
restore fails, leave the recovery file intact. The next safe check attempts its
restore with LS stopped. For manual recovery, stop Companion and LS first, inspect
and back up both current files, then restore the transaction snapshot atomically;
do not replace settings while LS is live. Preserve unknown XML fields.

Leave the exact LS hashes, raw same-PID adapter list/dropdown comparison, before
and after native/helper profile values, process-running state, deferred-check
results, and test output. If the installed build cannot be verified, retain
`needs_validation` and describe the specific missing evidence. Do not publish a
release claiming live hardware verification based only on fixture tests.
