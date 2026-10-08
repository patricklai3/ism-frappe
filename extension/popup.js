// Popup script to display and select active RO status

document.addEventListener("DOMContentLoaded", () => {
  const roSelect = document.getElementById("active-ro-select");
  const roVehicleEl = document.getElementById("active-ro-vehicle");
  const openIsmLink = document.getElementById("open-ism-link");

  chrome.runtime.sendMessage({ action: "GET_CONTEXT" }, (context) => {
    const activeRoName = context?.active_ro?.name;
    const origin = context?.ism_origin || "http://localhost:8000";

    chrome.runtime.sendMessage({ action: "FETCH_ACTIVE_ROS" }, (res) => {
      roSelect.innerHTML = "";
      const orders = res?.orders || [];

      if (orders.length === 0) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No open Repair Orders found";
        roSelect.appendChild(opt);
        roVehicleEl.textContent = "Create a Repair Order in ISM to get started";
        return;
      }

      let selectedOrder = null;
      orders.forEach((ro) => {
        const opt = document.createElement("option");
        opt.value = ro.name;
        const vInfo = [ro.model_year, ro.model].filter(Boolean).join(" ");
        opt.textContent = `${ro.name} - ${ro.customer || "Customer"} (${vInfo || "Tesla"})`;
        if (activeRoName && ro.name === activeRoName) {
          opt.selected = true;
          selectedOrder = ro;
        }
        roSelect.appendChild(opt);
      });

      if (!selectedOrder && orders.length > 0) {
        roSelect.value = orders[0].name;
        selectedOrder = orders[0];
      }

      if (selectedOrder) {
        const vInfo = [selectedOrder.model_year, selectedOrder.model, selectedOrder.license_plate].filter(Boolean).join(" • ");
        roVehicleEl.textContent = `${selectedOrder.customer || "Customer"} | ${vInfo || "Tesla"}`;

        // Save selected RO into extension context
        chrome.runtime.sendMessage({
          action: "SYNC_ISM_CONTEXT",
          ro: selectedOrder
        });
      }
    });

    roSelect.addEventListener("change", () => {
      chrome.runtime.sendMessage({ action: "FETCH_ACTIVE_ROS" }, (res) => {
        const orders = res?.orders || [];
        const chosen = orders.find((o) => o.name === roSelect.value) || { name: roSelect.value };
        if (chosen) {
          const vInfo = [chosen.model_year, chosen.model, chosen.license_plate].filter(Boolean).join(" • ");
          roVehicleEl.textContent = `${chosen.customer || "Customer"} | ${vInfo || "Tesla"}`;
          chrome.runtime.sendMessage({
            action: "SYNC_ISM_CONTEXT",
            ro: chosen
          });
        }
      });
    });

    openIsmLink.href = `${origin}/desk#List/Repair%20Order/List`;
    openIsmLink.addEventListener("click", (e) => {
      e.preventDefault();
      chrome.tabs.create({ url: openIsmLink.href });
    });
  });
});
