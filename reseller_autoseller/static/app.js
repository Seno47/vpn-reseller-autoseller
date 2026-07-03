let adminToken = localStorage.getItem("reseller_admin_token") || "";

const loginScreen = document.querySelector("#loginScreen");
const loginForm = document.querySelector("#loginForm");
const loginError = document.querySelector("#loginError");
const metrics = document.querySelector("#metrics");
const products = document.querySelector("#products");
const sales = document.querySelector("#sales");
const botUsers = document.querySelector("#botUsers");
const sectionTabs = document.querySelectorAll("[data-section-tab]");
const appSections = document.querySelectorAll("[data-section]");
const logoutButton = document.querySelector("#logoutButton");
const refreshButton = document.querySelector("#refreshButton");
const productForm = document.querySelector("#productForm");
const resetMappingButton = document.querySelector("#resetMappingButton");
const settingsForm = document.querySelector("#settingsForm");
const botUserForm = document.querySelector("#botUserForm");
const restartTelegramButton = document.querySelector("#restartTelegramButton");
const telegramRestartStatus = document.querySelector("#telegramRestartStatus");
const tariffSearch = document.querySelector("#tariffSearch");
const tariffCode = document.querySelector("#tariffCode");
const tariffDropdown = document.querySelector("#tariffDropdown");
const tariffHint = document.querySelector("#tariffHint");
const mappingSource = document.querySelector("#mappingSource");
const parseMappingButton = document.querySelector("#parseMappingButton");
const mappingParseStatus = document.querySelector("#mappingParseStatus");
const variantSearch = document.querySelector("#variantSearch");
const variantDropdown = document.querySelector("#variantDropdown");
const variantHint = document.querySelector("#variantHint");
const mappingSearch = document.querySelector("#mappingSearch");
const templateActionSelect = document.querySelector("#templateActionSelect");
const templateStageSelect = document.querySelector("#templateStageSelect");
const templateCommandField = document.querySelector("#templateCommandField");
const templateCommandInput = document.querySelector("#templateCommandInput");
const deliveryTemplate = document.querySelector("#deliveryTemplate");
const defaultTemplateButton = document.querySelector("#defaultTemplateButton");
const clearTemplateButton = document.querySelector("#clearTemplateButton");
const saveTemplateButton = document.querySelector("#saveTemplateButton");
const templateVariableButtons = document.querySelector("#templateVariableButtons");
const complexVariableSelect = document.querySelector("#complexVariableSelect");
const complexVariableInfo = document.querySelector("#complexVariableInfo");
const complexVariableKey = document.querySelector("#complexVariableKey");
const complexVariableLabel = document.querySelector("#complexVariableLabel");
const complexVariableTemplate = document.querySelector("#complexVariableTemplate");
const complexVariableButtons = document.querySelector("#complexVariableButtons");
const newComplexVariableButton = document.querySelector("#newComplexVariableButton");
const deleteComplexVariableButton = document.querySelector("#deleteComplexVariableButton");
const defaultComplexVariableButton = document.querySelector("#defaultComplexVariableButton");
const clearComplexVariableButton = document.querySelector("#clearComplexVariableButton");
const saveComplexVariableButton = document.querySelector("#saveComplexVariableButton");
const statisticsPeriod = document.querySelector("#statisticsPeriod");
const statisticsMetrics = document.querySelector("#statisticsMetrics");
const statisticsMarketplaces = document.querySelector("#statisticsMarketplaces");
const statisticsActions = document.querySelector("#statisticsActions");
const statisticsTariffs = document.querySelector("#statisticsTariffs");
const statisticsDays = document.querySelector("#statisticsDays");
const actionParamsField = document.querySelector("#actionParamsField");
const actionParamsTitle = document.querySelector("#actionParamsTitle");
const actionParamAmountField = document.querySelector("#actionParamAmountField");
const actionParamAmount = document.querySelector("#actionParamAmount");
const actionParamFullPeriodField = document.querySelector("#actionParamFullPeriodField");
const actionParamFullPeriod = document.querySelector("#actionParamFullPeriod");
const actionParamMonthsField = document.querySelector("#actionParamMonthsField");
const actionParamMonths = document.querySelector("#actionParamMonths");
const actionParamsHint = document.querySelector("#actionParamsHint");

let tariffRows = [];
let variantRows = [];
let productRows = [];
let editingProductId = null;
let defaultDeliveryTemplate = "";
let deliveryTemplateVariables = [];
let deliveryTemplateActions = [];
let deliveryTemplateGroups = [];
let complexVariables = [];
let ordinaryComplexVariableSources = [];
let editingComplexVariableKey = "";
let statisticsData = null;

function setActiveSection(section) {
  sectionTabs.forEach((button) => {
    button.classList.toggle("active", button.dataset.sectionTab === section);
  });
  appSections.forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.section === section);
  });
}

function authHeaders(extra = {}) {
  return {
    ...extra,
    Authorization: `Bearer ${adminToken}`,
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: authHeaders(options.headers || {}),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function parseActionParams(value) {
  if (!value) {
    return {};
  }
  if (typeof value === "object") {
    return value;
  }
  try {
    const parsed = JSON.parse(String(value));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (_) {
    return {};
  }
}

function setLabelText(label, text) {
  if (label?.childNodes?.length) {
    label.childNodes[0].nodeValue = `${text}\n              `;
  }
}

function positiveNumber(value, label) {
  const normalized = String(value || "").replace(",", ".").trim();
  const number = Number(normalized);
  if (!Number.isFinite(number) || number <= 0) {
    throw new Error(`${label}: укажите число больше 0.`);
  }
  return Number.isInteger(number) ? number : Number(number.toFixed(2));
}

function positiveInteger(value, label) {
  const number = positiveNumber(value, label);
  if (!Number.isInteger(number)) {
    throw new Error(`${label}: укажите целое число.`);
  }
  return number;
}

function actionParamJsonExample(action) {
  if (action === "traffic") {
    return '{"gigabytes":10}';
  }
  if (action === "ip_limit") {
    return '{"extra_ip_count":1,"full_period":false,"months":1}';
  }
  return "{}";
}

function insertAtCursor(textarea, value) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  textarea.value = `${textarea.value.slice(0, start)}${value}${textarea.value.slice(end)}`;
  const next = start + value.length;
  textarea.focus();
  textarea.setSelectionRange(next, next);
}

function renderTemplateVariables(config) {
  defaultDeliveryTemplate = config.default_template || "";
  deliveryTemplateVariables = Array.isArray(config.variables) ? config.variables : [];
  deliveryTemplateActions = Array.isArray(config.actions) ? config.actions : [];
  deliveryTemplateGroups = Array.isArray(config.action_groups) ? config.action_groups : [];
  templateVariableButtons.innerHTML = deliveryTemplateVariables.map((item) => `
    <button type="button" data-template-token="${escapeHtml(item.token)}" title="${escapeHtml([item.label, item.description].filter(Boolean).join(' · '))}">
      ${escapeHtml(item.token)}
    </button>
  `).join("");
  renderTemplateActionOptions();
}

function renderComplexVariables(config) {
  complexVariables = Array.isArray(config?.variables) ? config.variables : [];
  ordinaryComplexVariableSources = Array.isArray(config?.ordinary_variables) ? config.ordinary_variables : [];
  if (!editingComplexVariableKey && complexVariables.length) {
    editingComplexVariableKey = complexVariables[0].key;
  }
  renderComplexVariableOptions();
  complexVariableButtons.innerHTML = ordinaryComplexVariableSources.map((item) => `
    <button type="button" data-complex-token="${escapeHtml(item.token)}" title="${escapeHtml([item.label, item.description].filter(Boolean).join(' · '))}">
      ${escapeHtml(item.token)}
    </button>
  `).join("");
  syncComplexVariableEditor();
}

function renderComplexVariableOptions() {
  const builtin = complexVariables.filter((item) => item.builtin);
  const custom = complexVariables.filter((item) => !item.builtin);
  const optionHtml = [];
  if (builtin.length) {
    optionHtml.push(`
      <optgroup label="Встроенные">
        ${builtin.map((item) => `<option value="${escapeHtml(item.key)}">${escapeHtml(item.token)} · ${escapeHtml(item.label)}</option>`).join("")}
      </optgroup>
    `);
  }
  if (custom.length) {
    optionHtml.push(`
      <optgroup label="Свои">
        ${custom.map((item) => `<option value="${escapeHtml(item.key)}">${escapeHtml(item.token)} · ${escapeHtml(item.label)}</option>`).join("")}
      </optgroup>
    `);
  }
  complexVariableSelect.innerHTML = optionHtml.join("");
  if (complexVariables.some((item) => item.key === editingComplexVariableKey)) {
    complexVariableSelect.value = editingComplexVariableKey;
  } else if (complexVariables.length) {
    editingComplexVariableKey = complexVariables[0].key;
    complexVariableSelect.value = editingComplexVariableKey;
  }
}

function selectedComplexVariable() {
  return complexVariables.find((item) => item.key === editingComplexVariableKey);
}

function syncComplexVariableEditor() {
  const variable = selectedComplexVariable();
  const isNew = editingComplexVariableKey === "__new__";
  complexVariableKey.disabled = Boolean(variable?.builtin);
  deleteComplexVariableButton.disabled = !variable || Boolean(variable.builtin);
  defaultComplexVariableButton.disabled = !variable?.default_template;
  saveComplexVariableButton.disabled = !variable && !isNew;
  if (isNew) {
    complexVariableKey.value = "";
    complexVariableLabel.value = "";
    complexVariableTemplate.value = "";
    complexVariableInfo.textContent = "Создайте переменную, затем используйте её токен в шаблонах выдачи.";
    complexVariableKey.focus();
    return;
  }
  complexVariableKey.value = variable?.key || "";
  complexVariableLabel.value = variable?.label || "";
  complexVariableTemplate.value = variable ? (variable.template || variable.default_template || "") : "";
  complexVariableInfo.textContent = variable?.description || "";
}

function startNewComplexVariable() {
  editingComplexVariableKey = "__new__";
  complexVariableSelect.value = "";
  syncComplexVariableEditor();
}

function productSearchText(row) {
  return [
    row.id,
    row.marketplace,
    row.external_product_id,
    row.external_variant_id,
    row.tariff_code,
    row.action,
    row.title,
  ].join(" ").toLowerCase();
}

function filteredProducts(query) {
  const value = String(query || "").trim().toLowerCase();
  if (!value) {
    return productRows;
  }
  return productRows.filter((row) => productSearchText(row).includes(value));
}

function renderTemplateActionOptions() {
  const current = templateActionSelect.value;
  const groups = new Map();
  deliveryTemplateActions.forEach((action) => {
    const category = action.category || "Прочее";
    if (!groups.has(category)) {
      groups.set(category, []);
    }
    groups.get(category).push(action);
  });
  templateActionSelect.innerHTML = Array.from(groups.entries()).map(([category, actions]) => `
    <optgroup label="${escapeHtml(category)}">
      ${actions.map((action) => (
        `<option value="${escapeHtml(action.key)}">${escapeHtml(action.label)}</option>`
      )).join("")}
    </optgroup>
  `).join("");
  if (deliveryTemplateActions.some((action) => action.key === current)) {
    templateActionSelect.value = current;
  }
  syncTemplateEditor();
}

function selectedTemplateAction() {
  return deliveryTemplateActions.find((action) => action.key === templateActionSelect.value);
}

function syncTemplateEditor() {
  const action = selectedTemplateAction();
  deliveryTemplate.value = action ? (action.template || action.default_template || "") : "";
  deliveryTemplate.disabled = !action;
  saveTemplateButton.disabled = !action;
  defaultTemplateButton.disabled = !action;
  clearTemplateButton.disabled = !action;
}

function selectedTemplateGroup() {
  return deliveryTemplateGroups.find((group) => group.key === templateActionSelect.value);
}

function renderTemplateActionOptions() {
  const current = templateActionSelect.value;
  templateActionSelect.innerHTML = deliveryTemplateGroups.map((group) => (
    `<option value="${escapeHtml(group.key)}">${escapeHtml(group.label)}</option>`
  )).join("");
  if (deliveryTemplateGroups.some((group) => group.key === current)) {
    templateActionSelect.value = current;
  }
  renderTemplateStageOptions();
  syncTemplateEditor();
}

function renderTemplateStageOptions() {
  const group = selectedTemplateGroup();
  const current = templateStageSelect.value;
  const stages = group?.stages || [];
  templateStageSelect.innerHTML = stages.map((stage) => (
    `<option value="${escapeHtml(stage.key)}">${escapeHtml(stage.label)}</option>`
  )).join("");
  if (stages.some((stage) => stage.key === current)) {
    templateStageSelect.value = current;
  }
}

function selectedTemplateAction() {
  const group = selectedTemplateGroup();
  return (group?.stages || []).find((stage) => stage.key === templateStageSelect.value);
}

function syncTemplateEditor() {
  const action = selectedTemplateAction();
  const group = selectedTemplateGroup();
  deliveryTemplate.value = action ? (action.template || action.default_template || "") : "";
  deliveryTemplate.disabled = !action;
  saveTemplateButton.disabled = !action;
  defaultTemplateButton.disabled = !action;
  clearTemplateButton.disabled = !action;
  templateStageSelect.disabled = !group;
  const hasCommand = Boolean(group?.command_action);
  templateCommandField.classList.toggle("hidden", !hasCommand);
  templateCommandInput.disabled = !hasCommand;
  templateCommandInput.value = hasCommand ? (group.command || "") : "";
}

function updateActionParamsVisibility({reset = false} = {}) {
  const action = productForm.elements.action.value;
  const needsParams = action === "traffic" || action === "ip_limit";
  const needsTariff = action === "create" || action === "renew";
  tariffSearch.required = needsTariff;
  if (needsTariff) {
    updateTariffHint();
  } else {
    tariffHint.textContent = "Для этого действия тариф не нужен";
  }
  actionParamsField.classList.toggle("hidden", !needsParams);
  if (!needsParams) {
    productForm.elements.action_params_text.value = "";
    return;
  }

  if (reset) {
    actionParamAmount.value = "";
    actionParamMonths.value = "1";
    actionParamFullPeriod.checked = false;
  }

  if (action === "traffic") {
    actionParamsTitle.textContent = "LTE-трафик";
    setLabelText(actionParamAmountField, "Сколько добавить, ГБ");
    actionParamAmount.placeholder = "например 10";
    actionParamFullPeriodField.classList.add("hidden");
    actionParamMonthsField.classList.add("hidden");
    actionParamsHint.textContent = "Введите объём пополнения. В API будет отправлено: " + actionParamJsonExample(action);
    return;
  }

  actionParamsTitle.textContent = "IP-лимит";
  setLabelText(actionParamAmountField, "Сколько IP добавить");
  actionParamAmount.placeholder = "например 1";
  actionParamFullPeriodField.classList.remove("hidden");
  actionParamMonthsField.classList.toggle("hidden", actionParamFullPeriod.checked);
  actionParamsHint.textContent = actionParamFullPeriod.checked
    ? "Ручной режим: используйте только если цена на витрине уже рассчитана на весь оставшийся срок подписки."
    : "По умолчанию IP добавляется на 1 месяц. Для Digiseller удобно делать отдельные кнопки на 1/3/6/12 месяцев. В API будет отправлено: " + actionParamJsonExample(action);
}

function fillActionParamControls(params) {
  const action = productForm.elements.action.value;
  const parsed = parseActionParams(params);
  if (action === "traffic") {
    actionParamAmount.value = parsed.gigabytes ?? parsed.gb ?? "";
  } else if (action === "ip_limit") {
    actionParamAmount.value = parsed.extra_ip_count ?? parsed.ip_count ?? parsed.count ?? "";
    actionParamFullPeriod.checked = parsed.full_period === true;
    actionParamMonths.value = parsed.full_period === true ? "" : (parsed.months ?? "1");
  } else {
    actionParamAmount.value = "";
    actionParamMonths.value = "1";
    actionParamFullPeriod.checked = false;
  }
  productForm.elements.action_params_text.value = Object.keys(parsed).length ? JSON.stringify(parsed) : "";
  updateActionParamsVisibility();
}

function buildActionParamsFromControls() {
  const action = productForm.elements.action.value;
  if (action === "traffic") {
    return {gigabytes: positiveNumber(actionParamAmount.value, "LTE-трафик")};
  }
  if (action === "ip_limit") {
    const params = {
      extra_ip_count: positiveInteger(actionParamAmount.value, "IP-лимит"),
      full_period: actionParamFullPeriod.checked,
    };
    if (!params.full_period) {
      params.months = positiveInteger(actionParamMonths.value, "Срок IP-лимита");
    }
    return params;
  }
  return {};
}

function showLogin() {
  loginScreen.classList.remove("hidden");
}

function hideLogin() {
  loginScreen.classList.add("hidden");
}

function logout() {
  adminToken = "";
  localStorage.removeItem("reseller_admin_token");
  showLogin();
}

function renderMetrics(status, summary) {
  const balance = summary && summary.balance ? `${summary.balance} ${summary.currency || "RUB"}` : "n/a";
  const telegram = status.telegram_running ? "ON" : "OFF";
  metrics.innerHTML = `
    <article class="metric"><span>Маппингов</span><strong>${status.products}</strong></article>
    <article class="metric"><span>Продаж</span><strong>${status.sales}</strong></article>
    <article class="metric"><span>Telegram</span><strong>${telegram}</strong><small>${status.bot_admins || 0} admin</small></article>
    <article class="metric"><span>Баланс</span><strong>${escapeHtml(balance)}</strong></article>
  `;
}

function moneyText(value, currency = "₽") {
  const text = value?.text ?? "0";
  return currency ? `${text} ${currency}` : text;
}

function revenueText(row) {
  const items = Array.isArray(row?.revenue) ? row.revenue : [];
  if (!items.length) {
    return "0 ₽";
  }
  return items.map((item) => `${escapeHtml(item.text)} ${escapeHtml(item.currency)}`).join("<br>");
}

function actionLabel(action) {
  return {
    create: "Покупка",
    renew: "Продление",
    reissue: "Перевыпуск",
    traffic: "LTE-трафик",
    ip_limit: "IP-лимит",
  }[action] || action;
}

function renderStatisticsTable(target, rows, labelMapper = (value) => value) {
  target.innerHTML = rows.map((row) => `
    <tr>
      <td>${escapeHtml(labelMapper(row.label || row.key))}</td>
      <td>${row.sales_count}<br><small>${row.delivered_count} выдано · ${row.pending_count} ждёт</small></td>
      <td>${revenueText(row)}</td>
      <td>${escapeHtml(moneyText(row.expense_rub))}</td>
      <td>${escapeHtml(moneyText(row.profit_rub))}<br><small>${row.margin_percent === null ? "маржа n/a" : `${row.margin_percent}%`}</small></td>
    </tr>
  `).join("") || `<tr><td colspan="5"><span class="muted">Нет данных за период</span></td></tr>`;
}

function renderStatistics(data) {
  statisticsData = data;
  const totals = data?.totals || {};
  const period = data?.period || {};
  statisticsMetrics.innerHTML = `
    <article class="stat-card"><span>Период</span><strong>${escapeHtml(period.label || "")}</strong><small>${escapeHtml(period.from ? period.from.slice(0, 10) : "всё время")}</small></article>
    <article class="stat-card"><span>Продаж</span><strong>${totals.sales_count || 0}</strong><small>${totals.delivered_count || 0} выдано · ${totals.pending_count || 0} ждёт</small></article>
    <article class="stat-card"><span>Сумма продаж</span><strong>${revenueText(totals)}</strong><small>по валютам площадок</small></article>
    <article class="stat-card"><span>Расход XyraNet</span><strong>${escapeHtml(moneyText(totals.expense_rub))}</strong><small>по сохранённым API-ответам</small></article>
    <article class="stat-card"><span>Прибыль</span><strong>${escapeHtml(moneyText(totals.profit_rub))}</strong><small>${totals.margin_percent === null ? "маржа n/a" : `маржа ${totals.margin_percent}%`} · чек ${escapeHtml(moneyText(totals.avg_order_rub))}</small></article>
  `;
  renderStatisticsTable(statisticsMarketplaces, data?.marketplaces || []);
  renderStatisticsTable(statisticsActions, data?.actions || [], actionLabel);
  renderStatisticsTable(statisticsTariffs, data?.tariffs || []);
  renderStatisticsTable(statisticsDays, data?.days || []);
}

function renderProducts(rows) {
  const visibleRows = filteredProducts(mappingSearch.value);
  products.innerHTML = visibleRows.map((row) => `
    <tr>
      <td>#${row.id}</td>
      <td>${escapeHtml(row.marketplace)}</td>
      <td>${escapeHtml(row.external_product_id)}<br><small>${escapeHtml(row.title || "")}</small></td>
      <td>${row.external_variant_id ? `<code>${escapeHtml(row.external_variant_id)}</code>` : "<span class=\"muted\">общий</span>"}</td>
      <td>
        <code>${escapeHtml(row.tariff_code || "-")}</code><br>
        <small>${escapeHtml(row.action || "create")}</small>
      </td>
      <td><button class="secondary" data-toggle="${row.id}" data-enabled="${row.enabled ? "0" : "1"}">
        <span class="pill ${row.enabled ? "" : "off"}">${row.enabled ? "включен" : "выключен"}</span>
      </button></td>
      <td>
        <div class="row-actions">
          <button class="secondary" data-product-edit="${row.id}" type="button">Редактировать</button>
          <button class="danger" data-product-delete="${row.id}" type="button">Удалить</button>
        </div>
      </td>
    </tr>
  `).join("") || `
    <tr><td colspan="7"><span class="muted">Ничего не найдено</span></td></tr>
  `;
  document.querySelectorAll("[data-toggle]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/admin/api/products/${button.dataset.toggle}/enabled`, {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled: button.dataset.enabled === "1"}),
      });
      await loadAll();
    });
  });
  document.querySelectorAll("[data-product-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      const product = productRows.find((row) => String(row.id) === String(button.dataset.productEdit));
      if (product) {
        editProductMapping(product);
      }
    });
  });
  document.querySelectorAll("[data-product-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm("Удалить этот маппинг?")) {
        return;
      }
      await api(`/admin/api/products/${button.dataset.productDelete}`, {method: "DELETE"});
      if (String(editingProductId || "") === String(button.dataset.productDelete)) {
        resetMappingForm();
      }
      await loadAll();
    });
  });
}

function flattenObject(value, prefix = "", output = {}) {
  if (Array.isArray(value)) {
    value.forEach((item, index) => flattenObject(item, `${prefix}${index}.`, output));
    return output;
  }
  if (value && typeof value === "object") {
    Object.entries(value).forEach(([key, item]) => {
      const nextKey = prefix ? `${prefix}${key}` : key;
      if (item && typeof item === "object") {
        flattenObject(item, `${nextKey}.`, output);
      } else if (item !== null && item !== undefined && String(item).trim()) {
        output[nextKey] = String(item).trim();
        output[key] = String(item).trim();
      }
    });
  }
  return output;
}

function collectKeyValues(source) {
  const text = String(source || "").trim();
  const values = {};
  if (!text) {
    return values;
  }

  try {
    Object.assign(values, flattenObject(JSON.parse(text)));
  } catch (_) {
    // Plain text, URL, or query string.
  }

  const queryLike = text.match(/[?&]?[A-Za-z0-9_.-]+=[^&\s]+/g) || [];
  queryLike.forEach((part) => {
    const clean = part.replace(/^[?&]/, "");
    const eq = clean.indexOf("=");
    if (eq > 0) {
      const key = clean.slice(0, eq);
      const value = clean.slice(eq + 1);
      try {
        values[key] = decodeURIComponent(value.replaceAll("+", " "));
      } catch (_) {
        values[key] = value;
      }
    }
  });

  return values;
}

function collectVariantRows(value, output = []) {
  if (Array.isArray(value)) {
    value.forEach((item) => collectVariantRows(item, output));
    return output;
  }
  if (!value || typeof value !== "object") {
    return output;
  }

  const id = value.variant_id ?? value.variantId ?? value.option_id ?? value.optionId ?? value.button_id ?? value.buttonId ?? value.id ?? value.value ?? value.sku;
  if (id !== null && id !== undefined && String(id).trim()) {
    const label = value.name ?? value.title ?? value.label ?? value.text ?? value.caption ?? value.value_text ?? value.valueText ?? "";
    output.push({
      id: String(id).trim(),
      label: String(label || id).trim(),
    });
  }

  Object.entries(value).forEach(([key, item]) => {
    if (["options", "variants", "buttons", "items", "values"].includes(key) || Array.isArray(item)) {
      collectVariantRows(item, output);
    }
  });
  return output;
}

function pickValue(values, keys) {
  for (const key of keys) {
    if (values[key] && String(values[key]).trim()) {
      return String(values[key]).trim();
    }
  }
  const normalized = Object.entries(values).map(([key, value]) => [key.toLowerCase(), value]);
  for (const key of keys.map((item) => item.toLowerCase())) {
    const found = normalized.find(([candidate]) => candidate.endsWith(key) || candidate === key);
    if (found && String(found[1]).trim()) {
      return String(found[1]).trim();
    }
  }
  return "";
}

function detectMarketplace(text) {
  const source = String(text || "").toLowerCase();
  if (source.includes("ggsel")) {
    return "ggsel";
  }
  if (source.includes("digiseller")) {
    return "digiseller";
  }
  if (source.includes("plati")) {
    return "plati";
  }
  return "";
}

function parseMappingSource(text) {
  const source = String(text || "").trim();
  const values = collectKeyValues(source);
  let parsedJson = null;
  let variants = [];
  try {
    parsedJson = JSON.parse(source);
    variants = collectVariantRows(parsedJson);
  } catch (_) {
    variants = [];
  }
  let marketplace = detectMarketplace(source);
  let productId = pickValue(values, [
    "external_product_id",
    "id_goods",
    "goods_id",
    "product_id",
    "productId",
    "item_id",
    "itemId",
    "offer_id",
    "offerId",
    "lot_id",
    "lotId",
  ]);
  let variantId = pickValue(values, [
    "external_variant_id",
    "variant_id",
    "variantId",
    "option_id",
    "optionId",
    "button_id",
    "buttonId",
    "selection_id",
    "selectionId",
    "sku",
  ]);

  try {
    const url = new URL(source);
    marketplace ||= detectMarketplace(url.hostname);
    url.searchParams.forEach((value, key) => {
      values[key] = value;
    });
    productId ||= pickValue(values, [
      "id_goods",
      "goods_id",
      "product_id",
      "offer_id",
      "item_id",
      "lot_id",
    ]);
    if (!productId) {
      const pathNumbers = url.pathname.match(/\d{4,}/g) || [];
      productId = pathNumbers[pathNumbers.length - 1] || "";
    }
  } catch (_) {
    if (!productId) {
      const labeled = source.match(/(?:id_goods|goods_id|product_id|offer_id|lot_id|лот|товар)\D{0,12}([A-Za-z0-9_-]{3,})/i);
      productId = labeled ? labeled[1] : "";
    }
  }

  if (variantId && !variants.some((item) => item.id === variantId)) {
    variants.unshift({id: variantId, label: variantId});
  }

  const uniqueVariants = [];
  const seenVariants = new Set();
  variants.forEach((item) => {
    if (!seenVariants.has(item.id)) {
      uniqueVariants.push(item);
      seenVariants.add(item.id);
    }
  });

  const hasExplicitTopLevelVariant = Boolean(
    parsedJson
    && !Array.isArray(parsedJson)
    && typeof parsedJson === "object"
    && (
      parsedJson.external_variant_id
      || parsedJson.variant_id
      || parsedJson.variantId
      || parsedJson.option_id
      || parsedJson.optionId
      || parsedJson.button_id
      || parsedJson.buttonId
    )
  );
  const hasExplicitQueryVariant = /(?:^|[?&\s])(?:external_variant_id|variant_id|variantId|option_id|optionId|button_id|buttonId|sku)=/i.test(source);
  if (uniqueVariants.length > 1 && !hasExplicitTopLevelVariant && !hasExplicitQueryVariant) {
    variantId = "";
  }

  let title = "";
  if (parsedJson && !Array.isArray(parsedJson) && typeof parsedJson === "object") {
    title = parsedJson.title || parsedJson.name || parsedJson.caption || "";
  }

  return {marketplace, productId, variantId, variants: uniqueVariants, title};
}

function shouldFetchLotPage(text) {
  try {
    const url = new URL(String(text || "").trim());
    const host = url.hostname.toLowerCase();
    return ["plati.io", "plati.market", "ggsel.net", "ggsel.com", "digiseller.com"]
      .some((item) => host === item || host.endsWith(`.${item}`));
  } catch (_) {
    return false;
  }
}

async function parseMappingSourceFull(text) {
  const local = parseMappingSource(text);
  if (!shouldFetchLotPage(text)) {
    return local;
  }
  mappingParseStatus.textContent = "Загружаю страницу лота и ищу кнопки...";
  const remote = await api("/admin/api/parse-lot", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({source: text}),
  });
  return {
    marketplace: remote.marketplace || local.marketplace,
    productId: remote.productId || local.productId,
    variantId: remote.variantId || local.variantId,
    variants: Array.isArray(remote.variants) && remote.variants.length ? remote.variants : local.variants,
    title: remote.title || local.title,
  };
}

function variantLabel(row) {
  return row.label && row.label !== row.id ? `${row.label} (${row.id})` : row.id;
}

function mappedVariantIdsForCurrentLot() {
  const marketplace = productForm.elements.marketplace.value;
  const productId = productForm.elements.external_product_id.value.trim();
  if (!marketplace || !productId) {
    return new Set();
  }
  return new Set(productRows
    .filter((row) => (
      row.marketplace === marketplace
      && row.external_product_id === productId
      && row.external_variant_id
      && String(row.id) !== String(editingProductId || "")
    ))
    .map((row) => String(row.external_variant_id)));
}

function availableVariantRows(value = "") {
  const query = String(value || "").trim().toLowerCase();
  const mapped = mappedVariantIdsForCurrentLot();
  return variantRows.filter((row) => {
    if (mapped.has(String(row.id))) {
      return false;
    }
    const haystack = `${row.id} ${row.label}`.toLowerCase();
    return !query || haystack.includes(query);
  });
}

function renderVariantDropdown(value = "", open = true) {
  const rows = availableVariantRows(value);
  if (!rows.length) {
    variantDropdown.innerHTML = `<div class="variant-empty">${variantRows.length ? "Все кнопки этого лота уже добавлены" : "Кнопки не найдены"}</div>`;
  } else {
    variantDropdown.innerHTML = rows.map((row) => `
      <button class="variant-option" type="button" data-variant="${escapeHtml(row.id)}">
        <span>${escapeHtml(variantLabel(row))}</span>
      </button>
    `).join("");
  }
  variantDropdown.classList.toggle("hidden", !open);
}

function setVariantRows(rows) {
  variantRows = Array.isArray(rows) ? rows : [];
  if (!variantRows.length) {
    variantDropdown.classList.add("hidden");
    variantHint.textContent = "Если у лота есть кнопки, они появятся после парсинга";
    return;
  }
  const availableCount = availableVariantRows("").length;
  variantHint.textContent = `Доступно кнопок: ${availableCount} из ${variantRows.length}`;
  renderVariantDropdown("", false);
}

function refreshVariantAvailability() {
  if (!variantRows.length) {
    return;
  }
  const availableCount = availableVariantRows("").length;
  variantHint.textContent = `Доступно кнопок: ${availableCount} из ${variantRows.length}`;
  renderVariantDropdown(variantSearch.value, false);
}

function selectVariant(id) {
  const selected = variantRows.find((row) => row.id === id);
  variantSearch.value = selected ? selected.id : id;
  variantDropdown.classList.add("hidden");
  variantHint.textContent = selected ? `Выбрано: ${variantLabel(selected)}` : `Выбрано: ${id}`;
}

function setMappingEditMode(productId) {
  editingProductId = productId ? Number(productId) : null;
  const saveButton = productForm.querySelector(".mapping-form-actions button[type=\"submit\"]");
  if (saveButton) {
    saveButton.textContent = editingProductId ? "Сохранить правки" : "Сохранить";
  }
}

function editProductMapping(row) {
  setMappingEditMode(row.id);
  productForm.elements.marketplace.value = row.marketplace;
  productForm.elements.external_product_id.value = row.external_product_id;
  productForm.elements.external_variant_id.value = row.external_variant_id || "";
  productForm.elements.action.value = row.action || "create";
  fillActionParamControls(row.action_params);
  productForm.elements.title.value = row.title || "";
  productForm.elements.delivery_template.value = row.delivery_template || "";
  const selectedTariff = findTariff(row.tariff_code);
  tariffCode.value = row.tariff_code;
  tariffSearch.value = selectedTariff ? tariffLabel(selectedTariff) : row.tariff_code;
  if (row.external_variant_id && !variantRows.some((item) => item.id === row.external_variant_id)) {
    variantRows.unshift({id: row.external_variant_id, label: row.external_variant_id});
  }
  updateTariffHint();
  renderVariantDropdown("", false);
  variantHint.textContent = row.external_variant_id
    ? `Редактируется: ${row.external_variant_id}`
    : "Редактируется общий маппинг";
  mappingParseStatus.textContent = `Редактирование маппинга #${row.id}`;
  productForm.scrollIntoView({behavior: "smooth", block: "start"});
}

function applyParsedMapping(parsed, options = {}) {
  const marketplaceSelect = productForm.elements.marketplace;
  const productInput = productForm.elements.external_product_id;
  const variantInput = productForm.elements.external_variant_id;
  const titleInput = productForm.elements.title;
  const applied = [];

  if (parsed.marketplace && marketplaceSelect.querySelector(`option[value="${parsed.marketplace}"]`)) {
    marketplaceSelect.value = parsed.marketplace;
    applied.push("площадка");
  }
  if (parsed.productId && (!productInput.value || options.force)) {
    productInput.value = parsed.productId;
    applied.push("ID лота");
  }
  if (parsed.variantId && (!variantInput.value || options.force)) {
    variantInput.value = parsed.variantId;
    applied.push("вариант");
  }
  if (options.force && !parsed.variantId) {
    variantInput.value = "";
  }
  if (parsed.title && titleInput && (!titleInput.value || options.force)) {
    titleInput.value = parsed.title;
    applied.push("название");
  }
  setVariantRows(parsed.variants || []);
  if (!parsed.variantId && variantRows.length === 1 && (!variantInput.value || options.force)) {
    selectVariant(variantRows[0].id);
    applied.push("вариант");
  } else if (variantRows.length > 1) {
    renderVariantDropdown("", true);
  }

  mappingParseStatus.textContent = applied.length
    ? `Заполнено: ${applied.join(", ")}`
    : "Не нашёл ID лота или вариант в этом тексте";
  return applied.length > 0;
}

function normalizeTariffs(payload) {
  if (Array.isArray(payload)) {
    return payload;
  }
  if (Array.isArray(payload?.value)) {
    return payload.value;
  }
  return [];
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) {
    return "";
  }
  const gb = value / 1024 / 1024 / 1024;
  return `${gb >= 1 ? gb.toFixed(gb >= 10 ? 0 : 1) : "<1"} ГБ`;
}

function tariffLabel(row) {
  const code = row.code || "";
  const family = (row.family_code || code.split("_")[0] || "tariff").toUpperCase();
  const period = row.duration_days ? `${row.duration_days} дн` : (row.period_key ? `${row.period_key}` : "");
  const ip = row.ip_limit ? `${row.ip_limit} IP` : "";
  const traffic = row.is_unlimited_traffic ? "безлимит" : formatBytes(row.included_traffic_bytes);
  const price = row.api_price_rub ? `${row.api_price_rub} ₽` : "";
  return [family, period, ip, traffic, price].filter(Boolean).join(" • ") + ` (${code})`;
}

function renderTariffOptions(rows) {
  tariffRows = normalizeTariffs(rows).filter((row) => row && row.code);
  const current = tariffCode.value || tariffSearch.value;
  if (!tariffRows.length) {
    tariffDropdown.innerHTML = "";
    tariffDropdown.classList.add("hidden");
    tariffHint.textContent = "Проверьте XyraNet API key и обновите страницу";
    return;
  }
  const selected = findTariff(current);
  if (selected) {
    tariffCode.value = selected.code;
    tariffSearch.value = tariffLabel(selected);
  }
  updateTariffHint();
  renderTariffDropdown(tariffSearch.value, false);
}

function findTariff(value) {
  const query = String(value || "").trim().toLowerCase();
  if (!query) {
    return null;
  }
  let found = tariffRows.find((row) => row.code.toLowerCase() === query);
  if (found) {
    return found;
  }
  found = tariffRows.find((row) => tariffLabel(row).toLowerCase() === query);
  if (found) {
    return found;
  }
  const matches = tariffRows.filter((row) => {
    const haystack = `${row.code} ${tariffLabel(row)}`.toLowerCase();
    return haystack.includes(query);
  });
  return matches.length === 1 ? matches[0] : null;
}

function resolveTariffCode() {
  const value = tariffSearch.value.trim();
  const selected = findTariff(value);
  if (selected) {
    tariffCode.value = selected.code;
    tariffSearch.value = tariffLabel(selected);
    updateTariffHint();
    return selected.code;
  }
  if (!tariffRows.length && value) {
    tariffCode.value = value;
    return value;
  }
  tariffCode.value = "";
  return "";
}

function matchingTariffs(value) {
  const query = String(value || "").trim().toLowerCase();
  if (!query) {
    return tariffRows;
  }
  return tariffRows.filter((row) => {
    const haystack = `${row.code} ${tariffLabel(row)}`.toLowerCase();
    return haystack.includes(query);
  });
}

function renderTariffDropdown(value = "", open = true) {
  const rows = matchingTariffs(value).slice(0, 20);
  if (!rows.length) {
    tariffDropdown.innerHTML = `<div class="tariff-empty">Ничего не найдено</div>`;
  } else {
    tariffDropdown.innerHTML = rows.map((row) => `
      <button class="tariff-option" type="button" data-code="${escapeHtml(row.code)}">
        <span>${escapeHtml(tariffLabel(row))}</span>
      </button>
    `).join("");
  }
  tariffDropdown.classList.toggle("hidden", !open);
}

function selectTariff(code) {
  const selected = tariffRows.find((row) => row.code === code);
  if (!selected) {
    return;
  }
  tariffCode.value = selected.code;
  tariffSearch.value = tariffLabel(selected);
  tariffDropdown.classList.add("hidden");
  updateTariffHint();
  const titleInput = productForm.elements.title;
  if (titleInput && !titleInput.value.trim()) {
    titleInput.value = `${(selected.family_code || selected.code).toUpperCase()} ${selected.duration_days || ""} дн`.trim();
  }
}

function updateTariffHint() {
  const selected = findTariff(tariffCode.value || tariffSearch.value);
  if (selected) {
    tariffCode.value = selected.code;
    tariffHint.textContent = `Будет сохранён код: ${selected.code}`;
    return;
  }
  tariffHint.textContent = tariffSearch.value.trim()
    ? "Продолжайте ввод или выберите точный вариант из списка"
    : "Начните вводить название, срок, цену или код тарифа";
}

function resetMappingForm() {
  setMappingEditMode(null);
  productForm.reset();
  tariffCode.value = "";
  variantRows = [];
  tariffDropdown.classList.add("hidden");
  variantDropdown.classList.add("hidden");
  mappingParseStatus.textContent = "Поля лота и варианта можно заполнить автоматически";
  variantHint.textContent = "Если у лота есть кнопки, они появятся после парсинга";
  updateTariffHint();
  updateActionParamsVisibility({reset: true});
}

function mappingPayloadFromForm() {
  const form = new FormData(productForm);
  const payload = Object.fromEntries(form.entries());
  const params = buildActionParamsFromControls();
  payload.action_params_text = Object.keys(params).length ? JSON.stringify(params) : "";
  const paramsText = String(payload.action_params_text || "").trim();
  delete payload.action_params_text;
  if (paramsText) {
    try {
      payload.action_params = JSON.parse(paramsText);
    } catch (_) {
      throw new Error("Не смог собрать параметры действия. Проверьте введённые числа.");
    }
  } else {
    payload.action_params = {};
  }
  return payload;
}

function renderSettings(rows) {
  settingsForm.innerHTML = rows.map((row) => {
    const restart = row.restart_required ? "<small>нужен рестарт бота</small>" : "";
    if (row.kind === "boolean") {
      const checked = row.value === "true" || row.value === true;
      return `
        <label class="check-row">
          <input name="${escapeHtml(row.key)}" type="checkbox" ${checked ? "checked" : ""}>
          <span>${escapeHtml(row.label)} ${restart}</span>
        </label>
      `;
    }
    const type = row.sensitive ? "password" : "text";
    const placeholder = row.sensitive && row.configured ? "задано, оставьте пустым чтобы не менять" : "";
    return `
      <label>${escapeHtml(row.label)} ${restart}
        <input name="${escapeHtml(row.key)}" type="${type}" value="${escapeHtml(row.value)}" placeholder="${escapeHtml(placeholder)}">
      </label>
    `;
  }).join("") + `<button type="submit">Сохранить настройки</button>`;
}

function renderBotUsers(rows) {
  botUsers.innerHTML = rows.map((row) => {
    const action = row.locked
      ? "<span class=\"muted\">защищён</span>"
      : `<button class="danger" data-user-delete="${row.telegram_id}" type="button">Удалить</button>`;
    return `
      <tr>
        <td>${escapeHtml(row.telegram_id)}</td>
        <td>${escapeHtml(row.label || "")}</td>
        <td><span class="pill ${row.source === "env" ? "" : "neutral"}">${row.source === "env" ? "env" : "панель"}</span></td>
        <td>${action}</td>
      </tr>
    `;
  }).join("");
  document.querySelectorAll("[data-user-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/admin/api/bot-users/${button.dataset.userDelete}`, {method: "DELETE"});
      await loadAll();
    });
  });
}

function renderSales(rows) {
  sales.innerHTML = rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.marketplace)}</td>
      <td>${escapeHtml(row.external_order_id)}</td>
      <td>${escapeHtml(row.external_product_id)}</td>
      <td>${row.external_variant_id ? escapeHtml(row.external_variant_id) : "<span class=\"muted\">общий</span>"}</td>
      <td>${escapeHtml(row.xyranet_order_id || "ожидает")}</td>
      <td>${escapeHtml(row.created_at)}</td>
    </tr>
  `).join("");
}

async function loadAll() {
  if (!adminToken) {
    showLogin();
    return;
  }
  try {
    const selectedPeriod = statisticsPeriod?.value || "30d";
    const [status, productsRows, salesRows, settingsRows, botUserRows, tariffsRows, templateConfig, complexVariableConfig, statisticsConfig] = await Promise.all([
      api("/admin/api/status"),
      api("/admin/api/products"),
      api("/admin/api/sales"),
      api("/admin/api/settings"),
      api("/admin/api/bot-users"),
      api("/admin/api/tariffs").catch(() => []),
      api("/admin/api/delivery-template"),
      api("/admin/api/complex-variables"),
      api(`/admin/api/statistics?period=${encodeURIComponent(selectedPeriod)}`),
    ]);
    let summary = null;
    try {
      summary = await api("/admin/api/summary");
    } catch (_) {
      summary = null;
    }
    productRows = Array.isArray(productsRows) ? productsRows : [];
    renderMetrics(status, summary);
    renderProducts();
    renderSales(salesRows);
    renderSettings(settingsRows);
    renderBotUsers(botUserRows);
    renderTariffOptions(tariffsRows);
    renderTemplateVariables(templateConfig);
    renderComplexVariables(complexVariableConfig);
    renderStatistics(statisticsConfig);
    hideLogin();
  } catch (error) {
    showLogin();
    loginError.textContent = "Сессия не активна или логин/пароль неверные.";
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.textContent = "";
  const form = new FormData(loginForm);
  try {
    const response = await fetch("/admin/api/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.fromEntries(form.entries())),
    });
    if (!response.ok) {
      throw new Error("login failed");
    }
    const data = await response.json();
    adminToken = data.token;
    localStorage.setItem("reseller_admin_token", adminToken);
    loginForm.reset();
    await loadAll();
  } catch (_) {
    loginError.textContent = "Не получилось войти. Проверь логин и пароль.";
  }
});

logoutButton.addEventListener("click", logout);
refreshButton.addEventListener("click", loadAll);
resetMappingButton.addEventListener("click", resetMappingForm);

sectionTabs.forEach((button) => {
  button.addEventListener("click", () => setActiveSection(button.dataset.sectionTab));
});

mappingSearch.addEventListener("input", () => renderProducts());

statisticsPeriod.addEventListener("change", async () => {
  try {
    renderStatistics(await api(`/admin/api/statistics?period=${encodeURIComponent(statisticsPeriod.value)}`));
  } catch (error) {
    alert(error.message || error);
  }
});

templateActionSelect.addEventListener("change", () => {
  renderTemplateStageOptions();
  syncTemplateEditor();
});
templateStageSelect.addEventListener("change", syncTemplateEditor);

parseMappingButton.addEventListener("click", async () => {
  parseMappingButton.disabled = true;
  try {
    applyParsedMapping(parseMappingSource(mappingSource.value), {force: true});
    const parsed = await parseMappingSourceFull(mappingSource.value);
    applyParsedMapping(parsed, {force: true});
    if (variantRows.length > 1) {
      variantSearch.focus();
      renderVariantDropdown("", true);
    }
  } catch (error) {
    mappingParseStatus.textContent = `Не смог загрузить кнопки: ${error.message || error}`;
  } finally {
    parseMappingButton.disabled = false;
  }
});

mappingSource.addEventListener("paste", () => {
  setTimeout(async () => {
    try {
      applyParsedMapping(parseMappingSource(mappingSource.value), {force: true});
      const parsed = await parseMappingSourceFull(mappingSource.value);
      applyParsedMapping(parsed, {force: true});
      if (variantRows.length > 1) {
        variantSearch.focus();
        renderVariantDropdown("", true);
      }
    } catch (error) {
      mappingParseStatus.textContent = `Не смог загрузить кнопки: ${error.message || error}`;
    }
  }, 0);
});

["external_product_id", "external_variant_id"].forEach((name) => {
  const input = productForm.elements[name];
  input.addEventListener("paste", (event) => {
    const text = event.clipboardData?.getData("text") || "";
    const parsed = parseMappingSource(text);
    if (!parsed.productId && !parsed.variantId && !parsed.marketplace) {
      return;
    }
    event.preventDefault();
    mappingSource.value = text;
    applyParsedMapping(parsed, {force: name === "external_product_id"});
  });
});

productForm.elements.marketplace.addEventListener("change", refreshVariantAvailability);
productForm.elements.external_product_id.addEventListener("input", refreshVariantAvailability);
productForm.elements.action.addEventListener("change", () => updateActionParamsVisibility({reset: true}));
actionParamFullPeriod.addEventListener("change", () => updateActionParamsVisibility());

variantSearch.addEventListener("input", () => {
  if (variantRows.length) {
    renderVariantDropdown(variantSearch.value, true);
  }
});

variantSearch.addEventListener("focus", () => {
  if (variantRows.length) {
    renderVariantDropdown(variantSearch.value, true);
  }
});

variantDropdown.addEventListener("mousedown", (event) => {
  const button = event.target.closest("[data-variant]");
  if (!button) {
    return;
  }
  event.preventDefault();
  selectVariant(button.dataset.variant);
});

defaultTemplateButton.addEventListener("click", () => {
  const action = selectedTemplateAction();
  deliveryTemplate.value = action?.default_template || defaultDeliveryTemplate;
  deliveryTemplate.focus();
});

clearTemplateButton.addEventListener("click", () => {
  deliveryTemplate.value = "";
  deliveryTemplate.focus();
});

saveTemplateButton.addEventListener("click", async () => {
  const action = selectedTemplateAction();
  const group = selectedTemplateGroup();
  if (!action) {
    return;
  }
  saveTemplateButton.disabled = true;
  try {
    const updated = await api(`/admin/api/delivery-template/${action.key}`, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({template: deliveryTemplate.value.trim()}),
    });
    if (group?.command_action && templateCommandInput.value.trim()) {
      const command = await api(`/admin/api/chat-command/${group.command_action}`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({command: templateCommandInput.value.trim()}),
      });
      group.command = command.command;
    }
    const stageIndex = (group?.stages || []).findIndex((item) => item.key === updated.key);
    if (stageIndex >= 0) {
      group.stages[stageIndex] = {...group.stages[stageIndex], ...updated};
    }
    renderTemplateStageOptions();
    templateStageSelect.value = updated.key;
    syncTemplateEditor();
  } catch (error) {
    alert(error.message || error);
  } finally {
    saveTemplateButton.disabled = false;
  }
});

templateVariableButtons.addEventListener("click", (event) => {
  const button = event.target.closest("[data-template-token]");
  if (!button) {
    return;
  }
  insertAtCursor(deliveryTemplate, button.dataset.templateToken);
});

complexVariableSelect.addEventListener("change", () => {
  editingComplexVariableKey = complexVariableSelect.value;
  syncComplexVariableEditor();
});

newComplexVariableButton.addEventListener("click", startNewComplexVariable);

defaultComplexVariableButton.addEventListener("click", () => {
  const variable = selectedComplexVariable();
  complexVariableTemplate.value = variable?.default_template || "";
  complexVariableTemplate.focus();
});

clearComplexVariableButton.addEventListener("click", () => {
  complexVariableTemplate.value = "";
  complexVariableTemplate.focus();
});

complexVariableButtons.addEventListener("click", (event) => {
  const button = event.target.closest("[data-complex-token]");
  if (!button) {
    return;
  }
  insertAtCursor(complexVariableTemplate, button.dataset.complexToken);
});

saveComplexVariableButton.addEventListener("click", async () => {
  const key = complexVariableKey.value.trim();
  if (!key) {
    alert("Укажите имя переменной.");
    complexVariableKey.focus();
    return;
  }
  saveComplexVariableButton.disabled = true;
  try {
    const saved = await api(`/admin/api/complex-variables/${encodeURIComponent(editingComplexVariableKey || key)}`, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        key,
        label: complexVariableLabel.value.trim(),
        template: complexVariableTemplate.value.trim(),
      }),
    });
    editingComplexVariableKey = saved.key;
    await loadAll();
  } catch (error) {
    alert(error.message || error);
  } finally {
    saveComplexVariableButton.disabled = false;
  }
});

deleteComplexVariableButton.addEventListener("click", async () => {
  const variable = selectedComplexVariable();
  if (!variable || variable.builtin) {
    return;
  }
  if (!confirm(`Удалить переменную ${variable.token}?`)) {
    return;
  }
  await api(`/admin/api/complex-variables/${encodeURIComponent(variable.key)}`, {method: "DELETE"});
  editingComplexVariableKey = "";
  await loadAll();
});

productForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = productForm.elements.action.value;
  const needsTariff = ["create", "renew"].includes(action);
  if (needsTariff && !resolveTariffCode()) {
    alert("Выберите тариф из подсказок или введите точный код тарифа.");
    return;
  }
  if (!needsTariff && !tariffCode.value) {
    tariffCode.value = "";
  }
  let payload;
  try {
    payload = mappingPayloadFromForm();
  } catch (error) {
    alert(error.message || error);
    return;
  }
  await api(editingProductId ? `/admin/api/products/${editingProductId}` : "/admin/api/products", {
    method: editingProductId ? "PUT" : "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const wasEditing = Boolean(editingProductId);
  if (wasEditing) {
    setMappingEditMode(null);
  }
  mappingParseStatus.textContent = wasEditing
    ? "Правки сохранены. Можно выбрать следующую кнопку этого лота."
    : "Маппинг сохранён. Можно выбрать следующую кнопку этого лота.";
  await loadAll();
  if (variantRows.length > 1) {
    variantHint.textContent = `Доступно кнопок: ${availableVariantRows("").length} из ${variantRows.length}`;
    variantSearch.focus();
    renderVariantDropdown("", true);
  }
});

tariffSearch.addEventListener("input", () => {
  const selected = findTariff(tariffSearch.value);
  tariffCode.value = selected ? selected.code : "";
  renderTariffDropdown(tariffSearch.value, true);
  updateTariffHint();
});

tariffSearch.addEventListener("focus", () => {
  renderTariffDropdown(tariffSearch.value, true);
});

tariffSearch.addEventListener("change", () => {
  const selectedCode = resolveTariffCode();
  const titleInput = productForm.elements.title;
  if (!titleInput || titleInput.value.trim()) {
    return;
  }
  const selected = tariffRows.find((row) => row.code === selectedCode);
  if (selected) {
    titleInput.value = `${(selected.family_code || selected.code).toUpperCase()} ${selected.duration_days || ""} дн`.trim();
  }
});

tariffDropdown.addEventListener("mousedown", (event) => {
  const button = event.target.closest("[data-code]");
  if (!button) {
    return;
  }
  event.preventDefault();
  selectTariff(button.dataset.code);
});

document.addEventListener("mousedown", (event) => {
  if (!event.target.closest(".tariff-field")) {
    tariffDropdown.classList.add("hidden");
  }
  if (!event.target.closest(".variant-field")) {
    variantDropdown.classList.add("hidden");
  }
});

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const settings = {};
  for (const field of settingsForm.elements) {
    if (!field.name) {
      continue;
    }
    if (field.type === "checkbox") {
      settings[field.name] = field.checked;
      continue;
    }
    if (field.type === "password" && !field.value.trim()) {
      continue;
    }
    settings[field.name] = field.value;
  }
  await api("/admin/api/settings", {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({settings}),
  });
  await loadAll();
});

restartTelegramButton.addEventListener("click", async () => {
  restartTelegramButton.disabled = true;
  telegramRestartStatus.textContent = "Перезапускаю Telegram-бота...";
  try {
    const result = await api("/admin/api/telegram/restart", {method: "POST"});
    const telegram = result.telegram || {};
    telegramRestartStatus.textContent = telegram.running
      ? "Telegram-бот запущен с актуальными настройками."
      : `Telegram-бот не запущен: ${telegram.reason || "проверьте токен, доступы и включение Telegram"}.`;
    await loadAll();
  } catch (error) {
    telegramRestartStatus.textContent = `Не удалось перезапустить Telegram-бота: ${error.message}`;
  } finally {
    restartTelegramButton.disabled = false;
  }
});

botUserForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(botUserForm);
  const payload = Object.fromEntries(form.entries());
  payload.telegram_id = Number(payload.telegram_id);
  await api("/admin/api/bot-users", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  botUserForm.reset();
  await loadAll();
});

loadAll();
