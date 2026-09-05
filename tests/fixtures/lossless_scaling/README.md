# Lossless Scaling XML fixtures

`Settings.xml` is a normalized test fixture based on a complete configuration
publicly posted to the AVSIM forum on 2025-01-22:

https://www.avsim.com/forums/topic/661769-new-version-of-lossless-scaling/page/10/

Lossless Scaling stores its live configuration at:

```text
%LOCALAPPDATA%\Lossless Scaling\Settings.xml
```

The public example predates current Lossless Scaling releases, so consumers
should tolerate missing and unknown elements. Do not copy this fixture over a
live configuration. If testing replacement behavior, work on a temporary copy
and ensure Lossless Scaling is closed first.
