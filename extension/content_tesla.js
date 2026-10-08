// Content script injected on Tesla Service Manual pages

(function () {
  // Prevent duplicate injection
  if (window.__ISM_TESLA_INJECTED__) return;
  window.__ISM_TESLA_INJECTED__ = true;

  function extractProcedureData() {
    let title = "";
    let correctionCode = "";
    let frt = 0.0;
    let description = "";

    // 1. Title
    const titleEl = document.querySelector(".procedure-title") ||
                    document.querySelector("h1.title") ||
                    document.querySelector("article > h1") ||
                    document.querySelector("h1");
    if (titleEl) {
      title = titleEl.textContent.trim().replace(/\s+/g, " ");
    }
    if (!title) {
      title = document.title.replace(/\s*\|\s*Tesla.*$/i, "").trim();
    }

    // 2. Correction Code
    const ccEl = document.querySelector(".correction-code");
    if (ccEl) {
      correctionCode = ccEl.textContent.trim();
    } else {
      const match = document.body.innerText.match(/Correction code\s*([0-9A-Z]{6,10})/i);
      if (match) correctionCode = match[1].trim();
    }

    // 3. FRT
    const frtEl = document.querySelector(".frt");
    if (frtEl) {
      frt = parseFloat(frtEl.textContent.trim()) || 0.0;
    } else {
      const match = document.body.innerText.match(/FRT\s*([0-9]+\.[0-9]{1,2})/i);
      if (match) frt = parseFloat(match[1]) || 0.0;
    }

    // 4. Short Description / Notes
    const descEl = document.querySelector(".shortdesc") || document.querySelector("p.shortdesc");
    if (descEl) {
      description = descEl.textContent.trim().replace(/\s+/g, " ").slice(0, 300);
    }

    // 5. GUID from URL
    const guidMatch = window.location.href.match(/(GUID-[0-9A-F-]{36})/i);
    const docGuid = guidMatch ? guidMatch[1].toUpperCase() : "";

    return {
      title: title,
      correction_code: correctionCode,
      frt: frt,
      description: description,
      doc_guid: docGuid,
      tesla_manual_url: window.location.href
    };
  }

  function initWidget() {
    const procData = extractProcedureData();
    // Only show if we found at least a title or correction code
    if (!procData.correction_code && !procData.frt && !window.location.href.includes("GUID-")) {
      return;
    }

    const widget = document.createElement("div");
    widget.id = "ism-tesla-widget";

    widget.innerHTML = `
      <div class="ism-card-header" id="ism-widget-header">
        <div class="ism-logo-title">
          <span>⚡ ISM Importer</span>
          ${procData.frt ? `<span class="ism-badge-frt">${procData.frt.toFixed(2)} hrs</span>` : ""}
        </div>
        <button type="button" class="ism-toggle-btn" id="ism-toggle-btn" title="Minimize/Maximize">_</button>
      </div>
      <div class="ism-card-body">
        <div class="ism-proc-title">${escapeHtml(procData.title || "Tesla Procedure")}</div>
        <div class="ism-proc-meta">
          <div>Code: <strong>${escapeHtml(procData.correction_code || "N/A")}</strong></div>
          <div>FRT: <strong>${procData.frt ? procData.frt.toFixed(2) + " hrs" : "N/A"}</strong></div>
        </div>
        <div class="ism-ro-selector-container">
          <div class="ism-ro-label">Target Repair Order</div>
          <select class="ism-ro-select" id="ism-ro-select">
            <option value="">Loading Repair Orders...</option>
          </select>
        </div>
        <div class="ism-status-msg" id="ism-status-msg"></div>
      </div>
      <div class="ism-card-footer">
        <button type="button" class="ism-btn-import" id="ism-btn-import">
          <span>Import to Repair Order</span>
        </button>
      </div>
    `;

    document.body.appendChild(widget);

    const toggleBtn = widget.querySelector("#ism-toggle-btn");
    const header = widget.querySelector("#ism-widget-header");
    const roSelect = widget.querySelector("#ism-ro-select");
    const importBtn = widget.querySelector("#ism-btn-import");
    const statusMsg = widget.querySelector("#ism-status-msg");

    // Toggle minimize
    const toggleMinimize = (e) => {
      e.stopPropagation();
      widget.classList.toggle("minimized");
      toggleBtn.textContent = widget.classList.contains("minimized") ? "+" : "_";
    };
    toggleBtn.addEventListener("click", toggleMinimize);
    header.addEventListener("click", (e) => {
      if (e.target !== toggleBtn) toggleMinimize(e);
    });

    // Extract ism_ro from URL hash or query params
    let urlRo = "";
    try {
      const hashMatch = window.location.hash.match(/ism_ro=([^&]+)/i);
      if (hashMatch) urlRo = decodeURIComponent(hashMatch[1]);
      if (!urlRo) {
        const searchMatch = window.location.search.match(/ism_ro=([^&]+)/i);
        if (searchMatch) urlRo = decodeURIComponent(searchMatch[1]);
      }
    } catch (e) {}

    // Fetch active ROs and sync
    chrome.runtime.sendMessage({ action: "GET_CONTEXT" }, (context) => {
      const activeRoName = urlRo || context?.active_ro?.name;

      chrome.runtime.sendMessage({ action: "FETCH_ACTIVE_ROS" }, (res) => {
        roSelect.innerHTML = "";
        const orders = res?.orders || [];

        if (orders.length === 0) {
          const opt = document.createElement("option");
          opt.value = "";
          opt.textContent = "No open Repair Orders found";
          roSelect.appendChild(opt);
          importBtn.disabled = true;
          return;
        }

        let selectedMatched = false;
        orders.forEach((ro) => {
          const opt = document.createElement("option");
          opt.value = ro.name;
          const vInfo = [ro.model_year, ro.model].filter(Boolean).join(" ");
          opt.textContent = `${ro.name} - ${ro.customer || "Customer"} (${vInfo || "Tesla"})`;
          if (activeRoName && ro.name === activeRoName) {
            opt.selected = true;
            selectedMatched = true;
          }
          roSelect.appendChild(opt);
        });

        if (!selectedMatched && orders.length > 0) {
          roSelect.value = orders[0].name;
        }

        importBtn.disabled = !roSelect.value;

        // Persist the currently selected RO to extension storage
        const currentSelected = orders.find((o) => o.name === roSelect.value) || { name: roSelect.value };
        chrome.runtime.sendMessage({
          action: "SYNC_ISM_CONTEXT",
          ro: currentSelected
        });
      });
    });

    // When technician switches RO in dropdown, sync globally
    roSelect.addEventListener("change", () => {
      chrome.runtime.sendMessage({
        action: "SYNC_ISM_CONTEXT",
        ro: { name: roSelect.value }
      });
    });

    // Handle Import
    const triggerImport = (allowMismatch = false, duplicateAction = null) => {
      const selectedRo = roSelect.value;
      if (!selectedRo) {
        showStatus("Please select a target Repair Order.", "error");
        return;
      }

      importBtn.disabled = true;
      importBtn.textContent = "Importing...";
      showStatus("Sending procedure to ISM...", "warning");

      chrome.runtime.sendMessage({
        action: "IMPORT_PROCEDURE",
        repair_order: selectedRo,
        procedure_data: procData,
        allow_generation_mismatch: allowMismatch,
        duplicate_action: duplicateAction
      }, (res) => {
        importBtn.disabled = false;
        importBtn.textContent = "Import to Repair Order";

        if (!res) {
          showStatus("No response from ISM backend. Ensure ISM tab is open.", "error");
          return;
        }

        if (!res.success) {
          showStatus(`Import failed: ${res.error || "Unknown error"}`, "error");
          return;
        }

        const result = res.result;
        if (result?.status === "warning" && result?.requires_confirmation) {
          showWarningConfirmation(result.warning, duplicateAction);
          return;
        }

        if (result?.status === "duplicate_warning" && result?.requires_duplicate_choice) {
          showDuplicateConfirmation(result, allowMismatch);
          return;
        }

        if (result?.status === "success") {
          showSuccess(result);
        } else {
          showStatus(result?.message || "Import completed.", "success");
        }
      });
    };

    importBtn.addEventListener("click", () => triggerImport(false));

    function showStatus(text, type) {
      statusMsg.className = `ism-status-msg ${type}`;
      statusMsg.textContent = text;
      statusMsg.style.display = "block";
    }

    function showWarningConfirmation(warningText, duplicateAction = null) {
      statusMsg.className = "ism-status-msg warning";
      statusMsg.innerHTML = `
        <div><strong>Generation Mismatch:</strong> ${escapeHtml(warningText)}</div>
        <div class="ism-mismatch-actions">
          <button type="button" class="ism-btn-secondary" id="ism-btn-confirm-import">Proceed Anyway</button>
          <button type="button" class="ism-btn-secondary" id="ism-btn-cancel-import">Cancel</button>
        </div>
      `;
      statusMsg.style.display = "block";

      document.getElementById("ism-btn-confirm-import").addEventListener("click", () => {
        triggerImport(true, duplicateAction);
      });
      document.getElementById("ism-btn-cancel-import").addEventListener("click", () => {
        statusMsg.style.display = "none";
      });
    }

    function showDuplicateConfirmation(result, allowMismatch) {
      statusMsg.className = "ism-status-msg warning";
      const code = result.correction_code || (procData && procData.correction_code) || "";
      const promptText = result.message || `Correction code ${code} is already on this Repair Order. Update existing row or Add as additional job?`;

      statusMsg.innerHTML = `
        <div class="ism-prompt-text">${escapeHtml(promptText)}</div>
        <div class="ism-duplicate-actions">
          <button type="button" class="ism-btn-duplicate ism-btn-update" id="ism-btn-dup-update">Update Existing Row</button>
          <button type="button" class="ism-btn-duplicate ism-btn-add" id="ism-btn-dup-add">Add as Additional Job</button>
          <button type="button" class="ism-btn-duplicate ism-btn-cancel" id="ism-btn-dup-cancel">Cancel</button>
        </div>
      `;
      statusMsg.style.display = "block";

      document.getElementById("ism-btn-dup-update").addEventListener("click", () => {
        triggerImport(allowMismatch, "update");
      });
      document.getElementById("ism-btn-dup-add").addEventListener("click", () => {
        triggerImport(allowMismatch, "add");
      });
      document.getElementById("ism-btn-dup-cancel").addEventListener("click", () => {
        statusMsg.style.display = "none";
      });
    }

    function showSuccess(result) {
      statusMsg.className = "ism-status-msg success";
      const actionVerb = result.action === "updated" ? "Updated" : "Added";
      statusMsg.innerHTML = `
        <div>✔ <strong>${actionVerb}!</strong> ${escapeHtml(result.service_package)} (${result.frt} hrs) on ${escapeHtml(result.repair_order)}.</div>
      `;
      statusMsg.style.display = "block";
      importBtn.textContent = `✔ ${actionVerb} on RO`;
      importBtn.disabled = true;
    }
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // Initialize once DOM is ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initWidget);
  } else {
    initWidget();
  }
})();
