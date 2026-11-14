# Overlay input integration

This is an opt-in Windows desktop test. It opens windows, changes focus, and
sends keys only after verifying that a test-owned window has foreground focus.
It never uses the native test-only focus overrides.

Build the packaged add-on host and runtime isolation bootstrap using the normal
repository build scripts, then build these fixtures with Visual Studio CMake:

```powershell
cmake -S tests/native/overlay_input -B build/overlay-input-fixture -A x64
cmake --build build/overlay-input-fixture --config Release
```

Run from the repository root with the development Python environment:

```powershell
python scripts/test_overlay_hook.py --ls-source build/overlay-input-ls/LosslessScaling.exe --reshade-dll build/native/panel-reshade-test/ReShade64.dll --output build/overlay-input-desktop-run
```

`--ls-source` must point to a complete LS installation. The runner creates a
separate managed clone, private Settings.xml, and add-on configuration beneath
the output directory. It preserves the source and existing user settings.
The input test disables frame generation and HDR and uses neutral GPU
preferences in its private settings. It is not a GPU performance benchmark.

`--reshade-dll` is optional and requires the actual ReShade injector exporting
`ReShadeVersion`; a DLL named `dxgi.dll` may instead be Special K. The runner
uses Companion's SHA-256 component manifest and startup hook to load it.
This checks module loading; it does not infer menu visibility from a successful
load. The test does not change the installed application's shortcut bindings.

Without `--ls-source`, the runner loads the **packaged Lossless.dll** into a
native presentation fixture with a fake engine forwarder. The workload still
uses the real benchmark source, compiled with WARP so it runs without a hardware
GPU. This mode checks subclass installation, click-through hit testing, actual
Home delivery to the window, restoration, and focus transitions. It does not
emulate LS capture or ReShade rendering.

On an interactive desktop, the runner checks explicit enable/disable over the
real named pipe, a foreign border window between LS and the target in Z order,
Alt-Tab-style focus suspension/resume, topmost restoration, and target closure.
Real LS mode uses a private Ctrl+Alt+Shift+F23 binding and requires an actual scaling
presentation. End-to-end ReShade menu visibility still needs visual inspection.

Results and native logs stay in the unique output directory. Exit codes are
0 for completed assertions, 1 for a failed assertion, and 2 when Windows will
not grant foreground ownership. A blocked run still checks real pipe disable
and rejection behavior, and explicitly leaves `passed` false.

The independent native suite can also run without an interactive desktop:

```powershell
build/addon_manager/<build-key>/host/Release/eam_featurestest.exe companion
```

That suite uses real window procedures, but deliberately simulates foreground
eligibility. It must not be reported as a live keyboard/Alt-Tab test.

## Regressions covered

LS 3.2.2's managed app and native engine both observe foreground changes.
Activating its presentation for keyboard input caused those listeners to stop
scaling and destroy the presentation. Revision 6 wraps those LS subscriptions
and excludes the intentional presentation handoff. Activation/window messages
and foreground events for other applications retain their normal handling.

The native presentation fixture registers its own LS-style foreground listener
that destroys its window if that handoff reaches the listener. It also creates
a larger settings window that must remain unhooked. The test checks real Home
delivery, suspension, external foreground callback forwarding, presentation
replacement while suspended, resumption, style restoration, and target closure.
There are no focus-check overrides in this test.

LS can recreate its presentation after suspension. The watcher retains the
requested input state while the same game is alive and attaches to the new
presentation. A test runner must reacquire that HWND after resumption too.

The startup hook formerly wrote an inherited receipt filename with File.Move,
which crashed a relaunched LS child when the parent's receipt already existed.
Receipts now have a PID suffix; the original first-writer receipt remains for
compatibility. The verifier prefers the PID-specific record and still requires
the exact PID, token and private settings path. The native runtime tests start
a real .NET parent/child pair with inherited launch credentials and verify both
receipts without overwriting the parent's identity or the seed settings.

The supplied LS archive contains LS 3.2.2, and its `dxgi.dll` identifies as Special
K 26.8.12. The hosted machine exposes Microsoft Basic Render Driver, which
ReShade 6.8 skips. Successful module loading and keyboard/focus tests therefore
do not establish ReShade effect rendering or menu visibility on a hardware GPU.

## Revision 6 verification artifacts

- `build/overlay-repair-final-package-build.log`: rebuilt bundled host and native checks.
- `build/overlay-repair-final-regressions.log`: 55 packaged-host, isolation and launch tests; one optional runtime test skipped.
- `build/overlay-repair-final-client-tests.log`: 50 client/overlay tests, including rejection of an old running host.
- `build/overlay-repair-real-ls-bootstrap-verified/results.json`: actual LS startup and controlled restart with the same launch credentials; separate verified PID receipts and a surviving replacement process.
- `build/overlay-repair-packaged-fixture-final/results.json`: final desktop rerun is blocked, not passed. Windows reports no foreground window and GetCursorPos error 5; an unlocked input desktop is required to complete it.

The earlier enhanced fixture run exercised Home delivery, forwarding of external
foreground notifications, presentation replacement, and resumption. Its last
assertion incorrectly compared the replacement's restored styles to the old
window's baseline. The runner now records the new baseline; its final full
keyboard/focus rerun remains pending desktop access. The real-LS final focus
test is pending for the same reason. None of these limitations are bypassed by
changing native focus checks.
