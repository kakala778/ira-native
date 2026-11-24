# PDF.js

Local web preview uses the unmodified browser build of PDF.js from Mozilla's
`pdfjs-dist` npm package, version `6.5.438`. The npm archive integrity is
`sha512-vg8Pw1x+AFKy4AMDvhuWl5cOYV8d698UoEANoFFhc79hqXDnZQqnOXoxw3HG907WHOg1JYOvLhP756boddFxpA==`.

The vendored subset contains `build/pdf.min.mjs`, `build/pdf.worker.min.mjs`,
`cmaps`, `standard_fonts`, `wasm`, and `iccs`. The original `LICENSE` and
the third-party notices in those asset directories accompany the files.

Upstream source and build instructions are available from the
[PDF.js repository](https://github.com/mozilla/pdf.js).
