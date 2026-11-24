const assets = new URL("./pdfjs/", import.meta.url);
let library;

function moduleForPdf() {
  library ??= import(new URL("pdf.min.mjs", assets)).then((pdfjs) => {
    pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdf.worker.min.mjs", assets).href;
    return pdfjs;
  });
  return library;
}

function drawHighlight(sheet, viewport, bbox) {
  const [x1, y1] = viewport.convertToViewportPoint(bbox[0], bbox[1]);
  const [x2, y2] = viewport.convertToViewportPoint(bbox[2], bbox[3]);
  const frame = document.createElement("div");
  frame.className = "pdf-highlight";
  frame.setAttribute("aria-hidden", "true");
  frame.style.left = Math.min(x1, x2) + "px";
  frame.style.top = Math.min(y1, y2) + "px";
  frame.style.width = Math.abs(x2 - x1) + "px";
  frame.style.height = Math.abs(y2 - y1) + "px";
  sheet.append(frame);
}

function viewer(container) {
  const stage = container.querySelector("[data-stage]");
  const status = container.querySelector("[data-pdf-status]");
  const current = container.querySelector("[data-current]");
  const total = container.querySelector("[data-total]");
  const previous = container.querySelector("[data-prev]");
  const next = container.querySelector("[data-next]");
  const originalPage = Number(container.dataset.page);
  const evidenceBox = JSON.parse(container.dataset.bbox);
  let pageNumber = originalPage;
  let pageCount = Number(total.textContent);
  let revision = 0;
  let renderTask;
  let priorWidth = 0;
  const pdf = moduleForPdf().then((pdfjs) => pdfjs.getDocument({
    url: container.dataset.pdf,
    enableXfa: false,
    isEvalSupported: false,
    cMapUrl: new URL("cmaps/", assets).href,
    cMapPacked: true,
    standardFontDataUrl: new URL("standard_fonts/", assets).href,
    wasmUrl: new URL("wasm/", assets).href,
    iccUrl: new URL("iccs/", assets).href,
  }).promise);

  function buttons() {
    current.textContent = pageNumber;
    total.textContent = pageCount;
    previous.disabled = pageNumber <= 1;
    next.disabled = pageNumber >= pageCount;
  }

  async function showPage() {
    const id = ++revision;
    const number = pageNumber;
    renderTask?.cancel();
    stage.replaceChildren();
    buttons();
    status.textContent = "正在载入 PDF 物理第 " + number + " 页";
    try {
      const file = await pdf;
      if (id !== revision) return;
      pageCount = file.numPages;
      const page = await file.getPage(number);
      if (id !== revision) return;
      const unscaled = page.getViewport({ scale: 1 });
      const scale = Math.min((stage.clientWidth - 28) / unscaled.width, 1.7, 1400 / unscaled.height);
      if (scale <= 0) throw new Error("No preview area");
      const viewport = page.getViewport({ scale });
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const sheet = document.createElement("div");
      sheet.className = "pdf-sheet";
      const canvas = document.createElement("canvas");
      canvas.setAttribute("aria-label", "PDF 物理第 " + number + " 页");
      canvas.width = Math.ceil(viewport.width * ratio);
      canvas.height = Math.ceil(viewport.height * ratio);
      canvas.style.width = viewport.width + "px";
      canvas.style.height = viewport.height + "px";
      sheet.append(canvas);
      renderTask = page.render({
        canvas,
        canvasContext: canvas.getContext("2d"),
        viewport,
        transform: [ratio, 0, 0, ratio, 0, 0],
        annotationMode: 0,
      });
      await renderTask.promise;
      if (id !== revision) return;
      const anchored = number === originalPage && container.dataset.precision === "block" &&
        Array.isArray(evidenceBox) && evidenceBox.length === 4 && evidenceBox.every(Number.isFinite);
      if (anchored) drawHighlight(sheet, viewport, evidenceBox);
      stage.replaceChildren(sheet);
      status.textContent = "物理第 " + number + " 页 · " + (anchored ? "块级定位" : "页级定位");
      buttons();
    } catch {
      if (id !== revision) return;
      const fallback = new Image();
      fallback.className = "page-fallback";
      fallback.alt = "PDF 物理第 " + number + " 页";
      fallback.src = container.dataset.pageImage + number + "/image";
      fallback.addEventListener("error", () => {
        if (id === revision) status.textContent = "原页预览不可用";
      });
      stage.replaceChildren(fallback);
      status.textContent = "物理第 " + number + " 页 · 页级预览";
      buttons();
    }
  }

  previous.addEventListener("click", () => {
    if (pageNumber > 1) {
      pageNumber -= 1;
      void showPage();
    }
  });
  next.addEventListener("click", () => {
    if (pageNumber < pageCount) {
      pageNumber += 1;
      void showPage();
    }
  });
  const observer = new ResizeObserver(() => {
    const width = stage.clientWidth;
    if (width !== priorWidth && width > 28) {
      priorWidth = width;
      void showPage();
    }
  });
  observer.observe(stage);
}

for (const container of document.querySelectorAll("[data-pdf]")) viewer(container);
