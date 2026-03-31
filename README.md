lspleanklib
===========

This repository is online at both:

* [gitlab.com](https://gitlab.com/castedo/lspleanklib) for active development, and
* [github.com](https://github.com/castedo/lspleanklib) as a mirror.


lspleanklib is a low-level library that implements functionality used by
[webleank](https://gitlab.com/castedo/webleank/), such as:

* LSP,
* JSON-RPC,
* multiplexing multiple `lake serve` workspace sessions into one unified editor LSP session,
* connecting to `lake serve` via local UNIX domain sockets,
* running `lake serve` outside the editor process,
* and proper reading of stdin with Python asyncio.

It has no required dependencies on Linux and macOS, but Windows requires the Python
package `platformdirs` to be installed. The pip install package specification
`lspleanklib[crossplatform]` will add `platformdirs` as a requirement.

This package also includes two CLI utilities for advanced usage:
* lspleank: an LSP server used by webleank to run inside an editor process, and
* lakelspout: a Lake LSP proxy that connects via local UNIX domain sockets to lspleank.

For more information, visit [webleank](https://gitlab.com/castedo/webleank) and
[lean.castedo.com](https://lean.castedo.com).
