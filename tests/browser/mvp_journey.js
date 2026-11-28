async page => {
  const live = "__ROOT__";
  const failure = "__FAIL__";
  const dest = "__OUT__";
  const allowed = new Set([live, failure]);
  const requests = { local: 0, external: [], resourceTypes: {} };
  const errors = [];
  const stages = {};
  page.context().on("request", request => {
    const url = new URL(request.url());
    if (allowed.has(url.origin)) {
      requests.local++;
      requests.resourceTypes[request.resourceType()] = (requests.resourceTypes[request.resourceType()] || 0) + 1;
    } else if (!["blob:", "data:"].includes(url.protocol)) {
      requests.external.push(url.origin);
    }
  });
  await page.context().route("**/*", async route => {
    const url = new URL(route.request().url());
    if (allowed.has(url.origin) || ["blob:", "data:"].includes(url.protocol)) await route.continue();
    else await route.abort();
  });
  page.on("pageerror", e => errors.push(String(e)));
  page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });

  const check = (condition, reason) => { if (!condition) throw new Error(reason); };
  const heading = async name => page.getByRole("heading", { name, exact: true }).first().waitFor();
  const click = async name => page.getByRole("button", { name, exact: true }).click();
  const fetchSummary = async () => page.evaluate(async () => {
    const r = await fetch(location.pathname + ".json");
    if (!r.ok) throw new Error("Summary failed");
    return r.json();
  });
  const upload = async (pdf, artifact, sheet, options) => {
    await page.goto(live + "/workbench/import");
    const pdfUpload = page.getByText("上传规格 PDF 和解析文件", {exact: true});
    if (!(await page.locator('[name="pdf"]').isVisible())) await pdfUpload.click();
    await page.locator('[name="pdf"]').setInputFiles(pdf);
    await page.locator('[name="parsed"]').setInputFiles(artifact);
    await click("登记技术规格");
    await heading("选择设备清单");
    const sheetUpload = page.getByText("上传设备清单", {exact: true});
    if (!(await page.locator('[name="spreadsheet"]').isVisible())) await sheetUpload.click();
    await page.locator('[name="spreadsheet"]').setInputFiles(sheet);
    if (options.sheet || options.mapping) await page.getByText("导入设置", {exact: true}).click();
    if (options.sheet) await page.locator('[name="sheet"]').fill(options.sheet);
    if (options.mapping) {
      await page.getByText("指定表头映射", { exact: true }).click();
      for (const [key, value] of Object.entries(options.mapping)) await page.locator('[name="column_' + key + '"]').fill(value);
    }
    await click("登记设备清单");
    await page.getByText("查看字段映射", { exact: true }).waitFor();
    if (options.pages) {
      await page.getByText("限定核验页范围", {exact: true}).click();
      await page.locator('[name="pages"]').fill(options.pages);
    }
    await click("查看候选项");
    await heading("核验草案");
    return page.url();
  };
  const runDraft = async (draft, select) => {
    await page.goto(draft);
    if (select) {
      const row = page.locator(".candidate-row").first();
      await row.locator(":scope > summary").click();
      await row.getByText("选择候选与条件化计算", {exact: true}).click();
      const first = page.locator('select[name^="declaration_"]').first();
      const name = await first.getAttribute("name");
      await first.selectOption({ index: 1 });
      await page.locator('[name="confirmed_id"][value="' + name.replace("declaration_", "") + '"]').check();
      await page.locator('[name^="same_version_"]').first().check();
      await page.getByText("计算设置", {exact: true}).click();
      await page.locator('[name="conditional_preview"]').check();
      await page.locator('[name="mapping_reviewed"]').check();
    }
    await page.locator('[name="reviewer"]').fill("SYNTH_OPERATOR_ONLY");
    await click("运行核验");
    await heading("核验审查");
    return new URL(page.url()).pathname;
  };
  const pairAt = async (draft, index) => {
    await page.goto(draft);
    const region = page.locator(".candidate-row").nth(index);
    await region.locator(":scope > summary").click();
    await region.getByRole("link", { name: /核对这组候选/ }).first().click();
    await heading("配对人工核验");
    return page.url();
  };
  const record = async (category, decision) => {
    const form = page.locator('form:has(input[name="category"][value="' + category + '"])');
    await form.locator("..").locator(":scope > summary").click();
    if (category === "fields") {
      for (const part of await form.locator("details").all()) {
        await part.locator("summary").click();
        await part.locator('input[name="target"]').check();
      }
    }
    await form.locator('[name="reviewer"]').fill("SYNTH_OPERATOR_ONLY");
    await form.locator('[name="note"]').fill("SYNTH_NOTE_ONLY");
    await form.locator('[name="decision"]').selectOption(decision);
    await form.locator('[name="attest"]').check();
    await form.getByRole("button", { name: "保存本项核验" }).click();
    await heading("配对人工核验");
  };
  const reviewed = async (draft, index, categories, expected) => {
    const route = await pairAt(draft, index);
    await click("查看原文");
    await page.locator('[data-pdf][data-render-state="canvas"] canvas').waitFor();
    await click("关闭证据栏");
    check(await page.getByRole("heading", { name: "清单原始行" }).count() === 1, "No declaration row");
    for (const category of categories) await record(category, "confirmed");
    await page.locator('form[action$="recalculate"] [name="reviewer"]').fill("SYNTH_OPERATOR_ONLY");
    await click("按当前记录重算");
    await heading("核验审查");
    await page.getByRole("link", { name: "查看报告" }).click();
    await heading("核验报告");
    const summary = await fetchSummary();
    check(summary.findings[index].current_disposition === expected, "Unexpected disposition: " + JSON.stringify(summary.counts));
    return { route, report: page.url(), summary };
  };

  const draft = await upload("__PDF__", "__PARSED__", "__SHEET__", { sheet: "Synthetic devices", pages: "1-3" });
  const initial = await runDraft(draft, true);
  const agentTab = () => page.getByRole("navigation", {name:"当前任务"}).getByRole("link", {name:"Agent 补查"}).click();
  await agentTab();
  await page.locator('[name="goal"]').fill("Call search_evidence once with query GPON VERIFY TRACE. If found, return the exact evidence_id with decision evidence_found.");
  await click("补查证据缺口");
  await heading("证据补查");
  const suggestion = page.getByRole("link", { name: /打开原始证据块/ }).first();
  await suggestion.waitFor({ timeout: 100000 });
  await page.getByText("模型与授权详情", {exact:true}).click();
  const task = await page.getByRole("link", { name: "导出 JSON 轨迹" }).getAttribute("href");
  const trace = await page.evaluate(async path => (await fetch(path)).json(), task);
  stages.agent = { state: trace.task_state, stop: trace.stop_reason, calls: trace.usage.tool_calls, backend: trace.backend.backend };
  check(trace.model_mode === "local" && trace.evidence_ids.length > 0, "Expected live model evidence");
  await suggestion.click();
  await page.locator('[data-pdf][data-render-state="canvas"] canvas').waitFor();
  const initialPrecision = await page.locator("[data-pdf]").getAttribute("data-precision");
  const highlight = await page.locator(".pdf-highlight").count();
  await page.getByRole("button", { name: "下一物理页" }).click();
  await page.waitForFunction(() => document.querySelector("[data-current]")?.textContent.trim() === "2");
  check(await page.locator(".pdf-highlight").count() === 0, "Old highlight retained on next page");
  await page.getByRole("link", { name: "返回补查任务" }).click();
  await heading("证据补查");
  stages.viewer = { canvas: true, initialPrecision, highlight, secondPage: true, returnedToTask: true };

  await page.goto(failure + initial);
  await agentTab();
  await page.locator('[name="goal"]').fill("Find OLT evidence.");
  await click("补查证据缺口");
  await heading("证据补查");
  await page.locator('[data-task-state="blocked"]').waitFor({ timeout: 20000 });
  check(await page.locator("[data-task-state]").getAttribute("data-agent-status") === "model_unavailable", "Unexpected fallback result");
  await page.getByRole("link", { name: "返回审查" }).click();
  await heading("核验审查");
  stages.modelFailure = "model_unavailable; verification still accessible";

  const all = ["fields", "source", "binding", "applicability"];
  const positive = await reviewed(draft, 0, all, "no_issue");
  check(!JSON.stringify(positive.summary).match(/SYNTH_OPERATOR_ONLY|SYNTH_NOTE_ONLY|raw_content|effective_requirement|original_cells/),
        "Sensitive details appeared in summary");
  await page.screenshot({ path: dest + "/edge-report.png", fullPage: true });
  await page.emulateMedia({ media: "print" });
  check(await page.locator(".report-actions").first().isHidden(), "Print stylesheet did not hide actions");
  await page.pdf({ path: dest + "/edge-synthetic-report.pdf", printBackground: false });
  await page.emulateMedia({ media: "screen" });
  const negative = await reviewed(draft, 1, all, "issue");
  const unknown = await reviewed(draft, 2, ["fields", "source", "binding"], "review_required");
  stages.trusted = [positive.summary.findings[0].formal_status, negative.summary.findings[1].formal_status,
                    unknown.summary.findings[2].formal_status];
  check(unknown.summary.findings[2].trust_blockers.includes("APPLICABILITY_NOT_REVIEWED"), "Unknown condition not diagnosed");

  await page.goto(positive.route);
  await record("applicability", "rejected");
  await page.goto(positive.report);
  const historical = await fetchSummary();
  check(historical.status === "stale" && historical.findings[0].historical_disposition === "no_issue" &&
        historical.findings[0].current_disposition === "review_required", "History was promoted or lost");
  await page.goto(live);
  check(await page.getByRole("link", { name: /继续核验/ }).count() > 0, "Run not discoverable");
  check(await page.getByRole("link", { name: /继续草案/ }).count() > 0, "Draft not discoverable");
  stages.history = { stale: historical.status, prior: historical.findings[0].historical_disposition, resumable: true };

  const native = await upload("__NATIVEPDF__", "__NATIVEJSON__", "__NATIVESHEET__", { pages: "1", mapping: {
    equipment_class: "设备类别", manufacturer_model: "厂家型号", configuration_variant: "配置",
    scope: "工程范围", site_group: "站点组", config: "接口配置", quantity: "数量", unit: "单位",
    count_basis: "计数口径", condition: "安装条件", source_revision: "来源版本",
  }});
  const nativeRun = await runDraft(native, false);
  await page.getByRole("link", { name: "查看报告" }).click();
  const nativeReport = await fetchSummary();
  check(Object.keys(nativeReport.counts).every(status => status === "review_required"), "Native fixture was promoted");
  stages.nativeMineru = { run: nativeRun, status: nativeReport.status, counts: nativeReport.counts };
  check(requests.external.length === 0, "External requests: " + JSON.stringify(requests.external));
  check(errors.length === 0, "Browser errors: " + JSON.stringify(errors));
  return { verified: true, browser: await page.evaluate(() => navigator.userAgent), requests, errors, stages };
}
