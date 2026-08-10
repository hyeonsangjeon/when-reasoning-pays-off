(function () {
  "use strict";

  var SUPPORTED = ["en", "ko", "ja", "zh-CN", "hi"];
  var DATA_ROOT = "../data/chart-data/";
  var root;
  var labels = {};
  var baseLabels = {};

  function get(obj, path) {
    return path.split(".").reduce(function (cur, part) {
      return cur && Object.prototype.hasOwnProperty.call(cur, part) ? cur[part] : undefined;
    }, obj);
  }

  function labelValue(path) {
    var value = get(labels, path);
    if (value === undefined && labels !== baseLabels) value = get(baseLabels, path);
    return value;
  }

  function t(path) {
    var value = labelValue(path);
    if (typeof value === "string") return value;
    return path.split(".").pop();
  }

  function localeFromUrl() {
    var params = new URLSearchParams(window.location.search);
    var lang = params.get("lang") || "en";
    return SUPPORTED.indexOf(lang) === -1 ? "en" : lang;
  }

  function applyNavHrefs(locale) {
    var suffix = locale === "en" ? "" : locale + "/";
    var hrefs = {
      "nav-home": "../../" + locale + "/",
      "nav-articles": "../articles/" + suffix,
      "nav-overview": "../articles/when-reasoning-pays-off/" + suffix
    };
    Object.keys(hrefs).forEach(function (cls) {
      document.querySelectorAll(".site-links ." + cls).forEach(function (node) {
        node.setAttribute("href", hrefs[cls]);
      });
    });
    document.querySelectorAll(".brand").forEach(function (node) {
      node.setAttribute("href", "../../" + locale + "/");
    });
    document.querySelectorAll(".lang [data-locale]").forEach(function (node) {
      if (node.getAttribute("data-locale") === locale) {
        node.setAttribute("aria-current", "true");
      } else {
        node.removeAttribute("aria-current");
      }
    });
  }

  function fetchJson(path) {
    return fetch(path, { credentials: "same-origin" }).then(function (res) {
      if (!res.ok) throw new Error("fetch failed");
      return res.json();
    });
  }

  function loadLabels(locale) {
    return Promise.all([
      fetchJson(DATA_ROOT + "locales/en.json"),
      locale === "en" ? Promise.resolve(null) : fetchJson(DATA_ROOT + "locales/" + locale + ".json")
    ]).then(function (items) {
      var en = items[0];
      var local = items[1];
      baseLabels = en;
      labels = en;
      if (local && local.meta && local.meta.fallback === false) labels = local;
      if (local && local.meta && local.meta.fallback === true) labels.meta = local.meta;
      return labels;
    });
  }

  function esc(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch];
    });
  }

  function metricTitle(chart) {
    return t("metrics." + chart.metric_key);
  }

  function valueLabel(key) {
    return t("series." + key);
  }

  function effortLabel(value) {
    return t("effort." + value);
  }

  function numericSeries(chart) {
    return chart.series_keys.filter(function (key) {
      return chart.rows.some(function (row) { return typeof row[key] === "number"; });
    });
  }

  function primarySeries(chart) {
    var preferred = {
      cost_per_request: "mean_usd_per_request",
      latency: "mean_latency_ms",
      quality: "mean_judge_score",
      throughput_gain: "throughput_gain_factor",
      token_composition: "mean_reasoning_tokens",
      ptu_payg_crossover: "modeled_break_even_rpm"
    };
    return preferred[chart.metric_key] || numericSeries(chart)[0];
  }

  function formatNumber(value) {
    if (typeof value !== "number") return value;
    if (Math.abs(value) < 0.01 && value !== 0) return value.toPrecision(4);
    if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString();
    return Number(value.toFixed(4)).toString();
  }

  function shortNumber(value) {
    if (typeof value !== "number") return value;
    if (value === 0) return "0";
    if (Math.abs(value) < 0.01) return value.toPrecision(3);
    if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString();
    return Number(value.toFixed(3)).toString();
  }

  function rowLabel(row) {
    if (row.effort !== undefined) return effortLabel(row.effort);
    return Object.keys(row).slice(0, 2).map(function (key) { return row[key]; }).join(" / ");
  }

  function chartDomId(chart) {
    return "chart-" + [chart.family_key, chart.benchmark_key, chart.metric_key]
      .join("-")
      .replace(/[^A-Za-z0-9_-]+/g, "-");
  }

  function renderBars(chart) {
    var key = primarySeries(chart);
    var rows = chart.rows;
    var max = Math.max.apply(null, rows.map(function (row) { return Number(row[key]) || 0; })) || 1;
    var left = 68;
    var right = 28;
    var top = 46;
    var bottom = 182;
    var barWidth = rows.length > 7 ? 28 : 38;
    var gap = rows.length > 7 ? 14 : 28;
    var plotHeight = bottom - top;
    var plotWidth = rows.length * barWidth + Math.max(0, rows.length - 1) * gap;
    var w = Math.max(430, left + plotWidth + right);
    var bars = rows.map(function (row, i) {
      var value = Number(row[key]) || 0;
      var height = Math.max(2, Math.round((value / max) * plotHeight));
      var x = left + i * (barWidth + gap);
      var y = bottom - height;
      var label = rowLabel(row);
      var tooltip = label + " · " + valueLabel(key) + ": " + formatNumber(value);
      if (row.model) tooltip += " · " + row.model;
      return `<g class="chart-bar" data-effort="${esc(row.effort || "")}">` +
        `<rect x="${x}" y="${y}" width="${barWidth}" height="${height}"><title>${esc(tooltip)}</title></rect>` +
        `<text class="chart-value-label" x="${x + barWidth / 2}" y="${Math.max(top + 12, y - 7)}" text-anchor="middle">${esc(shortNumber(value))}</text>` +
        `<text class="chart-category-label" x="${x + barWidth / 2}" y="${bottom + 18}" text-anchor="middle">${esc(label)}</text>` +
        `</g>`;
    }).join("");
    var title = esc(chart.benchmark_key + " · " + metricTitle(chart));
    var xLabel = esc(t("chart_explainer.x_axis_label"));
    var yLabel = esc(valueLabel(key));
    var axes = `<line class="chart-axis" x1="${left}" y1="${bottom}" x2="${w - right}" y2="${bottom}"></line>` +
      `<line class="chart-axis" x1="${left}" y1="${top}" x2="${left}" y2="${bottom}"></line>`;
    var labels = `<text class="chart-title" x="${w / 2}" y="22" text-anchor="middle">${title}</text>` +
      `<text class="chart-axis-label chart-x-axis" x="${(left + w - right) / 2}" y="228" text-anchor="middle">${xLabel}</text>` +
      `<text class="chart-axis-label chart-y-axis" x="18" y="${(top + bottom) / 2}" text-anchor="middle" transform="rotate(-90 18 ${(top + bottom) / 2})">${yLabel}</text>`;
    return `<svg class="chart-svg" role="img" aria-label="${title}" viewBox="0 0 ${w} 238">${labels}${axes}${bars}</svg>`;
  }

  function chartExplainer(chart) {
    var key = primarySeries(chart);
    var reading = labelValue("chart_reading." + chart.metric_key) || labelValue("chart_reading.default");
    if (!reading) return "";
    return `<aside class="chart-explainer" aria-label="${esc(t("chart_explainer.title"))}">` +
      `<h4>${esc(t("chart_explainer.title"))}</h4>` +
      "<dl>" +
      `<dt>${esc(t("chart_explainer.x_axis"))}</dt><dd>${esc(reading.x_axis || t("chart_explainer.default_x_axis"))}</dd>` +
      `<dt>${esc(t("chart_explainer.y_axis"))}</dt><dd>${esc((reading.y_axis || t("chart_explainer.default_y_axis")).replace("{series}", valueLabel(key)))}</dd>` +
      `<dt>${esc(t("chart_explainer.bars"))}</dt><dd>${esc(reading.bars || t("chart_explainer.default_bars"))}</dd>` +
      `<dt>${esc(t("chart_explainer.table"))}</dt><dd>${esc(reading.table || t("chart_explainer.default_table"))}</dd>` +
      "</dl></aside>";
  }

  function renderTable(chart, qualityChart) {
    var keys = chart.dimension_keys.concat(chart.series_keys);
    var head = keys.map(function (key) { return "<th>" + esc(t("dimensions." + key) || valueLabel(key)) + "</th>"; }).join("");
    var rows = chart.rows.map(function (row) {
      return "<tr>" + keys.map(function (key) {
        var value = row[key];
        if (key === "effort") value = effortLabel(value);
        return "<td>" + esc(formatNumber(value)) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    var q = "";
    if (qualityChart) {
      q = "<p class=\"chart-note quality-pairing\"><strong>" + esc(t("metrics.quality")) + ":</strong> " + esc(t("notes.quality_guardrail")) + "</p>";
    }
    return q + "<table class=\"chart-table\"><caption>" + esc(t("a11y.tableCaption") + " — " + metricTitle(chart)) + "</caption><thead><tr>" + head + "</tr></thead><tbody>" + rows + "</tbody></table>";
  }

  function renderChart(chart, qualityChart) {
    var ptuNote = chart.family_key === "ptu-payg-crossover" ? `<p class="chart-note ptu-hypothesis">${esc(t("notes.ptu_payg_modeled_hypothesis"))}</p>` : "";
    var qualityNote = (chart.metric_key === "cost_per_request" || chart.metric_key === "throughput_gain" || chart.metric_key === "ptu_payg_crossover") ? `<p class="chart-note quality-guardrail">${esc(t("notes.quality_guardrail"))}</p>` : "";
    return `<article id="${esc(chartDomId(chart))}" class="chart-card" data-family="${esc(chart.family_key)}" data-metric="${esc(chart.metric_key)}" data-quality-paired="${qualityChart ? "true" : "false"}">` +
      `<h3>${esc(chart.benchmark_key + " · " + metricTitle(chart))}</h3>` +
      ptuNote + qualityNote + chartExplainer(chart) + renderBars(chart) + renderTable(chart, qualityChart) + "</article>";
  }

  function qualityKey(path) {
    return path.replace(/\/(cost-per-request|throughput-gain|latency)\.json$/, "/quality.json").replace(/^results\/public\/chart-data\//, "");
  }

  function render(manifest, charts) {
    var byPath = {};
    charts.forEach(function (item) { byPath[item.rel] = item.chart; });
    var families = {};
    charts.forEach(function (item) {
      var family = item.chart.family_key;
      if (!families[family]) families[family] = [];
      families[family].push(item);
    });
    var fallback = labels.meta && labels.meta.fallback ? "<p class=\"banner fallback-badge\">" + esc(t("page.fallbackBadge")) + "</p>" : "";
    var html = fallback;
    Object.keys(families).sort().forEach(function (family) {
      var f = get(labels, "families." + family) || { title: family, description: "" };
      html += "<section class=\"chart-family\" data-family=\"" + esc(family) + "\"><h2>" + esc(f.title) + "</h2><p>" + esc(f.description || "") + "</p>";
      families[family].forEach(function (item) {
        var chart = item.chart;
        var qChart = null;
        if (chart.quality_pairing && chart.quality_pairing.quality_chart_data_path) {
          qChart = byPath[chart.quality_pairing.quality_chart_data_path.replace(/^results\/public\/chart-data\//, "")];
        } else if (chart.metric_key === "cost_per_request" || chart.metric_key === "throughput_gain") {
          qChart = byPath[qualityKey(item.rel)];
        }
        html += renderChart(chart, qChart);
      });
      html += "</section>";
    });
    root.innerHTML = html;
  }

  function start() {
    root = document.getElementById("chart-root");
    var locale = localeFromUrl();
    loadLabels(locale).then(function () {
      document.documentElement.lang = locale;
      document.title = t("page.title") + " | when reasoning pays off";
      document.querySelectorAll("[data-i18n]").forEach(function (node) {
        node.textContent = t(node.getAttribute("data-i18n"));
      });
      var siteNavLabel = labelValue("a11y.siteNav");
      if (typeof siteNavLabel === "string") {
        document.querySelectorAll("nav.site-links").forEach(function (node) {
          node.setAttribute("aria-label", siteNavLabel);
        });
      }
      var languageNavLabel = labelValue("a11y.languageNav");
      if (typeof languageNavLabel === "string") {
        var languageNav = document.querySelector("nav.lang");
        if (languageNav) languageNav.setAttribute("aria-label", languageNavLabel);
      }
      var footer = labelValue("page.footer");
      if (typeof footer === "string") {
        var footerCopy = document.querySelector("footer.site > p");
        if (footerCopy) footerCopy.textContent = footer;
      }
      applyNavHrefs(locale);
      return fetchJson(DATA_ROOT + "public_chart_candidates.json");
    }).then(function (manifest) {
      return Promise.all(manifest.candidates.map(function (candidate) {
        var rel = candidate.chart_data_path.replace(/^results\/public\/chart-data\//, "");
        return fetchJson(DATA_ROOT + rel).then(function (chart) { return { rel: rel, chart: chart }; });
      })).then(function (charts) { render(manifest, charts); });
    }).catch(function () {
      root.innerHTML = "<p class=\"banner\">" + esc(t("page.error")) + "</p>";
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
