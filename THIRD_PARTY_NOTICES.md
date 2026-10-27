# Third-party notices

## Echo LS Add-on Manager

LS Companion downloads official release packages from
[Echo-Storm/ls-addon-manager](https://github.com/Echo-Storm/ls-addon-manager) when
the user chooses Install or Update. Echo LS Add-on Manager is a separate project;
it is not covered by LS Companion's license.

The manager began as LosslessProxy by FrankBarretta. Attribution retained from
the upstream project:

> Copyright (c) 2025 FrankBarretta (LosslessProxy, the project this one started from)  
> Copyright (c) 2026 Echo-Storm

Echo LS Add-on Manager is distributed under the MIT License:

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Echo's release contains separately licensed third-party components and runtimes,
including components from AMD, Intel, Microsoft, and NVIDIA. LS Companion keeps
the license and notice files shipped in the verified Echo payload. The upstream
[NOTICE](https://github.com/Echo-Storm/ls-addon-manager/blob/main/NOTICE.md) is
the authoritative component-level attribution and licensing inventory.

## py7zr

Special K's official 7-Zip download is read with unmodified
[py7zr](https://github.com/miurahr/py7zr), licensed under LGPL-2.1-or-later.
Packaged builds include its distribution metadata, full license and dependency
notices. Its source is available from the upstream repository; the dependency
range used to rebuild Companion is recorded in `companion/requirements.txt`.

