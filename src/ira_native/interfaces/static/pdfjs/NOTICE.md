# PDF.js

Local web preview uses the unmodified legacy browser build of PDF.js from Mozilla's
`pdfjs-dist` npm package, version `6.5.438`. The npm archive integrity is
`sha512-vg8Pw1x+AFKy4AMDvhuWl5cOYV8d698UoEANoFFhc79hqXDnZQqnOXoxw3HG907WHOg1JYOvLhP756boddFxpA==`.

The vendored subset contains `legacy/build/pdf.min.mjs`,
`legacy/build/pdf.worker.min.mjs` (stored at the root of this directory),
`cmaps`, `standard_fonts`, `wasm`, and `iccs`. The legacy display and worker
modules are paired at the same version for older browser runtimes, including
those without `Map.prototype.getOrInsertComputed`. The original `LICENSE` and
the third-party notices in those asset directories accompany the files.

Upstream source and build instructions are available from the
[PDF.js repository](https://github.com/mozilla/pdf.js).
