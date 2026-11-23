"""Source records and completion rules for belt-handoff."""

from . import belt_views

BELT_HANDOFF = {
    'belt-handoff': {
        'title': '首件接驳放行',
        'reference_label': '行李带交接或工单编号',
        'outcomes': {
            'delivered': ('首件在到达窗口内出现', ('delivered',)),
            'ground': ('转交地服查找首件', None),
        },
        'sql': belt_views.belt_handoff_rows,
    },
}
