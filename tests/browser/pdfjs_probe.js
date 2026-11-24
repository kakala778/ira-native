async (page) => {
  const root = "__URL__";
  const phase = "__PHASE__";
  const id = "__EVIDENCE__";
  const evidenceURL = root + "/workbench/evidence/" + id;
  const feature = await page.evaluate(() => ({
    agent: navigator.userAgent,
    upsert: typeof Map.prototype.getOrInsertComputed,
  }));
  const foreignRequests = [];
  const browserErrors = [];
  const expectedAborts = new Set();
  page.on("pageerror", error => browserErrors.push(String(error)));
  page.on("console", event => {
    if (event.type() === "error" && !(
      expectedAborts.has(event.location().url) && event.text().startsWith("Failed to load resource:")
    )) browserErrors.push(event.text());
  });
  page.on("request", request => {
    if (!request.url().startsWith(root + "/") && !request.url().startsWith("blob:")) {
      foreignRequests.push(request.url());
    }
  });
  if (phase === "after") {
    await page.goto(evidenceURL);
    await page.locator('[data-pdf][data-render-state="canvas"] canvas').waitFor();
    await page.locator(".pdf-highlight").waitFor();
  }
  await page.route("**/pdf.worker.min.mjs", async route => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      body: "delete Map.prototype.getOrInsertComputed; delete Map.prototype.getOrInsert;" +
            "delete WeakMap.prototype.getOrInsertComputed; delete WeakMap.prototype.getOrInsert;\n" +
            await response.text(),
    });
  });
  await page.addInitScript(() => {
    for (const proto of [Map.prototype, WeakMap.prototype]) {
      delete proto.getOrInsert;
      delete proto.getOrInsertComputed;
    }
  });
  await page.goto(evidenceURL);
  const highlight = page.locator(".pdf-highlight");
  if (phase === "before") {
    await page.locator("img.page-fallback").waitFor();
    if (await page.locator("canvas").count() || await highlight.count()) {
      throw new Error("Expected PNG fallback with no canvas or overlay");
    }
    const error = await page.evaluate(async () => {
      try {
        const pdfjs = await import("/workbench/static/pdfjs/pdf.min.mjs");
        pdfjs.GlobalWorkerOptions.workerSrc = "/workbench/static/pdfjs/pdf.worker.min.mjs";
        const src = document.querySelector("[data-pdf]").dataset.pdf;
        const pdf = await pdfjs.getDocument({url:src}).promise;
        const p = await pdf.getPage(1);
        const canvas = document.createElement("canvas");
        await p.render({canvas, viewport:p.getViewport({scale:1})}).promise;
      } catch (e) {
        return String(e);
      }
      throw new Error("Expected direct PDF.js failure");
    });
    if (!error.includes("getOrInsertComputed is not a function")) {
      throw new Error("Unexpected baseline failure: " + error);
    }
    return {verified:true, feature, faultSimulation:true, error, foreignRequests};
  }

  const view = page.locator("[data-pdf]");
  await page.locator('[data-pdf][data-render-state="canvas"]').waitFor();
  if (await view.getAttribute("data-precision") !== "block") throw new Error("Missing rendered block precision");
  const metrics = await page.evaluate(() => {
    const stage = document.querySelector("[data-stage]");
    const canvas = stage.querySelector("canvas");
    const frame = stage.querySelector(".pdf-highlight").getBoundingClientRect();
    const bounds = canvas.getBoundingClientRect();
    const [x0,y0,x1,y1] = JSON.parse(document.querySelector("[data-pdf]").dataset.bbox);
    const expected = [bounds.x+x0*bounds.width/200,
      bounds.y+(200-y1)*bounds.height/200,
      (x1-x0)*bounds.width/200, (y1-y0)*bounds.height/200];
    const actual = [frame.x,frame.y,frame.width,frame.height];
    const pixels = canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data;
    let ink = 0, inkInBox = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i] >= 180 || pixels[i+3] < 200) continue;
      ink++;
      const x = (i/4 % canvas.width) * 200/canvas.width;
      const y = 200 - Math.floor(i/4 / canvas.width) * 200/canvas.height;
      if (x0 <= x && x <= x1 && y0 <= y && y <= y1) inkInBox++;
    }
    return {delta:Math.max(...expected.map((x,i)=>Math.abs(x-actual[i]))), ink, inkInBox,
      physicalPage:document.querySelector("[data-current]").textContent};
  });
  if (metrics.delta > 0.05 || metrics.inkInBox < 50 || metrics.physicalPage !== "1") {
    throw new Error("Canvas pixels, page number or highlight alignment failed " + JSON.stringify(metrics));
  }
  await page.locator("[data-next]").click();
  await page.locator('[data-pdf][data-render-state="canvas"] canvas[aria-label="PDF 物理第 2 页"]').waitFor();
  if (await highlight.count() || await view.getAttribute("data-precision") !== "page")
    throw new Error("Old overlay or block precision survived page turn");
  await page.route("**/pdf.min.mjs", route => {
    expectedAborts.add(route.request().url());
    return route.abort();
  });
  await page.goto(evidenceURL);
  await page.locator('[data-pdf][data-render-state="fallback"]').waitFor();
  const fallback = await view.locator("[data-pdf-status]").textContent();
  if (await view.getAttribute("data-evidence-precision") !== "block" ||
      await view.getAttribute("data-precision") !== "page" || !fallback.includes("高亮未显示") ||
      await highlight.count() || await page.locator("canvas").count()) {
    throw new Error("PNG fallback conflated with location precision or Canvas");
  }
  await page.route("**/image", route => {
    expectedAborts.add(route.request().url());
    return route.abort();
  });
  await page.goto(evidenceURL);
  await page.locator('[data-pdf][data-render-state="error"]').waitFor();
  const failure = await view.locator("[data-pdf-status]").textContent();
  if (!failure.includes("无法预览") || await highlight.count() ||
      await view.getAttribute("data-precision") !== "none") {
    throw new Error("Missing explicit error display");
  }
  if (foreignRequests.length) throw new Error("Unexpected network requests");
  if (browserErrors.length) throw new Error("Unexpected browser errors: " + browserErrors.join(" | "));
  return {verified:true, feature, nativeCanvas:true, faultSimulation:true, metrics, fallback, failure,
    pageTurnOverlay: false, browserErrors, foreignRequests};
}
