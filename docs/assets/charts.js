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

  function chartDomId(chart) {
    return "chart-" + [chart.family_key, chart.benchmark_key, chart.metric_key]
      .join("-")
      .replace(/[^A-Za-z0-9_-]+/g, "-");
  }

  function renderBars(chart) {
    var key = primarySeries(chart);
    var rows = chart.rows;
    var max = Math.max.apply(null, rows.map(function (row) { return Number(row[key]) || 0; })) || 1;
    var bars = rows.map(function (row, i) {
      var value = Number(row[key]) || 0;
      var width = 28;
      var gap = 12;
      var height = Math.max(2, Math.round((value / max) * 120));
      var x = 46 + i * (width + gap);
      var y = 150 - height;
      return `<rect x="${x}" y="${y}" width="${width}" height="${height}"><title>${esc(effortLabel(row.effort) + " " + formatNumber(value))}</title></rect>`;
    }).join("");
    var w = Math.max(340, 84 + rows.length * 40);
    var xLabel = esc(t("chart_explainer.x_axis_label"));
    var yLabel = esc(valueLabel(key));
    var axisLabels = `<text class="chart-axis-label chart-x-axis" x="${w / 2}" y="184" text-anchor="middle">${xLabel}</text>` +
      `<text class="chart-axis-label chart-y-axis" x="14" y="92" text-anchor="middle" transform="rotate(-90 14 92)">${yLabel}</text>`;
    return `<svg class="chart-svg" role="img" aria-label="${esc(metricTitle(chart))}" viewBox="0 0 ${w} 190">${axisLabels}${bars}</svg>`;
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
      document.querySelectorAll("[data-i18n]").forEach(function (node) {
        node.textContent = t(node.getAttribute("data-i18n"));
      });
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
