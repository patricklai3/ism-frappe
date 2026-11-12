// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.provide("frappe.views.calendar");

frappe.views.calendar["Schedule Entry"] = {
	field_map: {
		start: "start",
		end: "end",
		id: "name",
		title: "title",
		allDay: "allDay",
		status: "status",
	},
	get_events_method: "ism.api.scheduling_views.get_calendar_events",
	get_css_class: function (d) {
		const status = d.status || d.extendedProps?.status;
		const map = {
			"Draft": "gray",
			"Scheduled": "blue",
			"Confirmed": "green",
			"In Progress": "orange",
			"Completed": "cyan",
			"Cancelled": "red",
			"No Show": "dark",
		};
		return map[status] || "blue";
	},
	options: {
		editable: false,
		selectable: true,
		select: function (selectionInfo) {
			const start_date = selectionInfo.startStr ? selectionInfo.startStr.split("T")[0] : frappe.datetime.get_today();
			const start_time = selectionInfo.startStr && selectionInfo.startStr.includes("T")
				? selectionInfo.startStr.split("T")[1].slice(0, 8)
				: "09:00:00";
			frappe.new_doc("Schedule Entry", {
				scheduled_date: start_date,
				scheduled_time: start_time,
			});
		},
		eventDidMount: function (info) {
			const event = info.event;
			const props = event.extendedProps || {};
			const $el = $(info.el);

			const status = props.status || event.title;
			const color_map = {
				"Draft": "#9e9e9e",
				"Scheduled": "#2196f3",
				"Confirmed": "#4caf50",
				"In Progress": "#ff9800",
				"Completed": "#00bcd4",
				"Cancelled": "#f44336",
				"No Show": "#795548",
			};
			const border_color = props.color || color_map[status] || "#2196f3";

			$el.css({
				"border-left": "5px solid " + border_color,
				"padding-left": "4px",
			});

			let badges_html = "";
			if (props.service_bay) {
				badges_html += `<span class="badge badge-secondary" style="margin-right: 4px; font-size: 10px;">${frappe.utils.escape_html(props.service_bay)}</span>`;
			}

			if (badges_html) {
				const title_el = $el.find(".fc-event-title, .fc-title");
				if (title_el.length) {
					title_el.append(`<div class="schedule-entry-badges" style="margin-top: 3px;">${badges_html}</div>`);
				} else {
					$el.append(`<div class="schedule-entry-badges" style="margin-top: 3px; padding: 2px;">${badges_html}</div>`);
				}
			}

			const tooltip_content = `
				<div class="schedule-entry-popover" style="text-align: left; padding: 4px; font-size: 12px;">
					<div style="font-weight: bold; margin-bottom: 4px;">${frappe.utils.escape_html(event.title || "")}</div>
					<div><b>Entry Type:</b> ${frappe.utils.escape_html(props.entry_type || "Appointment")}</div>
					<div><b>Status:</b> ${frappe.utils.escape_html(props.status || "")}</div>
					<div><b>Service Bay:</b> ${frappe.utils.escape_html(props.service_bay || "Unassigned")}</div>
					<div><b>Technician:</b> ${frappe.utils.escape_html(props.assigned_technician || "Unassigned")}</div>
				</div>
			`;

			if ($.fn.popover) {
				$el.popover({
					title: `<span style="font-weight: 600;">${frappe.utils.escape_html(event.id || event.title)}</span>`,
					content: tooltip_content,
					html: true,
					trigger: "hover",
					placement: "auto",
					container: "body",
				});
			} else {
				$el.attr("title", `${event.title} | ${props.status} | Bay: ${props.service_bay || "Unassigned"}`);
			}
		},
	},
};

// Ambient Realtime synchronization: automatically refetch events when an appointment is booked or moved
if (frappe.realtime) {
	frappe.realtime.off("ism_calendar_update");
	frappe.realtime.on("ism_calendar_update", function (data) {
		if (window.cur_calendar) {
			window.cur_calendar.refresh();
		}
	});
}
