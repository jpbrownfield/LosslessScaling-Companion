# LS Companion security model

LS Companion manages graphics add-on files in the configured Lossless Scaling
directory. It does not write add-on DLLs into game directories or modify DLL
contents.

## Safeguards

- Release downloads use HTTPS and a provider-specific hostname allowlist.
- Assets must be listed by the selected provider release; arbitrary download URLs
  are rejected.
- Publisher-provided SHA-256 digests are verified when available. Every download
  is independently hashed and stored by content hash even when the publisher does
  not publish a digest.
- Archives are extracted into quarantine with traversal, link, device-name, file
  count, and expanded-size checks.
- Deployment accepts only an existing file named `LosslessScaling.exe` and rejects
  every destination that escapes its directory.
- Existing files are backed up. Deployments are transactional and restore their
  pre-change state after a failure.
- Managed binaries are checked against their recorded SHA-256 before replacement,
  removal, activation, or parking. Unexpected changes fail closed.
- Downloads and file-state changes produce receipts in
  `logs/security-audit.jsonl` under the configured asset-store directory.
- Release builds are not UPX-packed.

## Release requirements

Public installers and executables should be Authenticode-signed using one stable
publisher identity after the final build step. Signing credentials do not belong
in this repository. Publish SHA-256 sums for the final installer and executable,
and retain the build provenance for every release.

Do not re-sign or alter third-party DLLs. Their package metadata, original hash,
source release, and any publisher-provided digest must remain intact.

## Reporting a false positive

Include the LS Companion version, SHA-256, detection name, security product, and
the relevant audit-log entries. Microsoft Defender submissions can be made as a
software developer at https://www.microsoft.com/wdsi/filesubmission.
