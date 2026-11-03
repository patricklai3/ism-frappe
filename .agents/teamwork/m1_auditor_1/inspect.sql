SELECT name, tag_name, description FROM `tabEquipment Tag` ORDER BY name;

SELECT name, color, requires_repair_order, description FROM `tabSchedule Entry Type` ORDER BY name;

SELECT field, value FROM `tabSingles` WHERE doctype='ISM Settings' AND field IN (
    'operating_hours_start',
    'operating_hours_end',
    'break_start',
    'break_end',
    'default_slot_interval',
    'scheduling_horizon_days',
    'holiday_list',
    'technician_designation',
    'enable_technician_capacity',
    'estimation_percentile',
    'estimation_sigma',
    'buffer_padding_minutes',
    'default_diagnostic_duration'
) ORDER BY field;

SELECT name, role_name, desk_access FROM `tabRole` WHERE name='ISM Manager';
